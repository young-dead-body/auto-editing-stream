#!/usr/bin/env python3
"""Chunked local transcription of long video/audio with ffmpeg + mlx_whisper.
Usage: python transcribe_stream.py INPUT --work-dir WORKDIR [--audio-stream 0] [--max-chunks 1]
Requires ffmpeg and ffprobe in PATH; MLX mode also requires mlx_whisper.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import re
import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path
from device_profile import profile


def run(cmd: list[str]) -> None:
    print('+', ' '.join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def duration_seconds(path: Path) -> float:
    p = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
    ], capture_output=True, text=True, check=True)
    value = float(p.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'Invalid media duration: {value}')
    return value


def audio_stream_count(path: Path) -> int:
    p = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', 'a',
        '-show_entries', 'stream=index', '-of', 'json', str(path)
    ], capture_output=True, text=True, check=True)
    return len(json.loads(p.stdout).get('streams', []))


def read_segments(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'Invalid transcript: {path}')
    segments = data.get('segments')
    if not isinstance(segments, list):
        raise ValueError(f'Missing segments in {path}')
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError(f'Invalid segment in {path}')
        start, end = float(segment['start']), float(segment['end'])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError(f'Invalid segment time in {path}')
    return segments


def write_atomic(path: Path, data: str) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(data, encoding='utf-8')
    temporary.replace(path)


def is_obvious_loop(segment: dict) -> bool:
    """Drop long repeated token loops; raw chunk JSON remains intact."""
    text = str(segment.get('text', '')).lower()
    letters = ''.join(text.split())
    if len(letters) >= 80 and len(set(letters)) <= 2:
        return True
    words = re.findall(r'\w+', text)
    return len(words) >= 12 and Counter(words).most_common(1)[0][1] / len(words) >= 0.7


def remove_decoder_loops(segments: list[dict]) -> tuple[list[dict], int]:
    """Remove long repeated phrases from the combined view, keeping raw JSON."""
    segments = [s for s in segments if str(s.get('text', '')).strip()]
    kept = []
    removed = 0
    i = 0
    while i < len(segments):
        phrase = ' '.join(segments[i]['text'].lower().split())
        j = i + 1
        while (j < len(segments) and phrase and len(phrase) <= 25
               and ' '.join(segments[j]['text'].lower().split()) == phrase
               and float(segments[j]['start']) - float(segments[j - 1]['end']) <= 1.5):
            j += 1
        repeated = j - i >= 5 and float(segments[j - 1]['end']) - float(segments[i]['start']) >= 5
        if repeated:
            removed += j - i
        else:
            for segment in segments[i:j]:
                if is_obvious_loop(segment):
                    removed += 1
                else:
                    kept.append(segment)
        i = j
    return kept, removed


def stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def combine_chunks(work: Path, source: Path, total: int, block: int) -> tuple[int, int]:
    """Publish all finished chunks after each step, including resumed runs."""
    chunks = work / 'chunks'
    combined = []
    omitted_loops = 0
    done = 0
    for idx in range(total):
        item = chunks / f'chunk_{idx:04d}.json'
        if not item.exists():
            continue
        try:
            piece, removed = remove_decoder_loops(read_segments(item))
        except (ValueError, KeyError, TypeError, OSError):
            continue
        done += 1
        omitted_loops += removed
        for seg in piece:
            msg = str(seg.get('text', '')).strip()
            if msg:
                combined.append({'start': round(float(seg['start']) + idx * block, 3),
                                 'end': round(float(seg['end']) + idx * block, 3),
                                 'text': msg, 'chunk_index': idx})
    combined.sort(key=lambda x: (x['start'], x['end']))
    write_atomic(work / 'transcript_timestamps.json',
                 json.dumps({'source': str(source), 'complete': done == total,
                             'omitted_decoder_loops': omitted_loops, 'segments': combined},
                            ensure_ascii=False, indent=2))
    write_atomic(work / 'transcript_timestamps.txt',
                 ''.join(f"[{stamp(item['start'])}–{stamp(item['end'])}] {item['text']}\n" for item in combined))
    return done, omitted_loops


def transcribe_faster(model, audio: Path, language: str, previous_text: bool) -> list[dict]:
    segments, _ = model.transcribe(str(audio), language=language,
                                  condition_on_previous_text=previous_text)
    # faster-whisper performs GPU inference while the generator is consumed.
    return [{'start': segment.start, 'end': segment.end, 'text': segment.text}
            for segment in segments]


def recognize_with_fallback(model_factory, model_name: str, model, device: str,
                            compute_type: str, audio: Path, language: str,
                            previous_text: bool):
    """Retry the whole chunk on CPU if CUDA fails during loading or inference."""
    try:
        if model is None:
            model = model_factory(model_name, device=device, compute_type=compute_type)
        recognized = transcribe_faster(model, audio, language, previous_text)
    except (RuntimeError, OSError) as exc:
        if device != 'cuda':
            raise
        print(f'CUDA недоступна ({exc}); продолжение на CPU', flush=True)
        device = 'cpu'
        model = model_factory(model_name, device='cpu', compute_type='int8')
        recognized = transcribe_faster(model, audio, language, previous_text)
    return model, device, recognized


def main() -> int:
    ap = argparse.ArgumentParser(description='Transcribe long recordings in resumable segments')
    ap.add_argument('source', type=Path, help='Absolute path to original video/audio')
    ap.add_argument('--work-dir', type=Path, default=Path.home() / 'StreamEditor' / 'transcripts')
    ap.add_argument('--audio-stream', type=int, default=0, help='Audio stream index among audio streams (0:a:N)')
    ap.add_argument('--chunk-minutes', type=int, default=30)
    ap.add_argument('--language', default='ru')
    ap.add_argument('--backend', choices=('auto', 'mlx', 'faster-whisper'), default='auto')
    ap.add_argument('--model', default=None)
    ap.add_argument('--condition-on-previous-text', choices=('True', 'False'), default='False',
                    help='False prevents Whisper from looping on game audio')
    ap.add_argument('--max-chunks', type=int, default=0, help='For test: limit chunks this run; 0=all')
    ap.add_argument('--keep-audio', action='store_true')
    args = ap.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_file():
        ap.error(f'File not found: {source}')
    if args.audio_stream < 0 or args.chunk_minutes < 1 or args.max_chunks < 0:
        ap.error('audio-stream >= 0, chunk-minutes >= 1, max-chunks >= 0 required')
    device = profile()
    backend = device['backend'] if args.backend == 'auto' else args.backend
    if args.model is None:
        args.model = device['model'] if backend == device['backend'] else (
            'mlx-community/whisper-large-v3-turbo' if backend == 'mlx' else 'small')
    whisper_device = device['device'] if backend == 'faster-whisper' and device['device'] == 'cuda' else 'cpu'
    compute_type = device['compute_type'] if whisper_device == 'cuda' else 'int8'
    print(f'Whisper: {backend}, {args.model}, {whisper_device}, {compute_type}', flush=True)
    for command in ('ffmpeg', 'ffprobe'):
        if shutil.which(command) is None:
            ap.error(f'{command} is missing from PATH. See README.md for setup.')
    if backend == 'mlx' and shutil.which('mlx_whisper') is None:
        ap.error('mlx_whisper is missing from PATH')
    if backend == 'faster-whisper' and importlib.util.find_spec('faster_whisper') is None:
        ap.error('faster-whisper is not installed')
    available_streams = audio_stream_count(source)
    if args.audio_stream >= available_streams:
        ap.error(f'Audio stream {args.audio_stream} does not exist; found {available_streams} audio stream(s).')
    work = args.work_dir.expanduser().resolve()
    chunks = work / 'chunks'
    chunks.mkdir(parents=True, exist_ok=True)
    length = duration_seconds(source)
    block = args.chunk_minutes * 60
    total = math.ceil(length / block)
    identity = {
        'source': str(source), 'size_bytes': source.stat().st_size,
        'mtime_ns': source.stat().st_mtime_ns,
        'audio_stream': args.audio_stream, 'chunk_seconds': block,
        'model': args.model, 'language': args.language,
        'condition_on_previous_text': args.condition_on_previous_text,
    }
    if backend != 'mlx':
        identity['backend'] = backend
    manifest = work / 'job_manifest.json'
    if manifest.exists():
        previous = json.loads(manifest.read_text(encoding='utf-8'))
        if previous != identity:
            raise SystemExit('Existing work-dir belongs to a different file/settings. Pick another --work-dir.')
    else:
        write_atomic(manifest, json.dumps(identity, ensure_ascii=False, indent=2))
    progress_path = work / 'progress.json'
    progress = {'duration_seconds': length, 'total_chunks': total, 'chunk_seconds': block,
                'current_chunk': 0, 'stage': 'Подготовка'}
    write_atomic(progress_path, json.dumps(progress, ensure_ascii=False))
    until = min(total, args.max_chunks) if args.max_chunks else total
    print(f'Source: {source}; duration: {stamp(length)}; chunks: {total}; processing through: {until}')
    whisper_model = None
    done, omitted_loops = combine_chunks(work, source, total, block)

    for idx in range(until):
        progress.update(current_chunk=idx + 1, stage='Обработка аудио и распознавание')
        write_atomic(progress_path, json.dumps(progress, ensure_ascii=False))
        name = f'chunk_{idx:04d}'
        out_json = chunks / (name + '.json')
        if out_json.exists():
            try:
                read_segments(out_json)
                print(f'Skip done: {name}')
                continue
            except (ValueError, KeyError, TypeError, OSError):
                pass
            out_json.unlink()
        start = idx * block
        seconds = min(block, length - start)
        mp3 = chunks / (name + '.mp3')
        try:
            reusable_audio = mp3.is_file() and abs(duration_seconds(mp3) - seconds) < 2
        except (ValueError, subprocess.CalledProcessError):
            reusable_audio = False
        if reusable_audio:
            print(f'Reuse audio: {mp3}')
        else:
            run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                 '-ss', str(start), '-i', str(source), '-t', str(seconds),
                 '-map', f'0:a:{args.audio_stream}', '-vn', '-ac', '1', '-ar', '16000',
                 '-c:a', 'libmp3lame', '-b:a', '64k', str(mp3)])
        if backend == 'mlx':
            run(['mlx_whisper', str(mp3), '--model', args.model, '--language', args.language,
                 '--condition-on-previous-text', args.condition_on_previous_text,
                 '-f', 'json', '-o', str(chunks), '--output-name', name])
        else:
            from faster_whisper import WhisperModel
            whisper_model, whisper_device, recognized = recognize_with_fallback(
                WhisperModel, args.model, whisper_model, whisper_device, compute_type,
                mp3, args.language, args.condition_on_previous_text == 'True')
            write_atomic(out_json, json.dumps({'segments': recognized}, ensure_ascii=False))
        if not out_json.exists():
            raise RuntimeError(f'Whisper did not create {out_json}. Check CLI logs.')
        read_segments(out_json)
        done, omitted_loops = combine_chunks(work, source, total, block)
        if not args.keep_audio:
            mp3.unlink(missing_ok=True)

    progress['stage'] = 'Сборка транскрипта'
    write_atomic(progress_path, json.dumps(progress, ensure_ascii=False))

    done, omitted_loops = combine_chunks(work, source, total, block)
    print(f'Completed {done}/{total}. Read: {work / "transcript_timestamps.txt"}')
    if omitted_loops:
        print(f'Omitted {omitted_loops} obvious decoder-loop segments from combined transcript; raw chunks are preserved.')
    if done != total:
        print('This is a partial transcript. Run again without --max-chunks to complete.')
    progress['stage'] = 'Готово' if done == total else 'Частично готово'
    write_atomic(progress_path, json.dumps(progress, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print(f'Failed external command (exit {exc.returncode}). Re-run to resume.', file=sys.stderr)
        sys.exit(exc.returncode or 1)

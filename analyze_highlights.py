#!/usr/bin/env python3
"""Rank short transcript passages using speech, excitement and audio energy."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
import csv
import json
import math
from pathlib import Path
import re
import subprocess
import wave
import tempfile
from device_profile import profile

COLUMNS = ['start', 'end', 'category', 'score', 'reason', 'needs_video_review', 'relevance']
CUES = re.compile(r'\b(ого|вау|ничего себе|невероятно|побед[а-я]*|выигр[а-я]*|спас[а-я]*|лучший|смешн[а-я]*|смотри[а-я]*|круто|жесть|wow|amazing|win|clutch|funny|look)\b', re.I)


def speech_vector(text):
    """Compact word-frequency vector for a passage of speech."""
    return Counter(word for word in re.findall(r'[а-яёa-z]{3,}', text.lower())
                   if word not in {'это', 'как', 'что', 'вот', 'там', 'тут', 'the', 'and', 'for'})


def cosine(left, right):
    norm = math.sqrt(sum(value * value for value in left.values()) *
                     sum(value * value for value in right.values()))
    return sum(value * right.get(word, 0) for word, value in left.items()) / norm if norm else 0


def stamp(value):
    value = max(0, int(value))
    return f'{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}'


def audio_energy(source, stream):
    """Decode low rate mono audio into one RMS value per second."""
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / 'audio.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                        '-map', f'0:a:{stream}', '-vn', '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le', str(audio)], check=True)
        with wave.open(str(audio), 'rb') as stream_file:
            rate = stream_file.getframerate()
            energies = []
            import audioop
            while block := stream_file.readframes(rate):
                energies.append(audioop.rms(block, 2) / 32768)
    return energies


def candidates(segments, energies, duration):
    valid = sorted((s for s in segments if s.get('text') and float(s['end']) > float(s['start'])), key=lambda s: float(s['start']))
    baseline = sorted(energies)[len(energies) // 2] if energies else 0
    proposed = []
    starts = [float(s['start']) for s in valid]
    for center in range(0, math.ceil(duration), 20):
        start = max(0, center - 15)
        end = min(duration, center + 30)
        if end - start < 5:
            continue
        nearby = [s for s in valid[max(0, bisect_left(starts, start) - 2):bisect_left(starts, end)]
                  if float(s['end']) > start]
        words = ' '.join(str(s['text']) for s in nearby)
        previous = ' '.join(str(s['text']) for s in valid[bisect_left(starts, start - 75):
                                                       bisect_left(starts, start - 5)])
        vector = speech_vector(words)
        novelty = (1 - cosine(vector, speech_vector(previous))) if previous and vector else 0
        cue = len(CUES.findall(words))
        excitement = min(2, words.count('!'))
        spoken = sum(max(0, min(end, float(s['end'])) - max(start, float(s['start']))) for s in nearby)
        selected = energies[int(start):max(int(start) + 1, int(end))]
        peak = max(selected, default=0)
        increase_db = 20 * math.log10(max(peak, .0001) / max(baseline, .0001)) if energies else 0
        energy = min(2, max(0, increase_db / 6))
        score = cue * 2 + excitement + min(3, spoken / 10) + energy + novelty
        if (score < 3 if valid else energy < 1.5):
            continue
        reason = []
        if cue: reason.append('эмоциональная речь')
        if excitement: reason.append('восклицание')
        if energy >= 1.5: reason.append('всплеск громкости')
        if novelty >= .7: reason.append('новая тема речи')
        if not reason: reason.append('плотная речь' if valid else 'изменение звука')
        proposed.append((score, start, end, ', '.join(reason)))
    chosen = []
    for score, start, end, reason in sorted(proposed, reverse=True):
        if len(chosen) >= max(3, math.ceil(duration / 3600) * 12):
            break
        if any(max(0, min(end, b) - max(start, a)) > 10 for _, a, b, _ in chosen):
            continue
        chosen.append((score, start, end, reason))
    return sorted(chosen, key=lambda item: item[1])


def model_review(source, picks, audio_stream):
    """Recheck shortlisted audio with a local speech model suited to the device."""
    from faster_whisper import WhisperModel
    hardware = profile()
    gpu = hardware['device'] == 'cuda'
    model_name = hardware['model'] if hardware['backend'] == 'faster-whisper' else 'small'
    try:
        model = WhisperModel(model_name, device='cuda' if gpu else 'cpu',
                             compute_type=hardware['compute_type'] if gpu else 'int8')
    except (RuntimeError, OSError):
        if not gpu:
            raise
        print('CUDA недоступна при углублённом анализе; продолжение на CPU', flush=True)
        model = WhisperModel(model_name, device='cpu', compute_type='int8')
    reviewed = []
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / 'candidate.wav'
        for index, (score, start, end, reason) in enumerate(picks, 1):
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                            '-ss', str(start), '-i', str(source), '-t', str(end - start),
                            '-map', f'0:a:{audio_stream}', '-vn', '-ac', '1', '-ar', '16000', str(audio)], check=True)
            speech, _ = model.transcribe(str(audio), condition_on_previous_text=False)
            words = ' '.join(piece.text for piece in speech).strip()
            count = len(words.split())
            cues = len(CUES.findall(words))
            score += min(3, cues * 1.5) + min(1, count / 25)
            if count >= 3:
                reason += ', модель подтвердила речь'
            else:
                score -= 1
                reason += ', модель не обнаружила речь'
            reviewed.append((score, start, end, reason))
            print(f'Модель проверила {index}/{len(picks)}', flush=True)
    return reviewed, model_name


def analyze(source, transcript, output, audio_stream=0, deep=False):
    data = json.loads(Path(transcript).read_text(encoding='utf-8')) if Path(transcript).is_file() else {}
    segments = data.get('segments', [])
    duration = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                     '-of', 'default=noprint_wrappers=1:nokey=1', str(source)],
                                    check=True, capture_output=True, text=True).stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Неверная длительность видео')
    energies = audio_energy(source, audio_stream)
    picks = candidates(segments, energies, duration)
    reviewed_model = None
    if deep and picks:
        picks, reviewed_model = model_review(source, picks, audio_stream)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for score, start, end, reason in picks:
            writer.writerow({'start': stamp(start), 'end': stamp(end), 'category': 'авто',
                             'score': min(5, max(1, round(score / 2))), 'reason': reason,
                             'needs_video_review': 'true'})
    temporary.replace(output)
    output.with_suffix('.visual.json').unlink(missing_ok=True)
    output.with_suffix('.visual-progress.json').unlink(missing_ok=True)
    stats = {'count': len(picks), 'duration_seconds': sum(end - start for _, start, end, _ in picks),
             'source_duration_seconds': duration,
             'method': ('речь, речевой вектор и громкость' if segments else 'всплески громкости') +
                       (f'; проверка Whisper {reviewed_model}' if reviewed_model else ''),
             'csv': str(output)}
    output.with_suffix('.stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--audio-stream', type=int, default=0)
    parser.add_argument('--deep', action='store_true', help='Recheck candidates with a local Whisper model')
    args = parser.parse_args()
    analyze(args.source, args.transcript, args.output, args.audio_stream, args.deep)

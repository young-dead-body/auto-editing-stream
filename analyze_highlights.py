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
    """Decode audio into per-second loudness and frequency vectors."""
    import numpy as np
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / 'audio.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                        '-map', f'0:a:{stream}', '-vn', '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le', str(audio)], check=True)
        with wave.open(str(audio), 'rb') as stream_file:
            rate = stream_file.getframerate()
            energies = []
            while block := stream_file.readframes(rate):
                samples = np.frombuffer(block, dtype='<i2').astype(np.float32) / 32768
                if not len(samples):
                    continue
                rms = float(np.sqrt(np.mean(samples * samples)))
                spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
                frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
                bands = [float(np.sum(spectrum[(frequencies >= low) & (frequencies < high)]))
                         for low, high in ((0, 250), (250, 1000), (1000, 3000), (3000, rate / 2 + 1))]
                total = sum(bands)
                energies.append((rms, tuple(value / total for value in bands) if total else (0, 0, 0, 0)))
    return energies


def audio_events(energies):
    """Find short changes relative to the nearby audio, not absolute volume."""
    if not energies:
        return []
    volumes = [value[0] if isinstance(value, (tuple, list)) else value for value in energies]
    global_median = sorted(volumes)[len(volumes) // 2]
    events = []
    for idx, value in enumerate(energies):
        earlier = volumes[max(0, idx - 30):idx]
        local = sorted(earlier)[len(earlier) // 2] if earlier else global_median
        rise = max(0, 20 * math.log10(max(volumes[idx], .0001) / max(local, .0001)))
        if isinstance(value, (tuple, list)) and earlier:
            previous = [item[1] for item in energies[max(0, idx - 30):idx]]
            mean = [sum(band[k] for band in previous) / len(previous) for k in range(4)]
            similarity = cosine(dict(enumerate(value[1])), dict(enumerate(mean)))
            change = max(0, 1 - similarity)
            events.append(min(3, rise / 7) + min(2, change * 8))
        else:
            events.append(min(3, rise / 7))
    return events


def candidates(segments, energies, duration):
    valid = sorted((s for s in segments if s.get('text') and float(s['end']) > float(s['start'])), key=lambda s: float(s['start']))
    events = audio_events(energies)
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
        energy = max(events[int(start):max(int(start) + 1, int(end))], default=0)
        score = cue * 2 + excitement + min(3, spoken / 10) + energy + novelty
        if valid and (score < 3 or (not cue and not excitement and novelty < .5 and energy < 1.5)):
            continue
        if not valid and energy < 1.5:
            continue
        reason = []
        if cue: reason.append('эмоциональная речь')
        if excitement: reason.append('восклицание')
        if energy >= 1.5: reason.append('изменение звука')
        if novelty >= .7: reason.append('новая тема речи')
        if not reason: reason.append('плотная речь' if valid else 'изменение звука')
        focus = next((s for s in nearby if CUES.search(str(s['text']))), None)
        if focus is None:
            focus = next((s for s in nearby if '!' in str(s['text'])), None)
        if focus is not None:
            anchor = (float(focus['start']) + float(focus['end'])) / 2
        elif events and energy >= 1.5:
            indices = range(int(start), min(len(events), math.ceil(end)))
            anchor = max(indices, key=events.__getitem__) if indices else (start + end) / 2
        else:
            anchor = (start + end) / 2
        clip_start, clip_end = max(0, anchor - 8), min(duration, anchor + 22)
        proposed.append((score, clip_start, clip_end, ', '.join(reason)))
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
        gpu = False
        model = WhisperModel(model_name, device='cpu', compute_type='int8')
    reviewed = []
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / 'candidate.wav'
        for index, (score, start, end, reason) in enumerate(picks, 1):
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                            '-ss', str(start), '-i', str(source), '-t', str(end - start),
                            '-map', f'0:a:{audio_stream}', '-vn', '-ac', '1', '-ar', '16000', str(audio)], check=True)
            try:
                speech, _ = model.transcribe(str(audio), condition_on_previous_text=False)
                words = ' '.join(piece.text for piece in speech).strip()
            except (RuntimeError, OSError) as exc:
                if not gpu:
                    raise
                print(f'CUDA недоступна при проверке речи ({exc}); продолжение на CPU', flush=True)
                gpu = False
                model = WhisperModel(model_name, device='cpu', compute_type='int8')
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
             'method': ('речь, речевой вектор и звуковые признаки' if segments else 'звуковые признаки') +
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

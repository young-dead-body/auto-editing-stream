#!/usr/bin/env python3
"""Add model-based audio and visual relevance to an existing shortlist CSV."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import time

from analyze_highlights import audio_energy, model_review
from visual_analysis import PROMPT_VERSION, review, vision_profile

def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def seconds(value):
    parts = value.split(':')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f'Неверный таймкод: {value}')
    h, m, s = map(int, parts)
    if m > 59 or s > 59:
        raise ValueError(f'Неверный таймкод: {value}')
    return h * 3600 + m * 60 + s


def fingerprint(source, rows, audio_stream):
    source = Path(source).resolve()
    stat = source.stat()
    return {'source': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'audio_stream': audio_stream,
            'intervals': [[r['start'], r['end']] for r in rows], 'prompt_version': PROMPT_VERSION}


def relevance_score(event, reaction, clarity):
    result = round(25 * (.45 * event + .35 * reaction + .20 * clarity))
    return min(result, 49) if event == 0 else result


def rough_score(row):
    try:
        value = float(row.get('score') or 3)
        return value * 2 if math.isfinite(value) else 6.0
    except (TypeError, ValueError):
        return 6.0


def deep_analyze(source, csv_path, transcript_path, cache_dir, audio_stream=0):
    csv_path = Path(csv_path)
    with csv_path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows:
        raise ValueError('Быстрый поиск не нашёл моментов: углублённый анализ не запущен')
    intervals = [(seconds(row['start']), seconds(row['end'])) for row in rows]
    if any(end <= start for start, end in intervals):
        raise ValueError('Список моментов содержит неверные границы')
    identity = fingerprint(source, rows, audio_stream)
    report_path = csv_path.with_suffix('.visual.json')
    progress_path = csv_path.with_suffix('.visual-progress.json')
    progress = {'stage': 'Подготовка', 'completed': 0, 'total': len(rows), 'model': '', 'error': ''}
    atomic_json(progress_path, progress)
    data = json.loads(Path(transcript_path).read_text(encoding='utf-8')) if Path(transcript_path).is_file() else {}
    transcript = data.get('segments', [])
    audio_error = ''
    audio_available = True
    try:
        energies = audio_energy(source, audio_stream)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        energies = []
        audio_available = False
        audio_error = str(exc)
        print(f'Аудиодорожка недоступна: {exc}', flush=True)
    picks = [(rough_score(row), start, end, row.get('reason', ''))
             for row, (start, end) in zip(rows, intervals)]
    audio_results = None
    audio_model = ''
    if audio_available:
        try:
            progress['stage'] = 'Проверка речи моделью Whisper'
            atomic_json(progress_path, progress)
            reviewed, audio_model = model_review(source, picks, audio_stream)
            audio_results = [min(4, max(0, round((new[0] - old[0]) * 1.25)))
                             for old, new in zip(picks, reviewed)]
        except (OSError, RuntimeError, ValueError, ImportError, subprocess.SubprocessError) as exc:
            audio_error = str(exc)
            print(f'Аудиомодель недоступна: {exc}', flush=True)
    old_report = None
    if report_path.is_file():
        try:
            old_report = json.loads(report_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    requested_tier = requested_device = ''
    profile_error = ''
    try:
        requested_tier, requested_device = vision_profile()
    except RuntimeError as exc:
        profile_error = str(exc)
    if (old_report and old_report.get('identity') == identity and
            old_report.get('visual_status') == 'complete' and
            ((old_report.get('model') == requested_tier and
              old_report.get('device') == requested_device) or profile_error)):
        visual = old_report['items']
        tier, kind = old_report.get('model', ''), old_report.get('device', '')
        print('Повторное использование визуальной оценки для неизменённых интервалов', flush=True)
    else:
        def update(completed, total, model):
            progress.update(stage='Визуальная оценка', completed=completed, total=total, model=model)
            atomic_json(progress_path, progress)
        try:
            if profile_error:
                raise RuntimeError(profile_error)
            progress.update(stage='Загрузка видеомодели', model=requested_tier, device=requested_device)
            atomic_json(progress_path, progress)
            frames_dir = csv_path.with_suffix('.visual-frames')
            if frames_dir.is_dir():
                for old_frame in frames_dir.glob('[0-9]*_[0-9][0-9].jpg'):
                    old_frame.unlink()
            visual, tier, kind = review(source, intervals, energies, transcript, cache_dir, update,
                                       frames_dir)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            visual, tier, kind = [], '', ''
            progress['error'] = str(exc)
            print(f'Видеомодель недоступна: {exc}', flush=True)
    details = []
    for index, row in enumerate(rows):
        result = dict(visual[index]) if index < len(visual) else {'error': progress.get('error') or 'Нет оценки'}
        result['start'], result['end'] = row['start'], row['end']
        result['audio_reaction'] = audio_results[index] if audio_results else None
        if 'error' not in result:
            reaction = max(result['visible_reaction'], audio_results[index] if audio_results else 0)
            result['relevance'] = relevance_score(result['event'], reaction, result['clarity'])
            if result.get('low_confidence'):
                result['relevance'] = min(result['relevance'], 49)
            row['relevance'] = str(result['relevance'])
        else:
            row['relevance'] = ''
        details.append(result)
    status = 'complete' if visual and all('error' not in item for item in details) else 'partial'
    report = {'schema_version': 1, 'identity': identity, 'visual_status': status,
              'model': tier, 'device': kind, 'audio_model': audio_model,
              'audio_error': audio_error, 'visual_error': progress.get('error', ''),
              'items': details, 'created_at': time.time()}
    atomic_json(report_path, report)
    temporary = csv_path.with_suffix(csv_path.suffix + '.tmp')
    if 'relevance' not in columns:
        columns.append('relevance')
    with temporary.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path)
    stats_path = csv_path.with_suffix('.stats.json')
    stats = json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.is_file() else {}
    methods = ['Whisper'] if audio_model else []
    methods.append('видеомодель' if status == 'complete' else 'видеомодель частично')
    stats.update(count=len(rows), duration_seconds=sum(end-start for start,end in intervals),
                 method=' + '.join(methods),
                 visual_status=status, visual_model=tier, ranked=sum('relevance' in item for item in details))
    atomic_json(stats_path, stats)
    progress.update(stage='Готово' if status == 'complete' else 'Частично готово',
                    completed=len(rows), model=tier, error=progress.get('error', ''))
    atomic_json(progress_path, progress)
    print(json.dumps({'visual_status': status, 'ranked': stats['ranked'], 'model': tier,
                      'report': str(report_path)}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('csv', type=Path)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--audio-stream', type=int, default=0)
    parser.add_argument('--cache-dir', type=Path, default=Path(os.environ.get('STREAM_EDITOR_DATA', Path.home() / 'StreamEditor' / 'web')) / 'models' / 'visual')
    args = parser.parse_args()
    deep_analyze(args.source, args.csv, args.transcript, args.cache_dir, args.audio_stream)

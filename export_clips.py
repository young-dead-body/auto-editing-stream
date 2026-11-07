#!/usr/bin/env python3
"""Render each selected moment as a standalone MP4."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import re
import subprocess


def seconds(value):
    if not re.fullmatch(r'\d{2,}:[0-5]\d:[0-5]\d', value):
        raise ValueError(f'Неверный таймкод: {value}')
    h, m, s = map(int, value.split(':'))
    return h * 3600 + m * 60 + s


def export(source, csv_path, directory, audio_stream=0):
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError('Список моментов пуст')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows, 1):
        start, end = seconds(row['start']), seconds(row['end'])
        if end <= start:
            raise ValueError(f'Неверные границы момента {index}')
        output = directory / f'clip_{index:03d}.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-ss', str(start), '-i', str(source), '-t', str(end - start),
                        '-map', '0:v:0', '-map', f'0:a:{audio_stream}?', '-c:v', 'libx264', '-preset', 'veryfast',
                        '-crf', '20', '-c:a', 'aac', '-movflags', '+faststart', str(output)], check=True)
        print(f'{index}/{len(rows)}: {output}', flush=True)
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('csv', type=Path)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--audio-stream', type=int, default=0)
    args = parser.parse_args()
    export(args.source, args.csv, args.directory, args.audio_stream)

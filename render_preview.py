#!/usr/bin/env python3
"""Render a resumable 720p review MP4 from the same CSV used for Premiere XML."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path


def time_seconds(value: str) -> int:
    hours, minutes, seconds = map(int, value.split(':'))
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError(f'Invalid time: {value}')
    return hours * 3600 + minutes * 60 + seconds


def duration(path: Path) -> float:
    output = subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
    ], text=True)
    return float(output.strip())


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('highlights', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    highlights = args.highlights.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    with highlights.open(newline='', encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        parser.error('Highlights CSV is empty')
    source_length = duration(source)
    intervals = []
    for index, row in enumerate(rows, 1):
        start, end = time_seconds(row['start']), time_seconds(row['end'])
        if start >= end or end > source_length:
            parser.error(f'Invalid interval in row {index}')
        intervals.append((start, end))

    parts = output.parent / (output.stem + '-parts')
    parts.mkdir(parents=True, exist_ok=True)
    settings = {
        'source': str(source), 'source_size': source.stat().st_size,
        'source_mtime_ns': source.stat().st_mtime_ns,
        'csv_sha256': hashlib.sha256(highlights.read_bytes()).hexdigest(),
        'video': '720p30 libx264 veryfast crf23', 'audio': 'AAC stereo 48kHz 160k',
    }
    manifest = parts / 'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != settings:
        parser.error(f'{parts} contains parts for different media or CSV; choose a new output name')
    manifest.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')

    for index, (start, end) in enumerate(intervals, 1):
        part = parts / f'clip_{index:02d}.mp4'
        if part.exists():
            try:
                if abs(duration(part) - (end - start)) <= 0.1:
                    print(f'[{index}/{len(intervals)}] reuse {part.name}', flush=True)
                    continue
            except (ValueError, subprocess.CalledProcessError):
                pass
        print(f'[{index}/{len(intervals)}] render {start}–{end} s', flush=True)
        run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
             '-ss', str(start), '-i', str(source), '-t', str(end - start),
             '-map', '0:v:0', '-map', '0:a:0',
             '-vf', 'fps=30,scale=-2:720,format=yuv420p',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
             '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-ac', '2',
             '-movflags', '+faststart', str(part)])
        if abs(duration(part) - (end - start)) > 0.1:
            raise RuntimeError(f'Wrong duration for {part}')

    concat_file = parts / 'concat.txt'
    concat_file.write_text('ffconcat version 1.0\n' + ''.join(
        "file '" + str((parts / f'clip_{i:02d}.mp4').resolve()).replace("'", "'\\''") + "'\n"
        for i in range(1, len(intervals) + 1)), encoding='utf-8')
    temporary = output.with_name(output.stem + '.tmp.mp4')
    print(f'Joining {len(intervals)} clips...', flush=True)
    run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
         '-f', 'concat', '-safe', '0', '-i', str(concat_file), '-c', 'copy',
         '-movflags', '+faststart', str(temporary)])
    expected = sum(end - start for start, end in intervals)
    actual = duration(temporary)
    if abs(actual - expected) > 1:
        raise RuntimeError(f'Preview duration {actual:.2f}s differs from expected {expected}s')
    temporary.replace(output)
    print(f'Wrote {output}: {actual:.2f}s', flush=True)


if __name__ == '__main__':
    main()

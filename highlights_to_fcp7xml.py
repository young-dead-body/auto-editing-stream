#!/usr/bin/env python3
"""Create a simple Premiere-compatible FCP7 XML rough cut from highlights CSV.

The XML has one video track and two mono tracks for a stereo source. It uses
integer frame positions at a nominal frame rate; check cuts for VFR sources.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def node(parent: ET.Element, tag: str, value: object | None = None, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, tag, attrs)
    if value is not None:
        child.text = str(value)
    return child


def rate(parent: ET.Element, fps: int) -> None:
    item = node(parent, 'rate')
    node(item, 'timebase', fps)
    node(item, 'ntsc', 'FALSE')


def seconds(value: str) -> int:
    parts = value.split(':')
    if len(parts) != 3:
        raise ValueError(f'Expected HH:MM:SS, got {value!r}')
    hour, minute, second = map(int, parts)
    if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError(f'Invalid time: {value!r}')
    return hour * 3600 + minute * 60 + second


def probe(path: Path) -> dict:
    output = subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,width,height,avg_frame_rate,sample_rate,channels',
        '-of', 'json', str(path)
    ], text=True)
    return json.loads(output)


def load_clips(csv_path: Path, source_duration: float, fps: int) -> list[dict]:
    with csv_path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if not {'start', 'end'} <= set(reader.fieldnames or []):
            raise ValueError('CSV needs start,end columns')
        clips = []
        for line, row in enumerate(reader, 2):
            start, end = seconds(row['start']), seconds(row['end'])
            if start >= end or end > source_duration:
                raise ValueError(f'CSV line {line}: invalid or out-of-bounds interval')
            clips.append({'start': start * fps, 'end': end * fps,
                          'label': f"{len(clips) + 1:02d} {row.get('category', 'highlight')}"})
    if not clips:
        raise ValueError('CSV contains no clips')
    return clips


def clip_item(track: ET.Element, clip: dict, index: int, kind: str,
              timeline_start: int, fps: int, file_id: str,
              linked_ids: tuple[str, str, str]) -> ET.Element:
    identifier = f'clipitem-{kind}-{index:02d}'
    item = node(track, 'clipitem', id=identifier)
    node(item, 'name', clip['label'])
    duration = clip['end'] - clip['start']
    node(item, 'duration', duration)
    rate(item, fps)
    node(item, 'start', timeline_start)
    node(item, 'end', timeline_start + duration)
    node(item, 'in', clip['start'])
    node(item, 'out', clip['end'])
    node(item, 'file', id=file_id)
    if kind != 'v':
        source_track = node(item, 'sourcetrack')
        node(source_track, 'mediatype', 'audio')
        node(source_track, 'trackindex', 1 if kind == 'a1' else 2)
    for linked_id, media_type, track_index in zip(linked_ids, ('video', 'audio', 'audio'), (1, 1, 2)):
        link = node(item, 'link')
        node(link, 'linkclipref', linked_id)
        node(link, 'mediatype', media_type)
        node(link, 'trackindex', track_index)
        node(link, 'clipindex', index)
    return item


def build_xml(source: Path, clips: list[dict], metadata: dict, fps: int) -> ET.ElementTree:
    video_stream = next(x for x in metadata['streams'] if x['codec_type'] == 'video')
    audio_stream = next(x for x in metadata['streams'] if x['codec_type'] == 'audio')
    root = ET.Element('xmeml', version='4')
    sequence = node(root, 'sequence', id='sequence-1')
    node(sequence, 'name', 'Kipofox highlights rough cut')
    node(sequence, 'duration', sum(x['end'] - x['start'] for x in clips))
    rate(sequence, fps)
    media = node(sequence, 'media')
    video = node(media, 'video')
    video_format = node(node(video, 'format'), 'samplecharacteristics')
    rate(video_format, fps)
    node(video_format, 'width', video_stream['width'])
    node(video_format, 'height', video_stream['height'])
    node(video_format, 'anamorphic', 'FALSE')
    node(video_format, 'pixelaspectratio', 'square')
    node(video_format, 'fielddominance', 'none')
    video_track = node(video, 'track')
    audio = node(media, 'audio')
    audio_format = node(node(audio, 'format'), 'samplecharacteristics')
    node(audio_format, 'depth', 16)
    node(audio_format, 'samplerate', audio_stream['sample_rate'])
    audio_tracks = [node(audio, 'track') for _ in range(2)]
    file_id = 'file-1'
    position = 0
    for index, clip in enumerate(clips, 1):
        links = tuple(f'clipitem-{kind}-{index:02d}' for kind in ('v', 'a1', 'a2'))
        clip_item(video_track, clip, index, 'v', position, fps, file_id, links)
        clip_item(audio_tracks[0], clip, index, 'a1', position, fps, file_id, links)
        clip_item(audio_tracks[1], clip, index, 'a2', position, fps, file_id, links)
        position += clip['end'] - clip['start']
    file_node = video_track[0].find('file')
    node(file_node, 'name', source.name)
    node(file_node, 'pathurl', source.as_uri())
    node(file_node, 'duration', round(float(metadata['format']['duration']) * fps))
    rate(file_node, fps)
    file_media = node(file_node, 'media')
    file_video = node(file_media, 'video')
    characteristics = node(file_video, 'samplecharacteristics')
    rate(characteristics, fps)
    node(characteristics, 'width', video_stream['width'])
    node(characteristics, 'height', video_stream['height'])
    file_audio = node(file_media, 'audio')
    characteristics = node(file_audio, 'samplecharacteristics')
    node(characteristics, 'depth', 16)
    node(characteristics, 'samplerate', audio_stream['sample_rate'])
    node(file_audio, 'channelcount', 2)
    ET.indent(root, space='  ')
    return ET.ElementTree(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('highlights', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    metadata = probe(source)
    video_stream = next((x for x in metadata['streams'] if x['codec_type'] == 'video'), None)
    audio_stream = next((x for x in metadata['streams'] if x['codec_type'] == 'audio'), None)
    if video_stream is None or audio_stream is None or int(audio_stream['channels']) != 2:
        parser.error('Expected a video source with one stereo audio stream')
    fps = round(eval_fraction(video_stream['avg_frame_rate']))
    if fps <= 0:
        parser.error('Invalid video frame rate')
    clips = load_clips(args.highlights, float(metadata['format']['duration']), fps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_xml(source, clips, metadata, fps).write(args.output, encoding='utf-8', xml_declaration=True)
    print(f'Wrote {len(clips)} clips at {fps} fps to {args.output}')


def eval_fraction(value: str) -> float:
    numerator, denominator = map(int, value.split('/'))
    return numerator / denominator


if __name__ == '__main__':
    main()

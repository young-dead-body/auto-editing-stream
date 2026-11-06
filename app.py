#!/usr/bin/env python3
"""Local Kipofox web UI for transcription and rough-cut exports."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
os.environ['PATH'] = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get('PATH', '')])
if sys.platform == 'darwin':
    os.environ['PATH'] = os.pathsep.join(['/opt/homebrew/bin', '/usr/local/bin', os.environ.get('PATH', '')])
DATA = Path(os.environ.get('KIPOFOX_DATA', Path.home() / 'KipofoxAI' / 'web')).expanduser()
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = DATA / 'project.json'
COLUMNS = ['start', 'end', 'category', 'score', 'reason', 'needs_video_review']
job_lock = threading.Lock()
job = {'status': 'idle', 'kind': '', 'error': ''}
MAX_UPLOAD = 100 * 1024 ** 3


def project():
    return json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else {}


def save_project(value):
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CONFIG)


def transcript_metrics(p):
    directory = Path(p.get('transcript_dir', '')) if p.get('transcript_dir') else None
    result = {'completed_chunks': 0, 'total_chunks': 0, 'percent': 0,
              'duration_seconds': 0, 'segments': 0, 'omitted_loops': 0,
              'current_chunk': 0, 'stage': ''}
    if not directory:
        return result
    progress = directory / 'progress.json'
    if progress.is_file():
        data = json.loads(progress.read_text(encoding='utf-8'))
        result['duration_seconds'] = data.get('duration_seconds', 0)
        result['total_chunks'] = data.get('total_chunks', 0)
        result['current_chunk'] = data.get('current_chunk', 0)
        result['stage'] = data.get('stage', '')
        result['completed_chunks'] = len(list((directory / 'chunks').glob('chunk_*.json')))
        if result['total_chunks']:
            result['percent'] = min(100, round(100 * result['completed_chunks'] / result['total_chunks']))
    transcript = directory / 'transcript_timestamps.json'
    if transcript.is_file():
        data = json.loads(transcript.read_text(encoding='utf-8'))
        result['segments'] = len(data.get('segments', []))
        result['omitted_loops'] = data.get('omitted_decoder_loops', 0)
    return result


def job_state():
    with job_lock:
        current = dict(job)
    current['elapsed_seconds'] = round(current.get('finished_at', time.time()) - current['started_at']) if current.get('started_at') else 0
    current['metrics'] = transcript_metrics(project())
    log = DATA / 'job.log'
    current['log'] = log.read_text(encoding='utf-8', errors='replace')[-12000:] if log.exists() else ''
    return current


def rows_for(path):
    if not path or not Path(path).is_file():
        return []
    with open(path, newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def stamp_seconds(value):
    if not re.fullmatch(r'\d{2,}:[0-5]\d:[0-5]\d', value):
        raise ValueError(f'Неверный таймкод: {value}')
    hours, minutes, seconds = map(int, value.split(':'))
    return hours * 3600 + minutes * 60 + seconds


def command_for(kind, p):
    source = Path(p['source'])
    output = Path(p['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    if kind == 'transcribe':
        return 'transcribe_stream.py', [str(source), '--work-dir', p['transcript_dir'], '--audio-stream', str(p['audio_stream']), '--backend', p['backend']]
    csv_path = p['highlights']
    if not Path(csv_path).is_file():
        raise ValueError('Сначала сохраните список хайлайтов')
    if kind == 'xml':
        return 'highlights_to_fcp7xml.py', [str(source), csv_path, str(output / 'highlights.xml')]
    if kind == 'preview':
        return 'render_preview.py', [str(source), csv_path, str(output / 'preview.mp4')]
    raise ValueError('Неизвестная операция')


def run_job(kind, p):
    global job
    log_path = DATA / 'job.log'
    try:
        script, arguments = command_for(kind, p)
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--task', script, *arguments]
        else:
            command = [sys.executable, str(BASE / script), *arguments]
        with log_path.open('w', encoding='utf-8') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        with job_lock:
            job = {'status': 'done' if result.returncode == 0 else 'failed', 'kind': kind,
                   'error': '' if result.returncode == 0 else f'Код завершения: {result.returncode}',
                   'started_at': job.get('started_at'), 'finished_at': time.time()}
    except Exception as exc:
        with job_lock:
            job = {'status': 'failed', 'kind': kind, 'error': str(exc),
                   'started_at': job.get('started_at'), 'finished_at': time.time()}


class Handler(BaseHTTPRequestHandler):
    def reply(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 2_000_000:
            raise ValueError('Слишком большой запрос')
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/':
                raw = (BASE / 'web' / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif path == '/api/state':
                p = project()
                transcript = Path(p.get('transcript_dir', '')) / 'transcript_timestamps.json' if p.get('transcript_dir') else None
                segments = []
                if transcript and transcript.is_file():
                    segments = json.loads(transcript.read_text(encoding='utf-8')).get('segments', [])
                self.reply({'project': p, 'rows': rows_for(p.get('highlights')), 'segments': segments, 'job': job_state()})
            elif path == '/api/job':
                self.reply(job_state())
            elif path == '/api/video':
                p = project()
                source = Path(p.get('source', ''))
                if not source.is_file():
                    return self.send_error(404)
                size = source.stat().st_size
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
                start = int(match.group(1)) if match and match.group(1) else 0
                end = min(int(match.group(2)), size - 1) if match and match.group(2) else min(start + 8 * 1024 * 1024 - 1, size - 1)
                if start >= size or end < start:
                    return self.send_error(416)
                self.send_response(206)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Content-Length', str(end - start + 1))
                self.end_headers()
                with source.open('rb') as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                self.send_error(404)
        except (OSError, ValueError, KeyError) as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        try:
            if urlparse(self.path).path == '/api/upload':
                return self.upload_video()
            data = self.body()
            path = urlparse(self.path).path
            if path == '/api/project':
                source = Path(str(data.get('source', ''))).expanduser().resolve()
                if not source.is_file():
                    raise ValueError('Исходный видеофайл не найден')
                p = {'source': str(source), 'audio_stream': int(data.get('audio_stream', 0)),
                     'backend': data.get('backend', 'auto'),
                     'transcript_dir': str(Path(data.get('transcript_dir') or DATA / 'transcripts').expanduser().resolve()),
                     'highlights': str(Path(data.get('highlights') or DATA / 'highlights.csv').expanduser().resolve()),
                     'output_dir': str(Path(data.get('output_dir') or DATA / 'output').expanduser().resolve())}
                if p['audio_stream'] < 0 or p['backend'] not in ('auto', 'mlx', 'faster-whisper'):
                    raise ValueError('Неверные настройки транскрипции')
                save_project(p)
                self.reply({'project': p})
            elif path == '/api/highlights':
                p = project()
                if not p:
                    raise ValueError('Сначала укажите видео')
                rows = data.get('rows')
                if not isinstance(rows, list):
                    raise ValueError('Ожидался список хайлайтов')
                for row in rows:
                    if stamp_seconds(str(row.get('start', ''))) >= stamp_seconds(str(row.get('end', ''))):
                        raise ValueError('Начало должно быть раньше конца')
                target = Path(p['highlights'])
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = io.StringIO()
                writer = csv.DictWriter(stream, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows({key: str(row.get(key, '')) for key in COLUMNS} for row in rows)
                target.with_suffix(target.suffix + '.tmp').write_text(stream.getvalue(), encoding='utf-8')
                target.with_suffix(target.suffix + '.tmp').replace(target)
                self.reply({'saved': str(target)})
            elif path == '/api/start':
                p = project()
                if not p:
                    raise ValueError('Сначала укажите видео')
                kind = data.get('kind')
                command_for(kind, p)
                global job
                with job_lock:
                    if job['status'] == 'running':
                        raise ValueError('Другая операция ещё выполняется')
                    job = {'status': 'running', 'kind': kind, 'error': '', 'started_at': time.time()}
                threading.Thread(target=run_job, args=(kind, p), daemon=True).start()
                self.reply({'started': kind})
            else:
                self.send_error(404)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.reply({'error': str(exc)}, 400)

    def upload_video(self):
        temporary = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_UPLOAD:
                raise ValueError('Размер видео должен быть от 1 байта до 100 ГБ')
            with job_lock:
                if job['status'] == 'running':
                    raise ValueError('Дождитесь завершения текущей операции')
            name = Path(parse_qs(urlparse(self.path).query).get('name', ['video.mp4'])[0]).name
            if not re.search(r'\.(mp4|mkv|mov|webm|avi)$', name, re.I):
                raise ValueError('Поддерживаются MP4, MKV, MOV, WebM и AVI')
            upload_dir = DATA / 'uploads'
            upload_dir.mkdir(parents=True, exist_ok=True)
            destination = upload_dir / f'{uuid.uuid4().hex}{Path(name).suffix.lower()}'
            temporary = destination.with_suffix(destination.suffix + '.part')
            with temporary.open('wb') as output:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Загрузка видео прервана')
                    output.write(block)
                    remaining -= len(block)
            temporary.replace(destination)
            previous = project()
            p = {'source': str(destination), 'audio_stream': int(previous.get('audio_stream', 0)),
                 'backend': previous.get('backend', 'auto'),
                 'transcript_dir': str(DATA / 'transcripts' / destination.stem),
                 'highlights': str(DATA / 'highlights' / f'{destination.stem}.csv'),
                 'output_dir': str(DATA / 'output' / destination.stem)}
            save_project(p)
            self.reply({'project': p, 'name': name, 'size_bytes': length})
        except (OSError, ValueError, TypeError) as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            self.reply({'error': str(exc)}, 400)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '--task':
        import runpy
        script = BASE / sys.argv[2]
        if script.name not in ('transcribe_stream.py', 'highlights_to_fcp7xml.py', 'render_preview.py'):
            raise SystemExit('Unknown task')
        sys.argv = [str(script), *sys.argv[3:]]
        runpy.run_path(str(script), run_name='__main__')
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f'http://127.0.0.1:{args.port}'
    print(f'Kipofox: {url}', flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

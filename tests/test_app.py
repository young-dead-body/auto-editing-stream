"""Checks the local API contract and server startup."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

os.environ.setdefault('STREAM_EDITOR_DATA', str(Path(tempfile.gettempdir()) / 'stream-editor-tests'))
import app  # noqa: E402


class FakeHandler(app.Handler):
    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass

    def log_message(self, *args):
        pass


def request(method, path, body=None, raw=None):
    handler = object.__new__(FakeHandler)
    handler.path = path
    raw = raw if raw is not None else json.dumps(body).encode() if body is not None else b''
    handler.headers = {'Content-Length': str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    getattr(handler, method)()
    return handler.status, json.loads(handler.wfile.getvalue())


class AppApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        app.DATA = self.root
        app.CONFIG = self.root / 'project.json'
        self.source = self.root / 'stream.mp4'
        self.source.write_bytes(b'video fixture')
        self.project = {
            'source': str(self.source),
            'highlights': str(self.root / 'highlights.csv'),
            'transcript_dir': str(self.root / 'transcript'),
            'output_dir': str(self.root / 'output'),
            'audio_stream': 0,
            'backend': 'auto',
        }

    def test_save_project_and_highlights(self):
        status, _ = request('do_POST', '/api/project', self.project)
        self.assertEqual(status, 200)
        row = {'start': '00:00:02', 'end': '00:00:05', 'category': 'test',
               'score': '3', 'reason': 'sample', 'needs_video_review': 'true',
               'editor_note': 'keep'}
        status, _ = request('do_POST', '/api/highlights', {'rows': [row]})
        self.assertEqual(status, 200)
        status, state = request('do_GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['rows'][0]['reason'], 'sample')
        self.assertEqual(state['rows'][0]['editor_note'], 'keep')
        self.assertEqual(state['project']['source'], str(self.source.resolve()))
        self.assertEqual(state['highlight_stats']['count'], 1)
        self.assertEqual(state['highlight_stats']['duration_seconds'], 3)
        self.assertIn('vision_device', state)

    def test_invalid_interval_does_not_replace_csv(self):
        request('do_POST', '/api/project', self.project)
        path = Path(self.project['highlights'])
        path.write_text('original', encoding='utf-8')
        status, result = request('do_POST', '/api/highlights', {'rows': [
            {'start': '00:00:09', 'end': '00:00:02'}]})
        self.assertEqual(status, 400)
        self.assertIn('error', result)
        self.assertEqual(path.read_text(encoding='utf-8'), 'original')

    def test_cannot_edit_intervals_during_model_job(self):
        app.save_project(self.project)
        with mock.patch.object(app, 'job', {'status': 'running', 'kind': 'deep', 'error': ''}):
            status, result = request('do_POST', '/api/highlights', {'rows': [
                {'start': '00:00:01', 'end': '00:00:03'}]})
        self.assertEqual(status, 400)
        self.assertIn('Дождитесь', result['error'])

    def test_missing_source_is_rejected(self):
        self.project['source'] = str(self.root / 'missing.mp4')
        status, result = request('do_POST', '/api/project', self.project)
        self.assertEqual(status, 400)
        self.assertIn('error', result)

    def test_video_upload_sets_project_and_keeps_bytes(self):
        content = b'video fixture from browser'
        status, result = request('do_POST', '/api/upload?name=sample.mp4', raw=content)
        self.assertEqual(status, 200)
        path = Path(result['project']['source'])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(result['size_bytes'], len(content))
        self.assertIn('transcript_dir', result['project'])

    def test_upload_rejects_unsupported_extension(self):
        status, result = request('do_POST', '/api/upload?name=sample.txt', raw=b'content')
        self.assertEqual(status, 400)
        self.assertIn('error', result)
        self.assertFalse(list((self.root / 'uploads').glob('*')) if (self.root / 'uploads').exists() else False)

    def test_transcript_progress_metrics(self):
        work = Path(self.project['transcript_dir'])
        chunks = work / 'chunks'
        chunks.mkdir(parents=True)
        (work / 'progress.json').write_text('{"duration_seconds":3600,"total_chunks":2}')
        (chunks / 'chunk_0000.json').write_text('{"segments":[]}')
        metrics = app.transcript_metrics(self.project)
        self.assertEqual(metrics['completed_chunks'], 1)
        self.assertEqual(metrics['percent'], 50)

    def test_http_response_without_console_streams(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(app.sys, 'stderr', None):
                with urlopen(f'http://127.0.0.1:{server.server_port}/', timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b'<html', response.read().lower())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_startup_without_console_output(self):
        with mock.patch.object(app.sys, 'argv', ['app.py', '--no-browser', '--port', '0']), \
             mock.patch.object(app.sys, 'stdout', None), \
             mock.patch.object(app, 'ThreadingHTTPServer') as server:
            app.main()
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()

    def test_packaged_task_restores_log_streams(self):
        log = self.root / 'task.log'
        with mock.patch.dict(os.environ, {'STREAM_EDITOR_TASK_LOG': str(log)}), \
             mock.patch.object(app.sys, 'argv', ['StreamEditor.exe', '--task', 'highlights_to_fcp7xml.py']), \
             mock.patch.object(app.sys, 'stdout', None), \
             mock.patch.object(app.sys, 'stderr', None), \
             mock.patch('runpy.run_path') as run_path:
            app.main()
            self.assertIsNotNone(app.sys.stdout)
            self.assertIs(app.sys.stdout, app.sys.stderr)
            app.sys.stdout.write('task started\n')
            app.sys.stdout.close()
        run_path.assert_called_once()
        self.assertEqual(log.read_text(encoding='utf-8'), 'task started\n')

    def test_completed_job_reports_file_location(self):
        app.save_project(self.project)
        with mock.patch.object(app, 'job', {'status': 'done', 'kind': 'xml', 'error': ''}):
            state = app.job_state()
        self.assertEqual(state['output_path'], str(self.root / 'output' / 'highlights.xml'))

    def test_visual_report_is_invalidated_by_interval_edit(self):
        from deep_analysis import fingerprint
        app.save_project(self.project)
        row = {'start': '00:00:02', 'end': '00:00:05', 'category': 'game',
               'score': '4', 'reason': 'manual', 'needs_video_review': 'true', 'relevance': '83'}
        request('do_POST', '/api/highlights', {'rows': [row]})
        report_path = Path(self.project['highlights']).with_suffix('.visual.json')
        report_path.write_text(json.dumps({'identity': fingerprint(self.source, [row], 0),
                                           'items': [{'event': 4, 'start': row['start'], 'end': row['end']}]}))
        self.assertEqual(request('do_GET', '/api/state')[1]['visual_report']['items'][0]['event'], 4)
        row['reason'] = 'revised note'
        request('do_POST', '/api/highlights', {'rows': [row]})
        self.assertTrue(report_path.exists())
        row['end'] = '00:00:06'
        request('do_POST', '/api/highlights', {'rows': [row]})
        self.assertFalse(report_path.exists())

    def test_visual_frame_endpoint_only_serves_reported_frames(self):
        from deep_analysis import fingerprint
        app.save_project(self.project)
        row = {'start': '00:00:02', 'end': '00:00:05', 'score': '4'}
        request('do_POST', '/api/highlights', {'rows': [row]})
        frame_dir = Path(self.project['highlights']).with_suffix('.visual-frames')
        frame_dir.mkdir()
        content = b'\xff\xd8test\xff\xd9'
        (frame_dir / '000_00.jpg').write_bytes(content)
        Path(self.project['highlights']).with_suffix('.visual.json').write_text(json.dumps({
            'identity': fingerprint(self.source, [row], 0),
            'items': [{'frame_files': ['000_00.jpg'], 'start': row['start'], 'end': row['end']}]}))
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/visual-frame'
            with urlopen(base + '?index=0&frame=0') as response:
                self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                self.assertEqual(response.read(), content)
            with self.assertRaises(HTTPError) as invalid:
                urlopen(base + '?index=0&frame=1')
            self.assertEqual(invalid.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()

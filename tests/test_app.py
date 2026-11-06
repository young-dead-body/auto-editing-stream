"""Checks the local API contract without opening a network port."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('KIPOFOX_DATA', str(Path(tempfile.gettempdir()) / 'kipofox-tests'))
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


def request(method, path, body=None):
    handler = object.__new__(FakeHandler)
    handler.path = path
    raw = json.dumps(body).encode() if body is not None else b''
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
               'score': '3', 'reason': 'sample', 'needs_video_review': 'true'}
        status, _ = request('do_POST', '/api/highlights', {'rows': [row]})
        self.assertEqual(status, 200)
        status, state = request('do_GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['rows'][0]['reason'], 'sample')
        self.assertEqual(state['project']['source'], str(self.source.resolve()))

    def test_invalid_interval_does_not_replace_csv(self):
        request('do_POST', '/api/project', self.project)
        path = Path(self.project['highlights'])
        path.write_text('original', encoding='utf-8')
        status, result = request('do_POST', '/api/highlights', {'rows': [
            {'start': '00:00:09', 'end': '00:00:02'}]})
        self.assertEqual(status, 400)
        self.assertIn('error', result)
        self.assertEqual(path.read_text(encoding='utf-8'), 'original')

    def test_missing_source_is_rejected(self):
        self.project['source'] = str(self.root / 'missing.mp4')
        status, result = request('do_POST', '/api/project', self.project)
        self.assertEqual(status, 400)
        self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()

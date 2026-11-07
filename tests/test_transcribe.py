"""Regression checks for resumable transcription and lazy GPU failures."""
import json
from pathlib import Path
import tempfile
import unittest

import transcribe_stream


class TranscriptionTest(unittest.TestCase):
    def test_combines_every_completed_chunk_before_job_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            chunks = work / 'chunks'
            chunks.mkdir()
            for idx in (0, 2):
                (chunks / f'chunk_{idx:04d}.json').write_text(json.dumps({
                    'segments': [{'start': 1, 'end': 3, 'text': f'часть {idx}'}]}), encoding='utf-8')
            done, _ = transcribe_stream.combine_chunks(work, work / 'source.mp4', 3, 60)
            transcript = json.loads((work / 'transcript_timestamps.json').read_text(encoding='utf-8'))
            self.assertEqual(done, 2)
            self.assertFalse(transcript['complete'])
            self.assertEqual([item['start'] for item in transcript['segments']], [1, 121])
            self.assertEqual([item['chunk_index'] for item in transcript['segments']], [0, 2])

    def test_inference_error_while_consuming_segments_retries_whole_chunk_on_cpu(self):
        class LazyModel:
            def transcribe(self, *_args, **_kwargs):
                def segments():
                    yield type('Segment', (), {'start': 0, 'end': 1, 'text': 'начало'})()
                    raise RuntimeError('cublas64_12.dll is not found')
                return segments(), None

        class CpuModel:
            def transcribe(self, *_args, **_kwargs):
                return iter([type('Segment', (), {'start': 0, 'end': 2, 'text': 'полностью'})()]), None

        calls = []
        def factory(_name, *, device, compute_type):
            calls.append((device, compute_type))
            return LazyModel() if device == 'cuda' else CpuModel()

        model, device, segments = transcribe_stream.recognize_with_fallback(
            factory, 'base', None, 'cuda', 'float16', Path('audio.mp3'), 'ru', False)
        self.assertIsInstance(model, CpuModel)
        self.assertEqual(device, 'cpu')
        self.assertEqual(segments, [{'start': 0, 'end': 2, 'text': 'полностью'}])
        self.assertEqual(calls, [('cuda', 'float16'), ('cpu', 'int8')])


if __name__ == '__main__':
    unittest.main()

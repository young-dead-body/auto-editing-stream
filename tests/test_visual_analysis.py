"""Contracts for local visual ranking, persistence and hardware fallback."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deep_analysis
import visual_analysis


class VisualAnalysisTest(unittest.TestCase):
    def test_score_requires_visible_event_for_top_rank(self):
        self.assertEqual(deep_analysis.relevance_score(4, 4, 4), 100)
        self.assertEqual(deep_analysis.relevance_score(0, 4, 4), 49)
        self.assertGreater(deep_analysis.relevance_score(3, 4, 3),
                           deep_analysis.relevance_score(2, 1, 2))

    def test_checked_download_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'model.gguf'
            response = io.BytesIO(b'bad model')
            with mock.patch.object(visual_analysis, 'urlopen', return_value=response):
                with self.assertRaisesRegex(ValueError, 'контрольная сумма'):
                    visual_analysis.download_checked('https://example.invalid/model', destination, '0' * 64)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name('model.gguf.part').exists())

    def test_vision_model_selection(self):
        with mock.patch.object(visual_analysis.sys, 'platform', 'win32'), \
             mock.patch.object(visual_analysis.platform, 'machine', return_value='AMD64'):
            self.assertEqual(visual_analysis.vision_profile(16, 8), ('2.2b', 'gpu'))
            self.assertEqual(visual_analysis.vision_profile(8, 4), ('500m', 'gpu'))
            self.assertEqual(visual_analysis.vision_profile(5, 2), ('256m', 'cpu'))

    def test_deep_review_preserves_editor_fields_and_reuses_visual_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            source.write_bytes(b'synthetic source')
            csv_path = root / 'highlights.csv'
            csv_path.write_text('start,end,category,score,reason,needs_video_review,editor_note\n'
                                '00:00:02,00:00:08,ручной,4,сохранить причину,false,проверено\n', encoding='utf-8')
            visual = [{'event': 4, 'visible_reaction': 2, 'clarity': 3,
                       'event_type': 'win', 'evidence': 'score changes',
                       'uncertainty': '', 'low_confidence': True,
                       'frame_times': [1, 4], 'frame_files': ['000_00.jpg']}]
            with mock.patch.object(deep_analysis, 'audio_energy', return_value=[.01] * 10), \
                 mock.patch.object(deep_analysis, 'model_review', return_value=([(10, 2, 8, 'speech')], 'small')), \
                 mock.patch.object(deep_analysis, 'vision_profile', side_effect=[('500m', 'cpu'),
                                                                               ('500m', 'cpu'),
                                                                               ('2.2b', 'gpu')]), \
                 mock.patch.object(deep_analysis, 'review', side_effect=[(visual, '500m', 'cpu'),
                                                                        (visual, '2.2b', 'gpu')]) as review:
                report = deep_analysis.deep_analyze(source, csv_path, root / 'missing.json', root / 'models')
                deep_analysis.deep_analyze(source, csv_path, root / 'missing.json', root / 'models')
                upgraded = deep_analysis.deep_analyze(source, csv_path, root / 'missing.json', root / 'models')
            self.assertEqual(review.call_count, 2)
            self.assertEqual(upgraded['model'], '2.2b')
            with csv_path.open(encoding='utf-8', newline='') as file:
                row = next(csv.DictReader(file))
            self.assertEqual((row['category'], row['score'], row['reason'], row['needs_video_review']),
                             ('ручной', '4', 'сохранить причину', 'false'))
            self.assertEqual(row['editor_note'], 'проверено')
            self.assertEqual(row['relevance'], str(report['items'][0]['relevance']))
            self.assertLessEqual(int(row['relevance']), 49)
            self.assertEqual(report['visual_status'], 'complete')

    def test_invalid_model_response_is_rejected(self):
        server = visual_analysis.VisionServer('/tmp', '256m', 'cpu')
        server.port = 12345
        payload = {'choices': [{'message': {'content': '{"event": 9, "visible_reaction": 0, "clarity": 0}'}}]}
        response = io.BytesIO(json.dumps(payload).encode())
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / 'frame.jpg'
            frame.write_bytes(b'fake image')
            with mock.patch.object(visual_analysis, 'urlopen', return_value=response):
                with self.assertRaisesRegex(ValueError, 'Некорректная оценка'):
                    server.ask([(0, frame)], '')

    def test_gpu_start_failure_uses_cpu(self):
        class FakeServer:
            def __init__(self, cache, tier, kind):
                self.kind = kind

            def __enter__(self):
                if self.kind == 'gpu':
                    raise RuntimeError('GPU unavailable')
                return self

            def __exit__(self, *_):
                pass

            def ask(self, frames, transcript):
                return {'event': 2, 'visible_reaction': 1, 'clarity': 2,
                        'event_type': 'action', 'evidence': 'visible action in frame', 'uncertainty': ''}

        with mock.patch.object(visual_analysis, 'vision_profile', return_value=('2.2b', 'gpu')), \
             mock.patch.object(visual_analysis, 'vision_profile_ram', return_value=8), \
             mock.patch.object(visual_analysis, 'VisionServer', FakeServer), \
             mock.patch.object(visual_analysis, 'frame_times', return_value=[1]), \
             mock.patch.object(visual_analysis, 'extract_frames', return_value=[(1, Path('frame.jpg'))]):
            results, tier, kind = visual_analysis.review('source.mp4', [(0, 10)], [], [], '/cache')
        self.assertEqual((tier, kind), ('500m', 'cpu'))
        self.assertEqual(results[0]['event'], 2)


if __name__ == '__main__':
    unittest.main()

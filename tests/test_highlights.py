"""Exercise ranking and local task wiring without downloaded models."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
import sys
import types
from unittest import mock

import analyze_highlights
import app
import device_profile
import export_clips


class HighlightTest(unittest.TestCase):
    def test_audio_only_finds_spike(self):
        energy = [0.01] * 120
        energy[50] = 0.3
        picks = analyze_highlights.candidates([], energy, 120)
        self.assertTrue(picks)
        self.assertTrue(any(start <= 50 <= end for _, start, end, _ in picks))

    def test_speech_vector_and_transcript_candidate(self):
        self.assertLess(analyze_highlights.cosine(
            analyze_highlights.speech_vector('победа команды'),
            analyze_highlights.speech_vector('новая игра')), 0.1)
        segments = [{'start': 20, 'end': 35, 'text': 'Вау, невероятно! Победа!'}]
        picks = analyze_highlights.candidates(segments, [0.01] * 90, 90)
        self.assertTrue(picks)
        self.assertIn('эмоциональная речь', picks[0][3])

    def test_analysis_writes_csv_and_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / 'transcript.json'
            transcript.write_text(json.dumps({'complete': True, 'segments': [
                {'start': 20, 'end': 35, 'text': 'Вау! Победа!'}]}))
            output = root / 'moments.csv'
            probe = mock.Mock(stdout='90\n')
            with mock.patch.object(analyze_highlights.subprocess, 'run', return_value=probe), \
                 mock.patch.object(analyze_highlights, 'audio_energy', return_value=[.01] * 90):
                stats = analyze_highlights.analyze(root / 'source.mp4', transcript, output)
            self.assertGreater(stats['count'], 0)
            with output.open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), stats['count'])
            self.assertEqual(rows[0]['needs_video_review'], 'true')
            self.assertEqual(json.loads(output.with_suffix('.stats.json').read_text())['count'], stats['count'])

    def test_clip_export_calls_ffmpeg_per_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / 'moments.csv'
            listing.write_text('start,end\n00:00:02,00:00:05\n00:00:10,00:00:12\n')
            with mock.patch.object(export_clips.subprocess, 'run') as run:
                self.assertEqual(export_clips.export(root / 'source.mp4', listing, root / 'clips'), 2)
            self.assertEqual(run.call_count, 2)
            self.assertIn('clip_001.mp4', str(run.call_args_list[0]))

    def test_cpu_profile_when_gpu_unavailable(self):
        with mock.patch.object(device_profile, 'available_memory_gb', return_value=3), \
             mock.patch.object(device_profile, 'cuda_memory_gb', return_value=None):
            profile = device_profile.profile()
        self.assertEqual((profile['device'], profile['model'], profile['compute_type']),
                         ('cpu', 'base', 'int8'))

    def test_deep_review_uses_local_model_on_cpu(self):
        model = mock.Mock()
        model.transcribe.return_value = (iter([types.SimpleNamespace(text='Вау! Это победа!')]), None)
        fake = types.SimpleNamespace(WhisperModel=mock.Mock(return_value=model))
        hardware = {'device': 'cpu', 'backend': 'faster-whisper', 'model': 'base', 'compute_type': 'int8'}
        with mock.patch.dict(sys.modules, {'faster_whisper': fake}), \
             mock.patch.object(analyze_highlights, 'profile', return_value=hardware), \
             mock.patch.object(analyze_highlights.subprocess, 'run'):
            reviewed, name = analyze_highlights.model_review('source.mp4',
                                                               [(3, 10, 25, 'всплеск громкости')], 0)
        self.assertEqual(name, 'base')
        fake.WhisperModel.assert_called_once_with('base', device='cpu', compute_type='int8')
        self.assertGreater(reviewed[0][0], 3)
        self.assertIn('модель подтвердила речь', reviewed[0][3])

    def test_app_commands_for_new_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = {'source': str(root / 'source.mp4'), 'transcript_dir': str(root / 'transcript'),
                       'highlights': str(root / 'highlights.csv'), 'output_dir': str(root / 'output'),
                       'audio_stream': 0, 'backend': 'auto'}
            self.assertEqual(app.command_for('analyze', project)[0], 'analyze_highlights.py')
            Path(project['highlights']).write_text('start,end\n00:00:01,00:00:02\n')
            self.assertEqual(app.command_for('clips', project)[0], 'export_clips.py')


if __name__ == '__main__':
    unittest.main()

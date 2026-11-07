"""Optional real-model smoke test for release runners (downloads ~280 MB once)."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_analysis import VisionServer, extract_frames, frame_times


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    video = root / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10', '-t', '3',
                    '-c:v', 'mpeg4', str(video)], check=True)
    times = frame_times(video, 0, 3, [])
    frames = extract_frames(video, 0, times, root)
    assert frames, 'FFmpeg did not extract any frame'
    cache = Path(os.environ.get('STREAM_EDITOR_VISUAL_CACHE', root / 'cache'))
    with VisionServer(cache, '256m', 'cpu') as model:
        result = model.ask(frames, '')
    assert all(0 <= result[field] <= 4 for field in ('event', 'visible_reaction', 'clarity'))
    print('Visual smoke passed:', result)

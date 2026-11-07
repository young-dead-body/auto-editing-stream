"""Local visual review of shortlisted clips with SmolVLM2 and llama.cpp.

The model and runtime are downloaded only after a user starts deep analysis.  All
remote artifacts are revision pinned and SHA-256 checked before use.  This module
uses only the Python standard library so the desktop executable stays small.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile
import tarfile

MODEL_FILES = {
    '256m': ('ggml-org/SmolVLM2-256M-Video-Instruct-GGUF', 'a8383113a70b406f088ac936e5f1918ed08e620d',
             ('SmolVLM2-256M-Video-Instruct-Q8_0.gguf', '1202d1c54493bddff5b0ecbc36fcb7520ff720b9fa3d7224aeb293581f90529a'),
             ('mmproj-SmolVLM2-256M-Video-Instruct-Q8_0.gguf', '05d5751132244a6ebd64cba9b34898c0d874b2cb78159d758e1d4da3aad91581')),
    '500m': ('ggml-org/SmolVLM2-500M-Video-Instruct-GGUF', 'ccd7aae53bcb1997355c2f094959e72b3642ce17',
             ('SmolVLM2-500M-Video-Instruct-Q8_0.gguf', '6f67b8036b2469fcd71728702720c6b51aebd759b78137a8120733b4d66438bc'),
             ('mmproj-SmolVLM2-500M-Video-Instruct-Q8_0.gguf', '921dc7e259f308e5b027111fa185efcbf33db13f6e35749ddf7f5cdb60ef520b')),
    '2.2b': ('ggml-org/SmolVLM2-2.2B-Instruct-GGUF', '1bc3c9f74ceafd4c8d4411cc9cf188bba3798f91',
             ('SmolVLM2-2.2B-Instruct-Q4_K_M.gguf', '0cf76814555b8665149075b74ab6b5c1d428ea1d3d01c1918c12012e8d7c9f58'),
             ('mmproj-SmolVLM2-2.2B-Instruct-Q8_0.gguf', 'ae07ea1facd07dd3230c4483b63e8cda96c6944ad2481f33d531f79e892dd024')),
}
RUNTIME_TAG = 'b12186'
# Release digests from GitHub's immutable b12186 asset metadata.
RUNTIMES = {
    ('win32', 'AMD64', 'cpu'): ('llama-b12186-bin-win-cpu-x64.zip', '2353a275b364552c7bbe739d3391918b8762506f02bd56751ce568ac13b24c0f'),
    ('win32', 'AMD64', 'gpu'): ('llama-b12186-bin-win-vulkan-x64.zip', '38080467ded8a7b40ab82aee6d12727241efd909a3406cb223a3e385ee39263c'),
    ('linux', 'x86_64', 'cpu'): ('llama-b12186-bin-ubuntu-x64.tar.gz', '848b4dc38eee728d9d16c3700a1e643b4f06e5979e1b80613c064f090e989ae8'),
    ('linux', 'x86_64', 'gpu'): ('llama-b12186-bin-ubuntu-vulkan-x64.tar.gz', '616878139c6a1b0c16baf09ce418cd05b8fbcdf7b3ceea193c0e66c7c1d35155'),
    ('linux', 'aarch64', 'cpu'): ('llama-b12186-bin-ubuntu-arm64.tar.gz', 'a97ddf62dfd507e593d3de90aeeb3f0d36aa17f980a8065d3a6e3fca35e4db33'),
    ('darwin', 'arm64', 'gpu'): ('llama-b12186-bin-macos-arm64.tar.gz', '1055e0afaf01ef86eeb1a9bcca2fa237aeda5a5cd5ada3f98c9b95d911e627bd'),
    ('darwin', 'x86_64', 'cpu'): ('llama-b12186-bin-macos-x64.tar.gz', '0a4cceccaa7eb6b24b653bb5af96076b8d886216951d105ec2d039ff2c305e2e'),
}
PROMPT_VERSION = 2
_AUTO = object()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        while block := file.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download_checked(url, destination, expected):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256(destination) == expected:
        return destination
    temporary = destination.with_name(destination.name + '.part')
    try:
        with urlopen(Request(url, headers={'User-Agent': 'StreamEditor/visual-analysis'}), timeout=60) as response, temporary.open('wb') as out:
            total = int(getattr(response, 'headers', {}).get('Content-Length', '0'))
            written = 0
            next_report = 25 * 1024 * 1024
            print(f'Загрузка {destination.name} ({round(total / 1024**2)} МиБ)', flush=True)
            while block := response.read(1024 * 1024):
                out.write(block)
                written += len(block)
                if written >= next_report:
                    print(f'Загружено {destination.name}: {round(written / 1024**2)} МиБ', flush=True)
                    next_report += 25 * 1024 * 1024
        if sha256(temporary) != expected:
            raise ValueError(f'Неверная контрольная сумма: {destination.name}')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def model_paths(cache, tier):
    repo, revision, model, projector = MODEL_FILES[tier]
    target = Path(cache) / 'models' / tier
    paths = []
    for name, digest in (model, projector):
        url = f'https://huggingface.co/{repo}/resolve/{revision}/{name}'
        paths.append(download_checked(url, target / name, digest))
    return paths


def runtime_key(kind):
    machine = platform.machine()
    machine = {'x86_64': 'AMD64', 'amd64': 'AMD64'}.get(machine.lower(), machine) if sys.platform == 'win32' else machine
    if sys.platform == 'darwin' and machine == 'arm64':
        kind = 'gpu'  # Apple Silicon release uses Metal.
    return (sys.platform, machine, kind)


def _safe_members(members):
    for member in members:
        name = member.filename if hasattr(member, 'filename') else member.name
        parts = Path(name.replace('\\', '/')).parts
        if not name or name.startswith(('/', '\\')) or '..' in parts or (parts and ':' in parts[0]):
            raise ValueError(f'Небезопасный путь в архиве: {name}')
        yield member


def runtime_path(cache, kind):
    key = runtime_key(kind)
    if key not in RUNTIMES:
        raise RuntimeError(f'Нет сборки llama.cpp для {key[0]} / {key[1]} / {kind}')
    filename, digest = RUNTIMES[key]
    target = Path(cache) / 'runtime' / f'{RUNTIME_TAG}-{kind}'
    executable = target / ('llama-server.exe' if sys.platform == 'win32' else 'llama-server')
    marker = target / '.verified-v2'
    if executable.is_file() and marker.is_file() and marker.read_text().strip() == digest:
        return executable
    archive = download_checked(f'https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_TAG}/{filename}',
                               Path(cache) / 'downloads' / filename, digest)
    target.mkdir(parents=True, exist_ok=True)
    if filename.endswith('.zip'):
        with zipfile.ZipFile(archive) as bundle:
            for member in _safe_members(bundle.infolist()):
                if member.is_dir():
                    continue
                destination = target / Path(member.filename).name
                with bundle.open(member) as src, destination.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, 'r:gz') as bundle:
            links = []
            for member in _safe_members(bundle.getmembers()):
                if member.issym():
                    links.append(member)
                    continue
                if not member.isfile():
                    continue
                destination = target / Path(member.name).name
                with bundle.extractfile(member) as src, destination.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            pending = links
            while pending:
                remaining = []
                for member in pending:
                    link_target = Path(member.linkname).name
                    if not link_target or link_target != member.linkname:
                        raise ValueError(f'Небезопасная ссылка в архиве: {member.name}')
                    if not (target / link_target).is_file():
                        remaining.append(member)
                        continue
                    destination = target / Path(member.name).name
                    destination.unlink(missing_ok=True)
                    destination.symlink_to(link_target)
                if len(remaining) == len(pending):
                    raise ValueError('Архив llama.cpp содержит повреждённые ссылки')
                pending = remaining
    if not executable.is_file():
        raise RuntimeError('Архив llama.cpp не содержит llama-server')
    if sys.platform != 'win32':
        executable.chmod(0o755)
        for item in target.iterdir():
            if item.suffix in ('.so', '.dylib') or '.so.' in item.name:
                item.chmod(0o755)
    marker.write_text(digest, encoding='ascii')
    return executable


def vision_profile(available_ram_gb=_AUTO, vram_gb=_AUTO):
    """Choose quality tier; actual GPU launch is tried before CPU fallback."""
    from device_profile import available_memory_gb, cuda_memory_gb
    ram = available_memory_gb() if available_ram_gb is _AUTO else available_ram_gb
    vram = cuda_memory_gb() if vram_gb is _AUTO else vram_gb
    linux_render_device = sys.platform == 'linux' and any(Path('/dev/dri').glob('renderD*'))
    gpu_possible = ((sys.platform == 'darwin' and platform.machine() == 'arm64') or
                    (sys.platform == 'win32' and runtime_key('gpu') in RUNTIMES and
                     (vram is None or vram >= 4)) or
                    (linux_render_device and runtime_key('gpu') in RUNTIMES and
                     (vram is None or vram >= 4)))
    if (gpu_possible and vram is not None and vram >= 6) or (
            sys.platform == 'darwin' and platform.machine() == 'arm64' and (ram or 0) >= 12):
        return '2.2b', 'gpu'
    if gpu_possible:
        return '500m', 'gpu'
    if ram is not None and ram < 4:
        raise RuntimeError('Для видеомодели требуется не менее 4 ГБ свободной RAM')
    return ('500m' if ram is None or ram >= 6 else '256m'), 'cpu'


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class VisionServer:
    def __init__(self, cache, tier, kind):
        self.cache, self.tier, self.kind = Path(cache), tier, kind
        self.process = None
        self.port = None
        self.log = None

    def __enter__(self):
        model, projector = model_paths(self.cache, self.tier)
        executable = runtime_path(self.cache, self.kind)
        self.port = _free_port()
        self.log = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
        command = [str(executable), '--host', '127.0.0.1', '--port', str(self.port),
                   '-m', str(model), '--mmproj', str(projector), '-c', '8192',
                   '-ngl', '99' if self.kind == 'gpu' else '0']
        environment = os.environ.copy()
        library_path = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
        environment[library_path] = str(executable.parent) + os.pathsep + environment.get(library_path, '')
        try:
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT,
                                            cwd=str(executable.parent), env=environment,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            self.log.close()
            self.log = None
            raise
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError('llama-server не запустился: ' + self.log.read()[-2000:])
                try:
                    with urlopen(f'http://127.0.0.1:{self.port}/health', timeout=2) as response:
                        if response.status == 200:
                            return self
                except OSError:
                    pass
                time.sleep(0.5)
            raise TimeoutError('Превышено время загрузки видеомодели')
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()

    def ask(self, frames, transcript):
        prompt = ('Analyze chronological frames from a video that may or may not contain gameplay. '
                  'Judge only specific visible changes. A test pattern, menu, static screen or ambiguous '
                  'animation has event=0. Never invent a win, kill or score change. '
                  'Return ONLY JSON with integer scores 0..4: event, visible_reaction, clarity; '
                  'and strings event_type, evidence, uncertainty. Evidence must name concrete visible details, '
                  'not repeat event_type. '
                  'event=0 means no clear game event; visible_reaction=0 if no face reaction is visible. '
                  'Do not infer unseen actions. Transcript context: ' + transcript[:900])
        content = [{'type': 'text', 'text': prompt}]
        for timestamp, path in frames:
            data = base64.b64encode(Path(path).read_bytes()).decode('ascii')
            content.extend([{'type': 'text', 'text': f'Frame at {timestamp:.1f} seconds from clip start'},
                            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + data}}])
        body = json.dumps({'model': 'local', 'messages': [{'role': 'user', 'content': content}],
                           'temperature': 0, 'max_tokens': 256, 'stream': False,
                           'response_format': {'type': 'json_object', 'schema': {
                               'type': 'object',
                               'properties': {
                                   'event': {'type': 'integer', 'minimum': 0, 'maximum': 4},
                                   'visible_reaction': {'type': 'integer', 'minimum': 0, 'maximum': 4},
                                   'clarity': {'type': 'integer', 'minimum': 0, 'maximum': 4},
                                   'event_type': {'type': 'string', 'maxLength': 40},
                                   'evidence': {'type': 'string', 'maxLength': 120},
                                   'uncertainty': {'type': 'string', 'maxLength': 80}},
                               'required': ['event', 'visible_reaction', 'clarity', 'event_type',
                                            'evidence', 'uncertainty'],
                               'additionalProperties': False}}}).encode('utf-8')
        request = Request(f'http://127.0.0.1:{self.port}/v1/chat/completions', data=body,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=180) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise ValueError(f'Ошибка видеомодели HTTP {exc.code}: {exc.read(1000).decode("utf-8", "replace")}') from exc
        raw = result['choices'][0]['message']['content']
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            raise ValueError('Видеомодель не вернула JSON: ' + str(raw)[:500])
        value = json.loads(match.group())
        for key in ('event', 'visible_reaction', 'clarity'):
            if type(value.get(key)) is not int or not 0 <= value[key] <= 4:
                raise ValueError(f'Некорректная оценка видеомодели: {key}={value.get(key)!r}; ответ={raw[:500]}')
        for key in ('event_type', 'evidence', 'uncertainty'):
            value[key] = str(value.get(key, ''))[:500]
        return value


def frame_times(source, start, end, energies):
    """Choose context, audio peaks and scene-change frames from a 1 fps gray scan."""
    length = end - start
    if length <= 0:
        return []
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-ss', str(start),
               '-i', str(source), '-t', str(length), '-vf', 'fps=1,scale=160:90,format=gray',
               '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    data = subprocess.run(command, check=True, capture_output=True).stdout
    size = 160 * 90
    frames = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
    changes = [(sum(abs(a - b) for a, b in zip(frames[i], frames[i - 1])) / size, i)
               for i in range(1, len(frames))]
    suggestions = [length * ratio for ratio in (.08, .25, .5, .75, .92)]
    suggestions += [float(i) for _, i in sorted(changes, reverse=True)[:3]]
    if energies:
        local = [(energies[i], i - start) for i in range(max(0, int(start)), min(len(energies), int(end)))]
        suggestions += [float(offset) for _, offset in sorted(local, reverse=True)[:2]]
    chosen = []
    for point in suggestions:
        point = max(0, min(length - .1, point))
        if all(abs(point - existing) >= 1 for existing in chosen):
            chosen.append(point)
        if len(chosen) == 8:
            break
    return sorted(chosen)


def extract_frames(source, start, times, directory):
    frames = []
    for index, timestamp in enumerate(times):
        path = Path(directory) / f'{index:02d}.jpg'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-ss', str(start + timestamp), '-i', str(source), '-frames:v', '1',
                        '-vf', 'scale=512:-2', '-q:v', '4', str(path)], check=True)
        if path.is_file() and path.stat().st_size:
            frames.append((timestamp, path))
    return frames


def review(source, intervals, energies, transcript, cache, progress=None, frames_dir=None):
    """Return per-interval model output; preserve partial results on individual failures."""
    tier, kind = vision_profile()
    def run_with(tier, kind):
        with VisionServer(cache, tier, kind) as model:
            results = []
            for index, (start, end) in enumerate(intervals, 1):
                try:
                    times = frame_times(source, start, end, energies)
                    with tempfile.TemporaryDirectory() as directory:
                        frames = extract_frames(source, start, times, directory)
                        if not frames:
                            raise ValueError('Не удалось извлечь кадры')
                        text = ' '.join(str(s.get('text', '')) for s in transcript
                                        if float(s.get('end', 0)) > start and float(s.get('start', 0)) < end)
                        value = model.ask(frames, text)
                        value['low_confidence'] = (tier == '256m' or
                                                   len(re.findall(r'[\w]+', value['evidence'])) < 3)
                        if frames_dir is not None:
                            target = Path(frames_dir)
                            target.mkdir(parents=True, exist_ok=True)
                            value['frame_files'] = []
                            for frame_index, (_, path) in enumerate(frames):
                                name = f'{index - 1:03d}_{frame_index:02d}.jpg'
                                shutil.copy2(path, target / name)
                                value['frame_files'].append(name)
                    value['frame_times'] = [round(t, 2) for t, _ in frames]
                    results.append(value)
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                    results.append({'error': str(exc)})
                if progress:
                    progress(index, len(intervals), tier)
            return results
    try:
        return run_with(tier, kind), tier, kind
    except (OSError, RuntimeError, TimeoutError) as exc:
        if kind != 'gpu':
            raise
        print(f'GPU-видеомодель недоступна: {exc}; переход на CPU', flush=True)
        ram = vision_profile_ram()
        if ram is not None and ram < 4:
            raise RuntimeError('Видеомодель не запускается на GPU; для CPU нужно не менее 4 ГБ RAM') from exc
        cpu_tier = '500m' if ram is None or ram >= 6 else '256m'
        return run_with(cpu_tier, 'cpu'), cpu_tier, 'cpu'


def vision_profile_ram():
    from device_profile import available_memory_gb
    return available_memory_gb()

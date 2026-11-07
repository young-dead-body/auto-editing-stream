"""Choose a practical local Whisper configuration for the current device."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


def available_memory_gb():
    if sys.platform == 'win32':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        ('total', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                        ('page_total', ctypes.c_ulonglong), ('page_available', ctypes.c_ulonglong),
                        ('virtual_total', ctypes.c_ulonglong), ('virtual_available', ctypes.c_ulonglong),
                        ('extended', ctypes.c_ulonglong)]
        state = Memory()
        state.length = ctypes.sizeof(state)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            return round(state.available / 1024**3, 1)
    if sys.platform == 'linux':
        try:
            values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            available = int(values['MemAvailable'].strip().split()[0]) * 1024
            cgroup = Path('/sys/fs/cgroup')
            try:
                limit = (cgroup / 'memory.max').read_text().strip()
                if limit != 'max':
                    available = min(available, max(0, int(limit) - int((cgroup / 'memory.current').read_text().strip())))
            except (OSError, ValueError):
                pass
            return round(available / 1024**3, 1)
        except (OSError, KeyError, ValueError):
            pass
    if sys.platform == 'darwin':
        try:
            data = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=3, check=True).stdout
            page_size = int(re.search(r'page size of (\d+) bytes', data).group(1))
            pages = dict((key, int(value.replace('.', ''))) for key, value in
                         re.findall(r'^Pages (free|inactive|speculative):\s*([\d.]+)', data, re.M))
            return round(sum(pages.values()) * page_size / 1024**3, 1)
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    try:
        return round(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / 1024**3, 1)
    except (AttributeError, ValueError, OSError):
        return None


def cuda_memory_gb():
    executable = shutil.which('nvidia-smi')
    if not executable and sys.platform == 'win32':
        candidate = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NVIDIA Corporation' / 'NVSMI' / 'nvidia-smi.exe'
        executable = str(candidate) if candidate.is_file() else None
    if not executable:
        return None
    try:
        result = subprocess.run([executable, '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=3, check=True)
        return round(max(int(line.strip()) for line in result.stdout.splitlines()) / 1024, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def profile():
    ram = available_memory_gb()
    vram = cuda_memory_gb()
    mlx = sys.platform == 'darwin' and platform.machine() == 'arm64' and shutil.which('mlx_whisper') is not None
    cuda = False
    if vram is not None and vram >= 2:
        try:
            import ctranslate2
            cuda = bool(ctranslate2.get_supported_compute_types('cuda'))
        except (ImportError, RuntimeError, OSError):
            pass
    if mlx:
        return {'device': 'apple-silicon', 'backend': 'mlx', 'model': 'mlx-community/whisper-large-v3-turbo',
                'compute_type': 'auto', 'ram_gb': ram, 'vram_gb': vram}
    if cuda:
        model = 'large-v3-turbo' if vram >= 6 else 'small'
        return {'device': 'cuda', 'backend': 'faster-whisper', 'model': model,
                'compute_type': 'float16' if vram >= 6 else 'int8_float16', 'ram_gb': ram, 'vram_gb': vram}
    model = 'medium' if ram is not None and ram >= 12 else 'small' if ram is None or ram >= 4 else 'base'
    return {'device': 'cpu', 'backend': 'faster-whisper', 'model': model,
            'compute_type': 'int8', 'ram_gb': ram, 'vram_gb': vram}

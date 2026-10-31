"""Optional offline verification using an already installed FFmpeg."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from bdve_engine import CancelledError

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def find_ffmpeg():
    candidates = []
    capcut = Path(os.environ.get('LOCALAPPDATA', '')) / 'CapCut' / 'Apps'
    if capcut.is_dir():
        def version_key(path):
            return tuple(int(part) if part.isdigit() else 0 for part in path.name.split('.'))
        candidates.extend(p / 'ffmpeg.exe' for p in sorted(capcut.iterdir(), key=version_key, reverse=True) if p.is_dir())
    found = shutil.which('ffmpeg')
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            check = subprocess.run([str(candidate), '-version'], capture_output=True,
                                   timeout=5, creationflags=NO_WINDOW)
            if check.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def verify_playback(path, executable, cancel=None):
    if cancel and cancel.is_set():
        raise CancelledError('Đã dừng.')
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([
            str(executable), '-v', 'error', '-xerror', '-i', str(path),
            '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-',
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
           creationflags=NO_WINDOW)
        try:
            while process.poll() is None:
                if cancel and cancel.wait(.1):
                    raise CancelledError('Đã dừng kiểm tra phát.')
                if cancel is None:
                    import time
                    time.sleep(.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        errors.seek(0)
        detail = errors.read(16000).decode('utf-8', errors='replace').strip()
        if process.returncode != 0 or detail:
            raise RuntimeError('Không vượt qua kiểm tra phát hình/tiếng. ' + detail[:1500])
    return {'full_decode': True, 'exit_code': 0}

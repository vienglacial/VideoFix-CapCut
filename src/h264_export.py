"""Optional extra H.264 copy of a repaired video, made only when the user asks.

The repaired ``_FIXED.mp4`` stays untouched. The copy is re-encoded with an
already installed FFmpeg (usually CapCut's), audio is copied, and the result
is fully decoded before it is given its final name.
"""
from pathlib import Path
import os
import subprocess
import tempfile

from bdve_engine import CancelledError
from playback_check import NO_WINDOW, verify_playback

FOLDER_NAME = 'H264_compatible'

# Tried in order; CapCut's FFmpeg ships hardware encoders and MediaFoundation, not libx264.
ENCODERS = [
    ('h264_nvenc', ['-preset', 'p6', '-tune', 'hq', '-rc', 'vbr', '-cq', '16', '-b:v', '0', '-pix_fmt', 'yuv420p']),
    ('h264_qsv', ['-preset', 'slow', '-global_quality', '18', '-pix_fmt', 'nv12']),
    ('h264_amf', ['-quality', 'quality', '-rc', 'cqp', '-qp_i', '16', '-qp_p', '18', '-qp_b', '20',
                  '-pix_fmt', 'yuv420p']),
    ('h264_mf', ['-rate_control', 'quality', '-quality', '90', '-pix_fmt', 'nv12']),
    ('libx264', ['-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p']),
]


def h264_destination(fixed: Path) -> Path:
    return fixed.parent / FOLDER_NAME / (fixed.stem + '_H264.mp4')


def available_encoders(executable):
    try:
        listing = subprocess.run([str(executable), '-hide_banner', '-encoders'], capture_output=True,
                                 text=True, errors='replace', timeout=10, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = {line.split()[1] for line in listing.splitlines() if len(line.split()) > 1}
    return [entry for entry in ENCODERS if entry[0] in names]


def _encode(executable, source, temp, encoder, options, cancel):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([
            str(executable), '-v', 'error', '-y', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a?', '-c:v', encoder, *options,
            '-c:a', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(temp),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, creationflags=NO_WINDOW)
        try:
            while process.poll() is None:
                if cancel is not None and cancel.wait(.1):
                    raise CancelledError('Đã dừng tạo bản H264.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        errors.seek(0)
        detail = errors.read(4000).decode('utf-8', errors='replace').strip()
    if process.returncode != 0:
        raise RuntimeError(f'{encoder}: {detail[:600] or process.returncode}')


def make_h264_copy(fixed: Path, executable, cancel=None) -> dict:
    """Create ``H264_compatible/<name>_H264.mp4`` next to ``fixed``.

    Returns ``{"status": "created" | "skipped_existing", "path", "encoder"}``;
    raises on failure and never leaves a partial file behind.
    """
    destination = h264_destination(fixed)
    if destination.exists():
        return {'status': 'skipped_existing', 'path': str(destination)}
    encoders = available_encoders(executable)
    if not encoders:
        raise RuntimeError('FFmpeg trên máy không có bộ nén H264.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + '.part.mp4')
    failures = []
    try:
        for encoder, options in encoders:
            try:
                _encode(executable, fixed, temp, encoder, options, cancel)
                verify_playback(temp, executable, cancel)
            except CancelledError:
                raise
            except RuntimeError as error:
                failures.append(str(error))
                continue
            os.replace(temp, destination)
            return {'status': 'created', 'path': str(destination), 'encoder': encoder}
        raise RuntimeError('Không tạo được bản H264. ' + ' | '.join(failures)[:1500])
    finally:
        if temp.exists():
            temp.unlink()
        try:
            destination.parent.rmdir()  # only succeeds when nothing was written
        except OSError:
            pass

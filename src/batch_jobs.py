"""Safe input discovery and output planning for VideoFix."""
from dataclasses import dataclass
from pathlib import Path
import os
import hashlib

OUTPUT_NAME = '_PHUC_HOI_CAPCUT'
VIDEO_SUFFIXES = {'.mp4', '.mov', '.m4v', '.mkv', '.tmp'}
EXCLUDED_NAMES = {OUTPUT_NAME.casefold(), '_fixed', 'h264_compatible', '__pycache__'}


@dataclass(frozen=True)
class Job:
    source: Path
    destination: Path
    output_root: Path


def collect_jobs(paths, recursive=True, output_dir=None, cancel=None):
    jobs, problems = [], []
    seen, destinations = set(), set()
    explicit_output = Path(output_dir).resolve() if output_dir else None
    inputs = [Path(p).expanduser().resolve() for p in paths]

    def stopped():
        return cancel is not None and cancel.is_set()

    def add(source, root):
        if stopped():
            return
        key = os.path.normcase(str(source))
        if key in seen:
            return
        seen.add(key)
        output_root = explicit_output or root / OUTPUT_NAME
        if explicit_output:
            root_key = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()[:12]
            output_root = output_root / (root.name + '-' + root_key)
        relative = source.relative_to(root)
        destination = output_root / relative.parent / (source.name + '_FIXED.mp4')
        target_key = os.path.normcase(str(destination))
        suffix = 2
        while target_key in destinations:
            destination = destination.with_name(source.name + f'_FIXED_{suffix}.mp4')
            target_key = os.path.normcase(str(destination))
            suffix += 1
        destinations.add(target_key)
        jobs.append(Job(source, destination, output_root))

    for item in inputs:
        if stopped():
            break
        if item.is_file():
            add(item, item.parent)
        elif item.is_dir():
            for current, directories, filenames in os.walk(item, followlinks=False, onerror=lambda e: problems.append(str(e))):
                if stopped():
                    break
                base = Path(current)
                directories[:] = sorted(
                    (name for name in directories
                     if name.casefold() not in EXCLUDED_NAMES
                     and not (base / name).is_symlink()
                     and not getattr(base / name, 'is_junction', lambda: False)()
                     and not (explicit_output and (base / name).resolve() == explicit_output)),
                    key=str.casefold,
                ) if recursive else []
                for name in sorted(filenames, key=str.casefold):
                    source = base / name
                    if source.suffix.casefold() in VIDEO_SUFFIXES and not source.is_symlink():
                        add(source, item)
        else:
            problems.append(f'Không tìm thấy: {item}')
    return jobs, problems

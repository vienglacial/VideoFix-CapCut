"""Read-only duplicate discovery for VideoFix.

Exact matches use full-file SHA-256 after a size prefilter. Media matches use
every compressed MP4 ``mdat`` byte plus codec configuration, track geometry,
sample/timing/edit tables and the other playback structure in ``moov``.
Only metadata and creation/modification dates are omitted. Chunk offsets are
normalized relative to compressed payload positions, not discarded.
This deliberately does not detect visually similar videos or re-encodes.

The caller collects files/folders and excludes output directories. This module
opens only supplied regular files, rejects symlink/junction traversal, and
never creates, changes or deletes any input or output file. Deletion of
confirmed duplicates is a separate, user-driven step in ``duplicate_delete``.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import mmap
import os
from pathlib import Path
import stat
import struct
import threading
from typing import Callable

import bdve_engine


CHUNK_SIZE = 4 * 1024 * 1024
MAX_MOOV_SIZE = 64 * 1024 * 1024
MAX_BOXES = 1_000_000
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex"}
_METADATA = {b"udta", b"meta", b"uuid", b"free", b"skip"}


class _Cancelled(Exception):
    pass


class _MediaUnsupported(ValueError):
    pass


class _Changed(OSError):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _safe_call(callback, argument):
    if callback is not None:
        try:
            callback(argument)
        except Exception:
            # UI reporting must not turn one scan into an aborted batch.
            pass


def _safe_input(path):
    candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    for component in (candidate, *candidate.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise OSError("Symlink/junction inputs are not followed.")
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Expected a supplied regular file, not a directory or device.")
    return candidate, info


class _View:
    """Bounded, optionally XOR-normalized read-only view of one open file."""
    def __init__(self, path, initial, profile=None):
        self.path, self.initial, self.profile = path, initial, profile
        self.handle = None
        self.data = None
        self.size = initial.st_size
        if profile:
            self.size = int(profile["payload_end"])
            params = profile["profile"]
            self.step, self.length, self.key = (int(params[k]) for k in ("step", "length", "key"))
            if not (0 < self.length <= self.step and 0 <= self.key <= 255 and 0 < self.size <= initial.st_size):
                raise _MediaUnsupported("Invalid normalized BDVE profile.")
            self.table = bytes(value ^ self.key for value in range(256))

    def __enter__(self):
        self.handle = self.path.open("rb")
        try:
            if _identity(os.fstat(self.handle.fileno())) != _identity(self.initial):
                raise _Changed("The file changed before hashing.")
            if self.initial.st_size:
                self.data = mmap.mmap(self.handle.fileno(), 0, access=mmap.ACCESS_READ)
            return self
        except BaseException:
            self.handle.close()
            raise

    def read(self, offset, count):
        if offset < 0 or count < 0 or offset + count > self.size:
            raise _MediaUnsupported("MP4 byte range is outside the file.")
        if not count:
            return b""
        block = self.data[offset:offset + count]
        if not self.profile:
            return block
        result = bytearray(block)
        cursor = 0
        while cursor < count:
            remainder = (offset + cursor) % self.step
            if remainder < self.length:
                extent = min(self.length - remainder, count - cursor)
                result[cursor:cursor + extent] = result[cursor:cursor + extent].translate(self.table)
            else:
                extent = min(self.step - remainder, count - cursor)
            cursor += extent
        return bytes(result)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            changed = _identity(os.fstat(self.handle.fileno())) != _identity(self.initial)
        finally:
            if self.data is not None:
                self.data.close()
            self.handle.close()
        if changed and exc_type is not _Cancelled:
            raise _Changed("The file changed while hashing; its result was discarded.")


def _file_hash(record, progress, cancel):
    digest = hashlib.sha256()
    with _View(record["path_obj"], record["stat"]) as view:
        for position in range(0, view.size, CHUNK_SIZE):
            _check_cancel(cancel)
            data = view.read(position, min(CHUNK_SIZE, view.size - position))
            digest.update(data)
            progress(len(data))
        _check_cancel(cancel)
    return digest.hexdigest()


def _boxes(read, start, end, cancel):
    cursor, count = start, 0
    while cursor < end:
        _check_cancel(cancel)
        count += 1
        if count > MAX_BOXES or cursor + 8 > end:
            raise _MediaUnsupported("Too many MP4 boxes or a truncated box header.")
        size, kind = struct.unpack(">I4s", read(cursor, 8))
        header = 8
        if size == 1:
            if cursor + 16 > end:
                raise _MediaUnsupported("Truncated extended MP4 header.")
            size = struct.unpack(">Q", read(cursor + 8, 8))[0]
            header = 16
        elif size == 0:
            size = end - cursor
        if size < header or size > end - cursor or not all(32 <= b <= 126 for b in kind):
            raise _MediaUnsupported("Invalid MP4 box boundary or type.")
        yield cursor, size, kind, header
        cursor += size


def _children(data, start, end, cancel):
    return list(_boxes(lambda p, n: data[p:p + n], start, end, cancel))


def _find(data, start, end, kind, cancel):
    return next((box for box in _children(data, start, end, cancel) if box[2] == kind), None)


def _find_path(data, root, kinds, cancel):
    box = root
    for kind in kinds:
        box = _find(data, box[0] + box[3], box[0] + box[1], kind, cancel)
        if box is None:
            return None
    return box


def _validate_movie(data, cancel):
    top = _children(data, 0, len(data), cancel)
    if len(top) != 1 or top[0][2] != b"moov":
        raise _MediaUnsupported("Missing complete MP4 movie metadata.")
    moov, video_tracks = top[0], 0
    movie_header = _find_path(data, moov, (b"mvhd",), cancel)
    if movie_header is None:
        raise _MediaUnsupported("MP4 movie timing metadata is missing.")
    for trak in _children(data, moov[3], moov[1], cancel):
        if trak[2] != b"trak":
            continue
        handler = _find_path(data, trak, (b"mdia", b"hdlr"), cancel)
        if handler is None or handler[1] - handler[3] < 24:
            continue
        hstart = handler[0] + handler[3]
        if data[hstart + 8:hstart + 12] != b"vide":
            continue
        description = _find_path(data, trak, (b"mdia", b"minf", b"stbl", b"stsd"), cancel)
        timing = _find_path(data, trak, (b"mdia", b"mdhd"), cancel)
        if description is None or timing is None:
            raise _MediaUnsupported("Video codec/timing metadata is missing.")
        start, size, _, header = description
        payload = data[start + header:start + size]
        if len(payload) < 8 or int.from_bytes(payload[4:8], "big") < 1:
            raise _MediaUnsupported("MP4 video sample description is missing.")
        entries = _children(payload, 8, len(payload), cancel)
        if len(entries) != int.from_bytes(payload[4:8], "big"):
            raise _MediaUnsupported("Invalid MP4 video sample-description count.")
        for entry in entries:
            if entry[2] in (b"encv", b"enca"):
                raise _MediaUnsupported("Protected MP4 media is not canonicalized.")
            if entry[1] < 86:
                raise _MediaUnsupported("Incomplete MP4 video sample description.")
            width, height = struct.unpack_from(">HH", payload, entry[0] + 32)
            if not width or not height:
                raise _MediaUnsupported("MP4 video dimensions are missing.")
        video_tracks += 1
    if not video_tracks:
        raise _MediaUnsupported("No readable MP4 video track was found.")


def _canonical_offsets(payload, kind, mdat_ranges):
    if len(payload) < 8:
        raise _MediaUnsupported("Truncated MP4 chunk-offset table.")
    count = int.from_bytes(payload[4:8], "big")
    width = 8 if kind == b"co64" else 4
    if len(payload) != 8 + count * width:
        raise _MediaUnsupported("Invalid MP4 chunk-offset count.")
    normalized = bytearray(payload[:8])
    for position in range(8, len(payload), width):
        absolute = int.from_bytes(payload[position:position + width], "big")
        for start, end, logical_start in mdat_ranges:
            if start <= absolute < end:
                normalized.extend(struct.pack(">Q", logical_start + absolute - start))
                break
        else:
            raise _MediaUnsupported("A sample chunk points outside MP4 media payloads.")
    return bytes(normalized)


def _canonical_boxes(data, start, end, cancel, mdat_ranges, depth=0):
    if depth > 32:
        raise _MediaUnsupported("MP4 box nesting is too deep.")
    result = hashlib.sha256(b"MP4-structure-v1\0")
    for position, size, kind, header in _children(data, start, end, cancel):
        if kind in _METADATA:
            continue
        payload_start, payload_end = position + header, position + size
        if kind in _CONTAINERS:
            payload = _canonical_boxes(data, payload_start, payload_end, cancel, mdat_ranges, depth + 1)
        else:
            payload = data[payload_start:payload_end]
            if kind in (b"stco", b"co64"):
                payload = _canonical_offsets(payload, kind, mdat_ranges)
                kind = b"coff"  # same canonical representation for 32/64-bit offsets
            elif kind in (b"mvhd", b"mdhd", b"tkhd"):
                if not payload or payload[0] not in (0, 1):
                    raise _MediaUnsupported("Unsupported MP4 timing-header version.")
                date_end = 12 if payload[0] == 0 else 20
                minimum = (20 if kind != b"tkhd" else 24) if payload[0] == 0 else (32 if kind != b"tkhd" else 36)
                if len(payload) < minimum:
                    raise _MediaUnsupported("Truncated MP4 timing header.")
                payload = payload[:4] + payload[date_end:]
            elif kind == b"hdlr":
                if len(payload) < 24:
                    raise _MediaUnsupported("Truncated MP4 handler.")
                payload = payload[:24]  # optional handler display name is metadata
            elif kind == b"dref":
                if len(payload) < 8:
                    raise _MediaUnsupported("Truncated MP4 data reference.")
                references = _children(payload, 8, len(payload), cancel)
                if len(references) != int.from_bytes(payload[4:8], "big"):
                    raise _MediaUnsupported("Invalid MP4 data-reference count.")
                for ref in references:
                    p = payload[ref[0] + ref[3]:ref[0] + ref[1]]
                    if ref[2] != b"url " or len(p) != 4 or int.from_bytes(p, "big") & 0xFFFFFF != 1:
                        raise _MediaUnsupported("External MP4 media references are not canonicalized.")
        result.update(kind)
        result.update(struct.pack(">Q", len(payload)))
        result.update(payload)
    return result.digest()


def _media_hash(record, log, progress, cancel):
    path, initial = record["path_obj"], record["stat"]
    if initial.st_size < 16:
        raise _MediaUnsupported("The file is too small to be a complete MP4.")
    _check_cancel(cancel)
    profile = None
    with _View(path, initial) as raw:
        footer_size = int.from_bytes(raw.read(raw.size - 4, 4), "big")
        has_bdve = 16 <= footer_size <= raw.size and raw.read(raw.size - footer_size + 4, 4) == b"bdve"
    if has_bdve:
        helper = getattr(bdve_engine, "discover_readonly_profile", None)
        if helper is None:
            raise _MediaUnsupported("Read-only BDVE normalization is unavailable.")
        try:
            profile = helper(path, log=log, progress=lambda value: progress(0.15 * value), cancel=cancel)
        except bdve_engine.CancelledError:
            raise _Cancelled() from None
        if not profile:
            raise _MediaUnsupported("The BDVE normalization profile was not found.")
    record["wrapped"] = bool(profile)
    digest = hashlib.sha256(b"VideoFix-compressed-MP4-v1\0")
    with _View(path, initial, profile) as view:
        top = list(_boxes(view.read, 0, view.size, cancel))
        brands = [b for b in top if b[2] == b"ftyp"]
        movies = [b for b in top if b[2] == b"moov"]
        mdats = [b for b in top if b[2] == b"mdat"]
        if len(brands) != 1 or brands[0][1] - brands[0][3] < 8 or len(movies) != 1 or not mdats:
            raise _MediaUnsupported("Not a complete standard MP4 container.")
        moov = movies[0]
        if moov[1] > MAX_MOOV_SIZE:
            raise _MediaUnsupported("MP4 movie metadata exceeds the safe scan limit.")
        movie = view.read(moov[0], moov[1])
        _validate_movie(movie, cancel)
        mdat_ranges, logical_start = [], 0
        for position, size, _, header in mdats:
            mdat_ranges.append((position + header, position + size, logical_start))
            logical_start += size - header
        digest.update(_canonical_boxes(movie, 0, len(movie), cancel, mdat_ranges))
        # Fragment headers carry timing/sample data that is absent from moov.
        # Include them verbatim; a differently arranged remux may conservatively
        # fail to match, but cannot match through a changed playback timeline.
        for box in top:
            if box[2] == b"moof":
                if box[1] > MAX_MOOV_SIZE:
                    raise _MediaUnsupported("MP4 fragment metadata is too large.")
                digest.update(b"moof" + struct.pack(">Q", box[1]))
                digest.update(view.read(box[0], box[1]))
        total = sum(size - header for _, size, _, header in mdats)
        if not total:
            raise _MediaUnsupported("The MP4 has no compressed media payload.")
        digest.update(struct.pack(">Q", total))
        completed = 0
        for position, size, _, header in mdats:
            cursor, end = position + header, position + size
            while cursor < end:
                _check_cancel(cancel)
                block = view.read(cursor, min(CHUNK_SIZE, end - cursor))
                digest.update(block)
                cursor += len(block)
                completed += len(block)
                progress(0.15 + 0.85 * completed / total)
        _check_cancel(cancel)
    return digest.hexdigest()


def _build_groups(records):
    parent = list(range(len(records)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def join(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a
    exact_seen, media_seen = {}, {}
    for index, record in enumerate(records):
        if not record["scanned"] or record.get("unstable"):
            continue
        for mapping, value in ((exact_seen, record.get("sha256")), (media_seen, record.get("media_sha256"))):
            if value is not None:
                if value in mapping:
                    join(index, mapping[value])
                else:
                    mapping[value] = index
    components = defaultdict(list)
    for index, record in enumerate(records):
        if record["scanned"] and not record.get("unstable"):
            components[find(index)].append(record)
    groups = []
    for members in components.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (bool(r.get("wrapped")), str(r["path_obj"]).casefold(), str(r["path_obj"])))
        hashes = {r.get("sha256") for r in members}
        kind = "exact" if None not in hashes and len(hashes) == 1 else "media"
        groups.append({
            "kind": kind,
            "keep": str(members[0]["path_obj"]),
            "files": [{"path": str(r["path_obj"]), "bytes": r["stat"].st_size,
                       "mtime_ns": r["stat"].st_mtime_ns} for r in members],
            "sha256": members[0].get("sha256") if kind == "exact" else None,
            "media_sha256": next((r["media_sha256"] for r in members if r.get("media_sha256")), None),
            "duplicate_files": len(members) - 1,
            "reclaimable_bytes": sum(r["stat"].st_size for r in members[1:]),
        })
    groups.sort(key=lambda g: (g["keep"].casefold(), g["keep"]))
    return groups


def scan_duplicates(paths: list[Path], log: Callable[[str], None] | None = None,
                    progress: Callable[[float], None] | None = None,
                    cancel: threading.Event | None = None) -> dict:
    """List duplicate sets without modifying any file.

    ``progress`` receives monotonic floats in [0, 1]. Cancellation returns a
    partial result with ``cancelled=True``. Per-file failures are reported in
    ``errors`` and do not abort other files. Repeated input paths are ignored.
    ``scanned`` counts examined stable files; ``unique`` subtracts redundant
    group members. Reclaimable bytes are a logical-size estimate, not a delete
    operation. Groups are disjoint; a media group includes its exact subcopies.
    """
    records, errors, seen = [], [], set()
    cancelled, last_progress = False, 0.0
    def report(value):
        nonlocal last_progress
        last_progress = max(last_progress, min(1.0, max(0.0, float(value))))
        _safe_call(progress, last_progress)
    def logger(message):
        _safe_call(log, message)
    def problem(path, exc, stage):
        errors.append({"path": str(path), "error": str(exc), "stage": stage})
        logger(f"Skipped {path}: {exc}")
    report(0.0)
    try:
        for supplied in paths:
            _check_cancel(cancel)
            try:
                path, info = _safe_input(supplied)
                key = os.path.normcase(str(path))
                if key in seen:
                    continue
                seen.add(key)
                records.append({"path_obj": path, "stat": info, "scanned": False, "wrapped": False})
            except (OSError, TypeError, ValueError) as exc:
                problem(supplied, exc, "input")
        records.sort(key=lambda r: (str(r["path_obj"]).casefold(), str(r["path_obj"])))
        by_size = defaultdict(list)
        for record in records:
            by_size[record["stat"].st_size].append(record)
        candidates = [r for members in by_size.values() if len(members) > 1 for r in members]
        raw_total = sum(r["stat"].st_size for r in candidates)
        raw_done = 0
        def raw_progress(count):
            nonlocal raw_done
            raw_done += count
            report(0.05 + 0.35 * raw_done / max(1, raw_total))
        for record in candidates:
            _check_cancel(cancel)
            try:
                record["sha256"] = _file_hash(record, raw_progress, cancel)
                record["scanned"] = True
            except _Cancelled:
                raise
            except Exception as exc:
                record["unstable"] = True
                problem(record["path_obj"], exc, "exact_hash")
        report(0.4)
        media_total = sum(r["stat"].st_size or 1 for r in records) or 1
        media_done = 0
        for record in records:
            _check_cancel(cancel)
            weight = record["stat"].st_size or 1
            if not record.get("unstable"):
                logger(f"Comparing: {record['path_obj'].name}")
                try:
                    record["media_sha256"] = _media_hash(record, logger,
                        lambda value, base=media_done, size=weight: report(0.4 + 0.6 * (base + size * value) / media_total), cancel)
                except _Cancelled:
                    raise
                except bdve_engine.CancelledError:
                    raise _Cancelled() from None
                except Exception as exc:
                    if isinstance(exc, (OSError, _Changed)):
                        record["unstable"] = True
                        record.pop("sha256", None)
                    problem(record["path_obj"], exc, "media_hash")
                record["scanned"] = not record.get("unstable", False)
            media_done += weight
            report(0.4 + 0.6 * media_done / media_total)
        _check_cancel(cancel)
    except (_Cancelled, bdve_engine.CancelledError):
        cancelled = True
    groups = _build_groups(records)
    scanned = sum(bool(r["scanned"] and not r.get("unstable")) for r in records)
    duplicates = sum(g["duplicate_files"] for g in groups)
    if not cancelled:
        report(1.0)
    return {"groups": groups, "scanned": scanned, "unique": scanned - duplicates,
            "duplicate_files": duplicates,
            "reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups),
            "errors": errors, "cancelled": cancelled}

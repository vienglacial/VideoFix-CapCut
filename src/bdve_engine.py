"""Streaming, non-destructive repair of CapCut BDVE Type 1 MP4 files.

The trailer, parameter hash and sample-table parsing follow the MIT-licensed
capcut-cache-recover project, vendored as :mod:`bdve_reference`. Container
validation here is structural; a full decoder check can be run by the caller.
"""
from __future__ import annotations

import hashlib
import mmap
import os
from pathlib import Path
import struct
import tempfile
import threading
from typing import Callable

import bdve_reference as reference


class RepairError(RuntimeError):
    """A supported repair failed, or the repaired container was invalid."""


class CancelledError(RuntimeError):
    """Repair was cancelled; no destination was published."""


class UnsupportedFormat(RepairError):
    """The input is not a supported BDVE Type 1 file or standard video MP4."""


MAX_DISCOVERY_STEP = 5_000_000
MAX_DISCOVERY_HASHES = 12_000_000
MAX_MOOV_SIZE = 64 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024
KNOWN_PROFILES = (
    (600_056, 533_594), (463_682, 214_523),
    (766_265, 114_840), (331_491, 174_662),
    *reference.BDVE_KNOWN_PROFILES,
)
_profile_cache = list(dict.fromkeys(KNOWN_PROFILES))
_cache_lock = threading.Lock()
_CODECS = {
    b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc",
    b"av01": "av1", b"vp09": "vp9", b"mp4v": "mpeg4", b"mp4a": "aac",
    b"Opus": "opus", b"ac-3": "ac3", b"ec-3": "eac3", b"alac": "alac",
}
_STRUCTURAL_TYPES = (
    b"moov", b"mvhd", b"trak", b"tkhd", b"edts", b"elst", b"mdia", b"mdhd",
    b"hdlr", b"minf", b"vmhd", b"smhd", b"dinf", b"dref", b"url ", b"stbl",
    b"stsd", b"stts", b"stss", b"stsc", b"stsz", b"stco", b"co64", b"sgpd",
    b"sbgp", b"udta", b"meta", b"ilst",
)


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise CancelledError("Repair cancelled.")


def _boxes(data, start, end):
    """Strict box iterator; never silently accepts a partial trailing atom."""
    cursor = start
    while cursor < end:
        if cursor + 8 > end:
            raise RepairError("Truncated MP4 box header.")
        size, kind = struct.unpack_from(">I4s", data, cursor)
        header = 8
        if size == 1:
            if cursor + 16 > end:
                raise RepairError("Truncated extended MP4 box header.")
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header = 16
        elif size == 0:
            size = end - cursor
        if size < header or cursor + size > end:
            raise RepairError("Invalid MP4 box size.")
        yield reference.Box(cursor, size, kind, header)
        cursor += size


def _children(data, parent):
    return list(_boxes(data, parent.content_start, parent.end))


def _child(data, parent, kind):
    return next((b for b in _children(data, parent) if b.type == kind), None)


def _path(data, parent, *kinds):
    for kind in kinds:
        parent = _child(data, parent, kind)
        if parent is None:
            return None
    return parent


def _duration(data, box):
    if box is None or box.content_start + 20 > box.end:
        return None
    start = box.content_start
    if data[start] == 0:
        scale, duration = struct.unpack_from(">II", data, start + 12)
    elif data[start] == 1 and start + 32 <= box.end:
        scale = struct.unpack_from(">I", data, start + 20)[0]
        duration = struct.unpack_from(">Q", data, start + 24)[0]
    else:
        return None
    return duration / scale if scale else None


def _inspect_data(data):
    """Check all top-level atom boundaries and the first video description."""
    top = list(_boxes(data, 0, len(data)))
    ftyp = next((b for b in top if b.type == b"ftyp"), None)
    moov = next((b for b in top if b.type == b"moov"), None)
    mdats = [b for b in top if b.type == b"mdat"]
    if ftyp is None or ftyp.size < ftyp.header_size + 8 or moov is None or not mdats:
        raise UnsupportedFormat("Not a complete standard MP4 video container.")
    if any(not all(32 <= byte <= 126 for byte in b.type) for b in top):
        raise RepairError("Invalid top-level MP4 box type.")
    videos, audio = [], []
    for trak in _children(data, moov):
        if trak.type != b"trak":
            continue
        hdlr = _path(data, trak, b"mdia", b"hdlr")
        if hdlr is None or hdlr.content_start + 12 > hdlr.end:
            continue
        handler = data[hdlr.content_start + 8:hdlr.content_start + 12]
        stsd = _path(data, trak, b"mdia", b"minf", b"stbl", b"stsd")
        if stsd is None or stsd.content_start + 16 > stsd.end:
            continue
        count = struct.unpack_from(">I", data, stsd.content_start + 4)[0]
        if not count:
            continue
        entry = stsd.content_start + 8
        size, kind = struct.unpack_from(">I4s", data, entry)
        if size < 16 or entry + size > stsd.end:
            raise RepairError("Invalid MP4 sample description.")
        codec = _CODECS.get(kind, kind.decode("ascii", "replace"))
        if handler == b"soun":
            audio.append(codec)
        elif handler == b"vide":
            if size < 86:
                raise RepairError("Incomplete video sample description.")
            width, height = struct.unpack_from(">HH", data, entry + 32)
            if not width or not height:
                raise RepairError("Video dimensions are missing.")
            if kind in (b"encv", b"enca"):
                raise UnsupportedFormat("This MP4 uses a different encrypted media format.")
            duration = _duration(data, _path(data, trak, b"mdia", b"mdhd"))
            stsz = _path(data, trak, b"mdia", b"minf", b"stbl", b"stsz")
            sample_count = None
            if stsz is not None and stsz.content_start + 12 <= stsz.end:
                sample_count = struct.unpack_from(">I", data, stsz.content_start + 8)[0]
            videos.append({"video_codec": codec, "sample_entry": kind.decode("ascii", "replace"),
                           "width": width, "height": height, "video_duration_seconds": duration,
                           "sample_count": sample_count,
                           "frame_rate": sample_count / duration if sample_count and duration else None})
    if not videos:
        raise UnsupportedFormat("No readable video track was found in this MP4.")
    result = videos[0]
    result["audio_codecs"] = audio
    result["duration_seconds"] = _duration(data, _child(data, moov, b"mvhd"))
    result["validation"] = "container_and_video_metadata"
    return result


def inspect_mp4(path: Path):
    """Read-only container/video metadata validation; does not decode frames."""
    with Path(path).open("rb") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            raise UnsupportedFormat("The input file is empty.")
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _inspect_data(data)


def _footer(data):
    # Use the reviewed parser without allocating a copy of the media payload.
    try:
        return reference.parse_bdve_footer(data)
    except (IndexError, ValueError, OverflowError):
        return None


def _keys(data):
    candidates = [data[0]]
    if len(data) >= 8:
        for kind in (b"ftyp", b"uuid", b"free"):
            key = data[4] ^ kind[0]
            if bytes(byte ^ key for byte in data[4:8]) == kind:
                candidates.append(key)
    return tuple(dict.fromkeys(candidates))


def _moov_samples(data, end, key, cancel):
    """Read only a bounded candidate moov, never an XOR copy of the full file."""
    marker_states = [(b"moov", False), (bytes(x ^ key for x in b"moov"), True)]
    seen = set()
    for marker, encrypted in marker_states:
        cursor, candidates = 0, 0
        while candidates < 256:
            _cancelled(cancel)
            found = data.find(marker, cursor, end)
            if found < 0:
                break
            cursor, candidates = found + 1, candidates + 1
            start = found - 4
            if start < 0 or (start, encrypted) in seen:
                continue
            seen.add((start, encrypted))
            head = bytes(data[start:min(start + 16, end)])
            if encrypted:
                head = bytes(x ^ key for x in head)
            if len(head) < 8:
                continue
            size = int.from_bytes(head[:4], "big")
            header = 8
            if size == 1:
                if len(head) < 16:
                    continue
                size, header = int.from_bytes(head[8:16], "big"), 16
            if size < header or size > MAX_MOOV_SIZE or start + size > end:
                continue
            block = bytes(data[start:start + size])
            if encrypted:
                block = block.translate(bytes(x ^ key for x in range(256)))
            try:
                samples = reference.parse_video_samples_from_moov(
                    block, reference.Box(0, size, b"moov", header))
                if samples and all(0 <= s.pos < end and 0 < s.size <= end - s.pos
                                   for s in samples[:260]):
                    return samples, (start + 4, encrypted)
            except (reference.DecodeError, IndexError, ValueError, OverflowError):
                continue
    raise UnsupportedFormat("Cannot read video sample tables to discover this BDVE profile.")


def _moov_ranges(data, end, key, cancel):
    """Find a moov header even when its children cross many XOR boundaries."""
    seen = set()
    for encrypted in (False, True):
        marker = bytes(x ^ key for x in b"moov") if encrypted else b"moov"
        cursor = 0
        for _ in range(256):
            _cancelled(cancel)
            found = data.find(marker, cursor, end)
            if found < 0:
                break
            cursor = found + 4
            start = found - 4
            if start < 0 or start in seen:
                continue
            header = bytes(data[start:min(start + 16, end)])
            if encrypted:
                header = bytes(x ^ key for x in header)
            size = int.from_bytes(header[:4], "big")
            minimum = 8
            if size == 1 and len(header) == 16:
                size, minimum = int.from_bytes(header[8:16], "big"), 16
            if minimum <= size <= MAX_MOOV_SIZE and start + size <= end:
                seen.add(start)
                yield start, start + size


def _integer_state(data, position, width, upper, key):
    """Classify a bounded integer's FIRST byte, safely across XOR boundaries.

    Checking the entire word alone can misclassify a word that straddles an
    encryption boundary. Its most-significant byte must independently have
    exactly one possible raw/XOR interpretation before we use the observation.
    """
    first = data[position]
    maximum_first = upper >> (8 * (width - 1))
    raw_prefix = first <= maximum_first
    xor_prefix = (first ^ key) <= maximum_first
    if raw_prefix == xor_prefix:
        return None
    encrypted = xor_prefix
    word = bytes(data[position:position + width])
    if encrypted:
        word = bytes(x ^ key for x in word)
    value = int.from_bytes(word, "big")
    return encrypted if 0 < value <= upper else None


def _structural_observations(data, footer, key, cancel):
    """Recover constraints from independently readable atom headers/tables.

    A short periodic profile can split a moov hundreds of times, so there is no
    uniformly decoded copy to parse. FourCC headers and bounded sample-size /
    chunk-offset integers still reveal local states without knowing the period.
    Only observations are collected; the trailer SHA remains the final proof.
    """
    observations = []
    for start, end in _moov_ranges(data, footer.start, key, cancel):
        for kind in _STRUCTURAL_TYPES:
            for encrypted in (False, True):
                marker = bytes(x ^ key for x in kind) if encrypted else kind
                cursor = start + 4
                for _ in range(4096):
                    _cancelled(cancel)
                    found = data.find(marker, cursor, end)
                    if found < 0:
                        break
                    cursor = found + 4
                    box_start = found - 4
                    header = bytes(data[box_start:found + 4])
                    if encrypted:
                        header = bytes(x ^ key for x in header)
                    size = int.from_bytes(header[:4], "big")
                    if size < 8 or box_start + size > end:
                        continue
                    observations.extend((found + i, encrypted) for i in range(4))
                    if kind not in (b"stsz", b"stco", b"co64"):
                        continue
                    table_start = box_start + (20 if kind == b"stsz" else 16)
                    width = 8 if kind == b"co64" else 4
                    count = max(0, (box_start + size - table_start) // width)
                    stride = max(1, count // 2048)
                    upper = min(footer.start, (1 << (8 * width)) - 1)
                    for index in range(0, count, stride):
                        if index % 2048 == 0:
                            _cancelled(cancel)
                        position = table_start + index * width
                        state = _integer_state(data, position, width, upper, key)
                        if state is not None:
                            observations.append((position, state))
    return observations


def _ordered_observations(observations):
    """Test neighboring state changes first; they prune periods most sharply."""
    ordered = sorted(set(observations))
    changes = []
    for index, item in enumerate(ordered):
        if index == 0 or item[1] != ordered[index - 1][1]:
            changes.extend(ordered[max(0, index - 1):index + 1])
    return list(dict.fromkeys(changes + ordered))


def _observations(data, footer, key, log, cancel):
    observations = [(0, True), (4, True)]
    observations.extend(_structural_observations(data, footer, key, cancel))
    sample_count = 0
    try:
        samples, moov_state = _moov_samples(data, footer.start, key, cancel)
        sample_count = len(samples)
        observations.append(moov_state)
        # Cover the whole movie. The old first-260-only window left far too many
        # candidate lengths for profiles whose raw gaps are narrow or infrequent.
        if len(samples) <= 4096:
            selected = samples
        else:
            selected = [samples[i * (len(samples) - 1) // 4095] for i in range(4096)]
        for sample in selected:
            _cancelled(cancel)
            if sample.size < 5 or sample.pos + sample.size > footer.start:
                continue
            state = _integer_state(data, sample.pos, 4, sample.size - 4, key)
            if state is not None:
                observations.append((sample.pos, state))
    except UnsupportedFormat:
        # Mixed-state metadata is supported by the structural path above.
        pass
    if len(observations) < 6 or not any(not state for _, state in observations):
        raise UnsupportedFormat("Insufficient clear/raw sample boundaries for this unknown BDVE profile.")
    observations = _ordered_observations(observations)
    log(f"Discovering profile from {sample_count:,} video samples and {len(observations):,} structural/boundary observations.")
    return observations


def _discover(data, footer, log, progress, cancel):
    keys = _keys(data)
    with _cache_lock:
        profiles = tuple(_profile_cache)
    for step, length in profiles:
        for key in keys:
            _cancelled(cancel)
            if reference.param_sha256(step, length, key) == footer.sha256:
                return reference.CryptorParams(step, length, key)
    log("This profile is new; checking the parameter hash against video boundaries.")
    for key in keys:
        try:
            observations = _observations(data, footer, key, log, cancel)
        except UnsupportedFormat:
            continue
        hashes = 0
        for step in range(1, MAX_DISCOVERY_STEP + 1):
            if step % 2048 == 0:
                _cancelled(cancel)
                progress(0.05 + 0.28 * step / MAX_DISCOVERY_STEP)
            low, high = 1, step
            for pos, encrypted in observations:
                remainder = pos % step
                if encrypted:
                    low = max(low, remainder + 1)
                else:
                    high = min(high, remainder)
                if low > high:
                    break
            if low > high:
                continue
            prefix, suffix = step.to_bytes(4, "big"), bytes([key])
            for length in range(low, high + 1):
                if hashes % 2048 == 0:
                    _cancelled(cancel)
                hashes += 1
                if hashes > MAX_DISCOVERY_HASHES:
                    raise UnsupportedFormat("The unknown profile exceeded the bounded discovery work limit.")
                if hashlib.sha256(prefix + length.to_bytes(4, "big") + suffix).digest() == footer.sha256:
                    with _cache_lock:
                        if (step, length) not in _profile_cache:
                            _profile_cache.append((step, length))
                    return reference.CryptorParams(step, length, key)
    raise UnsupportedFormat("No supported BDVE profile matched the exact parameter hash.")


def _stream_recover(data, end, params, output, progress, cancel):
    table = bytes(x ^ params.key for x in range(256))
    position = 0
    while position < end:
        _cancelled(cancel)
        length = min(CHUNK_SIZE, end - position)
        block = bytearray(data[position:position + length])
        cursor = 0
        while cursor < length:
            remainder = (position + cursor) % params.step
            if remainder < params.length:
                count = min(params.length - remainder, length - cursor)
                block[cursor:cursor + count] = block[cursor:cursor + count].translate(table)
            else:
                count = min(params.step - remainder, length - cursor)
            cursor += count
        output.write(block)
        position += length
        progress(0.35 + 0.55 * position / end)
    _cancelled(cancel)


def _publish_no_overwrite(temporary, destination):
    # Windows rename never replaces an existing destination. On POSIX, linking
    # exclusively into the same directory supplies the same no-overwrite rule.
    if os.name == "nt":
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        os.unlink(temporary)


def discover_readonly_profile(source: Path,
                              log: Callable[[str], None] | None = None,
                              progress: Callable[[float], None] | None = None,
                              cancel: threading.Event | None = None) -> dict | None:
    """Return an exact-hash-verified BDVE profile without writing any file.

    Returns None when no BDVE trailer is present. Unsupported BDVE raises
    UnsupportedFormat; callers can use the profile to normalize read-only
    byte ranges for duplicate comparison. The encrypted payload ends at
    ``payload_end`` and excludes the trailer.
    """
    source = Path(source)
    logger, report = log or (lambda message: None), progress or (lambda value: None)
    _cancelled(cancel)
    with source.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            return None
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            footer = _footer(data)
            if footer is None:
                return None
            if footer.cryptor_type != 1:
                raise UnsupportedFormat(f"Unsupported BDVE cryptor type: {footer.cryptor_type}")
            params = _discover(data, footer, logger, report, cancel)
            _cancelled(cancel)
            report(1.0)
            return {"payload_end": footer.start, "footer_size": footer.size,
                    "profile": {"step": params.step, "length": params.length, "key": params.key}}


def repair_file(source: Path, destination: Path,
                log: Callable[[str], None] | None = None,
                progress: Callable[[float], None] | None = None,
                cancel: threading.Event | None = None,
                verify: Callable[[Path], dict] | None = None) -> dict:
    """Restore one file without changing its source or replacing a destination.

    Returns ``repaired``, ``skipped_healthy`` (structural MP4 validation), or
    ``unsupported``. I/O errors, collisions and failed output validation raise;
    cancellation raises :class:`CancelledError`. ``progress`` receives one float
    in [0, 1]. Optional ``verify(temporary_path)`` runs before publication and
    may return decoder-check metadata; any exception discards the temporary.
    """
    source, destination = Path(source), Path(destination)
    logger, report = log or (lambda message: None), progress or (lambda value: None)
    _cancelled(cancel)
    if os.path.normcase(str(source.resolve())) == os.path.normcase(str(destination.resolve())):
        raise ValueError("The destination must differ from the source file.")
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    if not source.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")
    common = {"source": str(source), "destination": str(destination)}
    report(0.0)
    temporary = None
    try:
        with source.open("rb") as input_file:
            initial = os.fstat(input_file.fileno())
            if initial.st_size == 0:
                return {**common, "status": "unsupported", "reason": "The input file is empty."}
            with mmap.mmap(input_file.fileno(), 0, access=mmap.ACCESS_READ) as data:
                _cancelled(cancel)
                footer = _footer(data)
                if footer is None:
                    try:
                        info = _inspect_data(data)
                    except (RepairError, ValueError, IndexError, struct.error) as exc:
                        return {**common, "status": "unsupported", "reason": str(exc)}
                    report(1.0)
                    return {**common, "status": "skipped_healthy", "size": len(data), "video_info": info}
                if footer.cryptor_type != 1:
                    return {**common, "status": "unsupported",
                            "reason": f"Unsupported BDVE cryptor type: {footer.cryptor_type}"}
                logger(f"BDVE Type 1, version {footer.version}; original file remains unchanged.")
                report(0.03)
                try:
                    params = _discover(data, footer, logger, report, cancel)
                except UnsupportedFormat as exc:
                    return {**common, "status": "unsupported", "reason": str(exc)}
                logger(f"Verified profile: step={params.step}, length={params.length}, key=0x{params.key:02X}.")
                _cancelled(cancel)
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix=".capcut-repair-", suffix=".bdve-part",
                                                   dir=destination.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    report(0.35)
                    _stream_recover(data, footer.start, params, output, report, cancel)
                    output.flush()
                    os.fsync(output.fileno())
                final = os.fstat(input_file.fileno())
                if (final.st_size, final.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
                    raise RepairError("The source changed during repair; the output was discarded.")
        _cancelled(cancel)
        logger("Checking recovered MP4 boxes and video metadata.")
        report(0.93)
        try:
            info = inspect_mp4(temporary)
        except (RepairError, ValueError, IndexError, struct.error) as exc:
            raise RepairError(f"Recovered MP4 validation failed: {exc}") from exc
        _cancelled(cancel)
        verification = verify(temporary) if verify is not None else None
        _cancelled(cancel)
        report(0.99)
        _cancelled(cancel)
        _publish_no_overwrite(temporary, destination)
        temporary = None
        result = {**common, "status": "repaired", "size": footer.start,
                  "profile": {"step": params.step, "length": params.length, "key": params.key},
                  "removed_footer_bytes": footer.size, "video_info": info,
                  "verification": verification}
        logger(f"Recovered: {destination}")
        report(1.0)
        return result
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

"""BDVE Type 1 recovery support for still images (PNG and JPEG).

CapCut wraps cached pictures exactly like videos: one key byte is XOR-ed over
the first ``length`` bytes of every ``step``-byte period, and a ``bdve``
trailer stores SHA-256(step, length, key). Video discovery reads MP4 sample
tables; pictures need their own evidence of which bytes are scrambled:

* PNG  - every chunk carries a CRC-32, so a chunk validates only in its true
  state, and a chunk that straddles a boundary pins that boundary exactly.
* JPEG - inside entropy-coded data 0xFF is always followed by 0x00 or RSTn,
  so any other follower proves which state that byte is in.

Evidence only narrows the search. A profile is accepted when it reproduces
the trailer hash ("parameter_hash"). A PNG that fits inside one period, where
the hash cannot be reached, is accepted when every chunk CRC maps the whole
file ("structure"). JPEG has no checksum, and a garbled stretch inside
metadata or between two 0xFF bytes passes any syntax check - and usually a
decoder too - so a JPEG is never accepted without the hash.

This module never writes files and does not import the engine.
"""
from __future__ import annotations

import hashlib
import re
import zlib

import bdve_reference as reference

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
SUFFIXES = {"png": (".png",), "jpeg": (".jpg", ".jpeg")}

MAX_SPLIT_CHUNK = 1 << 18     # largest PNG chunk searched for an exact boundary
EXACT_SPLITS = 12             # exact boundaries wanted before skipping the search
MIN_RUN = 24                  # consecutive agreeing JPEG clues that count as proof
JPEG_TRIMS = (0, 3)           # clues dropped from each end of a run: exact first, then cautious
MAX_MULTIPLE = 4096
MAX_HASHES = 8_000_000
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


class ImageError(ValueError):
    """The picture is damaged, or its BDVE profile cannot be proven."""


def detect(data, size, wrapped):
    """Return ``(format, key)`` when ``data`` starts with a PNG/JPEG signature.

    A ``wrapped`` file is tested under the key its first byte implies; a plain
    file uses key 0. Returns None for anything else (videos included).
    """
    if size < 16:
        return None
    head = bytes(data[:8])
    for name, signature in (("png", PNG_SIGNATURE), ("jpeg", JPEG_SIGNATURE)):
        key = head[0] ^ signature[0] if wrapped else 0
        if bytes(byte ^ key for byte in head[:len(signature)]) == signature:
            return name, key
    return None


def _chunk_type(kind):
    return len(kind) == 4 and all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)


class _TailFlips:
    """CRC-32 change caused by XOR-ing the last ``m`` bytes of a buffer with the key.

    CRC-32 is affine, so that change depends only on ``m``. Looking it up
    finds a boundary inside a chunk in linear time instead of trying every
    offset with a fresh checksum.
    """

    def __init__(self, key):
        self._key = bytes([key])
        self._flipped = self._plain = self._count = 0
        self._first, self._more = {0: 0}, {}

    def lengths(self, delta, limit):
        while self._count < limit:
            self._flipped = zlib.crc32(self._key, self._flipped)
            self._plain = zlib.crc32(b"\0", self._plain)
            self._count += 1
            value = self._flipped ^ self._plain
            if value in self._first:
                self._more.setdefault(value, []).append(self._count)
            else:
                self._first[value] = self._count
        found = [self._first[delta]] if delta in self._first else []
        return [m for m in found + self._more.get(delta, []) if m <= limit]


def _png_chunk(view, flips, key, position, end, state, split):
    """Validate the chunk at ``position`` assuming it starts in ``state``.

    Returns ``(stop, kind, low, high)`` or None. ``low``/``high`` are None for
    a chunk lying in one state; otherwise the state changes inside it, at an
    offset in ``(low, high]`` (one offset unless CRC-32 cannot separate them).
    """
    if position + 12 > end:
        return None
    length = int.from_bytes(view(position, position + 4, state), "big")
    stop = position + 12 + length
    if stop <= end:
        body = view(position + 4, stop, state)
        computed, stored = zlib.crc32(body[:-4]), int.from_bytes(body[-4:], "big")
        if computed == stored:
            return (stop, body[:4], None, None) if _chunk_type(body[:4]) else None
        if split and length + 4 <= MAX_SPLIT_CHUNK:
            spread = key * 0x01010101
            points = [stop - 4 - m for m in flips.lengths(computed ^ stored ^ spread, length + 4)]
            points += [stop - 4 + j for j in (1, 2, 3) if computed == stored ^ (spread >> (8 * j))]
            kinds = {}
            for point in points:
                kind = bytes(byte ^ key if position + 4 + index >= point else byte
                             for index, byte in enumerate(body[:4]))
                if _chunk_type(kind):
                    kinds[point] = kind
            if kinds:
                return stop, kinds[min(kinds)], min(kinds) - 1, max(kinds)
    if split:
        for j in (1, 2, 3):     # boundary inside the 4-byte length field
            mixed = view(position, position + j, state) + view(position + j, position + 4, not state)
            stop = position + 12 + int.from_bytes(mixed, "big")
            if stop > end or stop - position > MAX_SPLIT_CHUNK:
                continue
            body = view(position + 4, stop, not state)
            if zlib.crc32(body[:-4]) == int.from_bytes(body[-4:], "big") and _chunk_type(body[:4]):
                return stop, body[:4], position + j - 1, position + j
    return None


def _png_resync(view, flips, key, data, table, position, end, state):
    """Find the next chunk start after one that cannot be validated.

    The chunk's own length field is trusted first (a chunk that validates
    must follow it); only then is the file searched for the next chunk that
    validates, which can also land inside a picture embedded in metadata.
    """
    def valid(start, split=False):
        return any(_png_chunk(view, flips, key, start, end, candidate, split) for candidate in (True, False))

    for candidate in ((True, False) if state is None else (state, not state)):
        stop = position + 12 + int.from_bytes(view(position, position + 4, candidate), "big")
        if stop + 12 <= end and valid(stop, True):
            return stop
    markers = [name.translate(table) if encrypted else name
               for name in (b"IDAT", b"IEND") for encrypted in (False, True)]
    cursors = [position + 8] * len(markers)
    for _ in range(4096):
        hits = [data.find(marker, cursor, end) for marker, cursor in zip(markers, cursors)]
        live = [(hit, index) for index, hit in enumerate(hits) if hit >= 0]
        if not live:
            return None
        hit, index = min(live)
        if hit - 4 > position and valid(hit - 4):
            return hit - 4
        cursors[index] = hit + 1
    return None


def _png_scan(data, end, key, checkpoint):
    """Walk the chunk chain, collecting proven ``(offset, encrypted)`` states.

    The second result is True when every chunk up to a final IEND validated,
    so the whole scramble map of the file is known.
    """
    table = bytes(value ^ key for value in range(256))

    def view(start, stop, encrypted):
        block = bytes(data[start:stop])
        return block.translate(table) if encrypted else block

    flips = _TailFlips(key)
    observations = [(0, True), (7, True)]
    position, state, splits = 8, True, 0
    complete, ended = True, False
    while position < end and not ended:
        checkpoint()
        found = began = None
        for candidate in ((True, False) if state is None else (state, not state)):
            found = _png_chunk(view, flips, key, position, end, candidate, False)
            if found:
                began = candidate
                break
        if not found and splits < EXACT_SPLITS:
            for candidate in ((True, False) if state is None else (state,)):
                found = _png_chunk(view, flips, key, position, end, candidate, True)
                if found:
                    began, splits = candidate, splits + 1
                    break
        if not found:
            complete = False
            position, state = _png_resync(view, flips, key, data, table, position, end, state), None
            if position is None:
                break
            continue
        stop, kind, low, high = found
        observations.append((position, began))
        state = began
        if low is not None:
            state = not began
            observations += [(low, began), (high, state)]
        observations.append((stop - 1, state))
        # An IEND before the end belongs to a picture embedded in metadata.
        position, ended = stop, kind == b"IEND" and stop == end
    return observations, complete and ended


def _jpeg_runs(data, end, key, checkpoint):
    """Find stretches of entropy-coded data whose byte stuffing proves one state.

    A 0xFF that is not followed by 0x00/RSTn cannot be a plain data byte, and
    the same holds for the scrambled image of that pair. Metadata gives
    contradictory clues, so only long unbroken runs of agreeing clues count.
    Returns ``[(encrypted, [offsets])]`` and the state of the final EOI marker
    (None when the file does not end with one).
    """
    stuffing = {0x00, *range(0xD0, 0xD8)}
    neutral = stuffing | {byte ^ key for byte in stuffing} | {0xFF, 0xFF ^ key}
    follower = b"(?=[^" + b"".join(re.escape(bytes([byte])) for byte in sorted(neutral)) + b"])"
    pattern = re.compile(b"(\xff)" + follower + b"|" + re.escape(bytes([0xFF ^ key])) + follower, re.DOTALL)
    runs = []
    for index, match in enumerate(pattern.finditer(data, 0, end - 2)):   # the EOI is read separately
        if index % 4096 == 0:
            checkpoint()
        encrypted = match.group(1) is not None
        if runs and runs[-1][0] == encrypted:
            runs[-1][1].append(match.start())
        else:
            runs.append((encrypted, [match.start()]))
    tail = bytes(data[end - 2:end])
    closing = True if tail == bytes([0xFF ^ key, 0xD9 ^ key]) else False if tail == b"\xff\xd9" else None
    return [run for run in runs if len(run[1]) >= MIN_RUN], closing


def _jpeg_observations(runs, closing, end, trim):
    """Turn clean runs into ``(offset, encrypted)`` facts.

    A marker between two scans looks like one clue for the wrong state. It is
    harmless inside a run but can stretch a run's end past a real boundary,
    so ``trim`` drops that many clues from both ends of every run.
    """
    observations = [(0, True), (2, True)]
    for encrypted, offsets in runs:
        observations += [(offsets[trim], encrypted), (offsets[-1 - trim], encrypted)]
    if closing is not None:
        observations.append((end - 2, closing))
    return observations


def _solve(observations, key, target, checkpoint):
    """Return the ``(step, length)`` profile matching the trailer hash, or None.

    Wherever a raw offset is followed by an encrypted one a multiple of
    ``step`` lies between them; the narrowest such gap lists the candidates,
    every other observation prunes them, and SHA-256 decides.
    """
    ordered = sorted(set(observations))
    gaps = [(b - a, a, b) for (a, first), (b, second) in zip(ordered, ordered[1:]) if not first and second]
    if not gaps:
        return None
    _, low_edge, high_edge = min(gaps)
    changes = []
    for index in range(1, len(ordered)):
        if ordered[index][1] != ordered[index - 1][1]:
            changes += ordered[index - 1:index + 1]
    probes = list(dict.fromkeys(changes + ordered))
    suffix, hashes = bytes([key]), 0
    for multiple in range(1, MAX_MULTIPLE + 1):
        checkpoint()
        if high_edge // multiple < 2:
            break
        for step in range(low_edge // multiple + 1, high_edge // multiple + 1):
            low, high = 1, step
            for offset, encrypted in probes:
                remainder = offset % step
                if encrypted:
                    low = max(low, remainder + 1)
                else:
                    high = min(high, remainder)
                if low > high:
                    break
            if low > high:
                continue
            prefix = step.to_bytes(4, "big")
            for length in range(low, high + 1):
                hashes += 1
                if hashes % 4096 == 0:
                    checkpoint()
                    if hashes > MAX_HASHES:
                        return None
                if hashlib.sha256(prefix + length.to_bytes(4, "big") + suffix).digest() == target:
                    return reference.CryptorParams(step, length, key)
    return None


def discover(data, end, target, detected, log, checkpoint):
    """Return ``(CryptorParams, proof)`` for a wrapped picture.

    ``end`` is the payload size without the trailer, ``target`` the trailer
    hash and ``detected`` the result of :func:`detect`. ``checkpoint`` is
    called often and may raise to cancel. Raises :class:`ImageError` when no
    profile can be proven. ``proof`` is ``"parameter_hash"``, or - PNG only -
    ``"structure"``: the caller must then run :func:`inspect` with
    ``strict=True`` on the recovered file.
    """
    name, key = detected
    if name == "png":
        observations, whole = _png_scan(data, end, key, checkpoint)
        attempts = [observations]
    else:
        runs, closing = _jpeg_runs(data, end, key, checkpoint)
        attempts, whole = [_jpeg_observations(runs, closing, end, trim) for trim in JPEG_TRIMS], False
    ordered = sorted(set(attempts[0]))
    changes = [(a, b, second) for (a, first), (b, second) in zip(ordered, ordered[1:]) if first != second]
    log(f"Picture ({name}): {len(ordered):,} proven states, {len(changes):,} scramble boundaries located.")
    for observations in attempts:
        params = _solve(observations, key, target, checkpoint)
        if params is not None:
            return params, "parameter_hash"
    if whole and not changes:
        return reference.CryptorParams(end, end, key), "structure"
    if whole and len(changes) == 1 and changes[0][1] == changes[0][0] + 1 and not changes[0][2]:
        return reference.CryptorParams(end, changes[0][1], key), "structure"
    if name == "jpeg" and len(changes) < 2:
        raise ImageError("Ảnh JPEG này nằm gọn trong một chu kỳ mã hóa nên không có điểm nào để đối chiếu "
                         "với mã băm trong file; chưa thể phục hồi một cách chắc chắn. "
                         "Điều này chưa có nghĩa là ảnh bị hỏng.")
    raise ImageError("Chưa xác định được thông số mã hóa của ảnh này. Điều này chưa có nghĩa là ảnh bị hỏng.")


def _inspect_png(data, size, strict):
    if bytes(data[:8]) != PNG_SIGNATURE:
        raise ImageError("Không phải ảnh PNG.")
    position, info, pixels = 8, None, 0
    while position + 12 <= size:
        length = int.from_bytes(data[position:position + 4], "big")
        stop = position + 12 + length
        if stop > size:
            raise ImageError("Ảnh PNG bị cắt cụt.")
        kind = bytes(data[position + 4:position + 8])
        if zlib.crc32(data[position + 4:stop - 4]) != int.from_bytes(data[stop - 4:stop], "big"):
            raise ImageError(f"Ảnh PNG sai mã kiểm tra (CRC) tại vị trí {position:,}.")
        if position == 8:
            if kind != b"IHDR" or length != 13:
                raise ImageError("Ảnh PNG thiếu phần đầu IHDR.")
            info = {"format": "png", "width": int.from_bytes(data[16:20], "big"),
                    "height": int.from_bytes(data[20:24], "big"), "bit_depth": data[24], "color_type": data[25]}
        pixels += kind == b"IDAT"
        position = stop
        if kind == b"IEND":
            break
    else:
        raise ImageError("Ảnh PNG thiếu phần kết thúc IEND.")
    if info is None or not pixels or not info["width"] or not info["height"]:
        raise ImageError("Ảnh PNG không có dữ liệu hình.")
    if strict and position != size:
        raise ImageError("Có dữ liệu lạ sau phần kết thúc của ảnh PNG.")
    return info


def _inspect_jpeg(data, size, strict):
    """Walk the header segments up to the first scan; ``strict`` adds nothing for JPEG."""
    if bytes(data[:3]) != JPEG_SIGNATURE:
        raise ImageError("Không phải ảnh JPEG.")
    position, info = 2, None
    while True:
        if position + 4 > size or data[position] != 0xFF:
            raise ImageError("Cấu trúc ảnh JPEG không hợp lệ hoặc bị cắt cụt.")
        marker = data[position + 1]
        if marker == 0xFF:          # fill byte before a marker
            position += 1
            continue
        if marker < 0xC0 or marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            raise ImageError("Cấu trúc ảnh JPEG không hợp lệ.")
        length = int.from_bytes(data[position + 2:position + 4], "big")
        if length < 2 or position + 2 + length > size:
            raise ImageError("Ảnh JPEG bị cắt cụt.")
        if marker in _SOF_MARKERS and info is None and length >= 7:
            info = {"format": "jpeg", "height": int.from_bytes(data[position + 5:position + 7], "big"),
                    "width": int.from_bytes(data[position + 7:position + 9], "big")}
        if marker == 0xDA:
            break
        position += 2 + length
    if info is None or not info["width"] or not info["height"]:
        raise ImageError("Ảnh JPEG không có dữ liệu hình.")
    return info


def inspect(data, size, name, strict=False):
    """Validate a plain picture and return its format and dimensions.

    PNG: every chunk CRC up to IEND; ``strict`` also rejects data after IEND.
    JPEG: the header segments up to the first scan (it has no checksum).
    """
    try:
        info = (_inspect_png if name == "png" else _inspect_jpeg)(data, size, strict)
    except IndexError as error:
        raise ImageError("Ảnh bị cắt cụt.") from error
    info["validation"] = "full_structure" if name == "png" else "container_and_image_metadata"
    return info

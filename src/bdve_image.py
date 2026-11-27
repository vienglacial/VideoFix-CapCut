"""BDVE Type 1 recovery support for still images (PNG and JPEG).

CapCut wraps cached pictures exactly like videos: one key byte is XOR-ed over
the first ``length`` bytes of every ``step``-byte period, and a ``bdve``
trailer stores SHA-256(step, length, key). Video discovery reads MP4 sample
tables; pictures need their own evidence of which bytes are scrambled:

* PNG  - every chunk carries a CRC-32, so a chunk validates only in its true
  state, and a chunk that straddles a boundary pins that boundary exactly.
  When periods are shorter than a chunk no CRC can match; the chunk headers
  (an IDAT repeats the previous one) and the closing IEND are then known
  plaintext, and each of their bytes proves its own state.
* JPEG - inside entropy-coded data 0xFF is always followed by 0x00 or RSTn,
  so any other follower proves which state that byte is in.

Evidence only narrows the search. A file whose only boundary leaves the
scrambled state (the period is longer than the file) gives no step; steps up
to MAX_STEP are then tried against the hash. A profile is accepted when it reproduces
the trailer hash ("parameter_hash"). A PNG that fits inside one period, where
the hash cannot be reached, is accepted when every chunk CRC maps the whole
file ("structure"). JPEG has no checksum, and a garbled stretch inside
metadata or between two 0xFF bytes passes any syntax check - and usually a
decoder too - so a JPEG is never accepted without the hash.

This module never writes files and does not import the engine.
"""
from __future__ import annotations

import bisect
import concurrent.futures as futures
import hashlib
import os
import re
import zlib

import bdve_reference as reference

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
SUFFIXES = {"png": (".png",), "jpeg": (".jpg", ".jpeg")}

MAX_SPLIT_CHUNK = 1 << 18     # largest PNG chunk whose boundary search uses the lookup table
MAX_SCAN_CHUNK = 1 << 23      # larger ones (C2PA metadata, single-IDAT pictures) are walked instead
MAX_PAIR_CHUNK = 1 << 17      # largest chunk searched for a scrambled stretch lying wholly inside it
EXACT_SPLITS = 16             # exact boundaries wanted before skipping the search
MIN_RUN = 24                  # consecutive agreeing JPEG clues that count as proof
JPEG_TRIMS = (0, 3)           # clues dropped from each end of a run: exact first, then cautious
MAX_MULTIPLE = 4096
MAX_HASHES = 8_000_000
MAX_STEP = 1 << 24            # longest period tried when the file shows no step
LIKELY_STEP = 1 << 20         # every period seen so far is shorter (longest: 978,424); searched first
MAX_SINGLE_WIDTH = 2048       # widest boundary interval worth that search
SINGLE_HASHES_PER_WORKER = 60_000_000   # roughly 100 s of hashing on each worker process
PARALLEL_HASHES = 4_000_000   # smaller searches stay in this process
TASK_HASHES = 1_500_000       # work handed to a worker process at a time
JPEG_PATHS = 8                # finished decoding walks kept for later candidates to merge into
IEND_CHUNK = b"\0\0\0\0IEND\xaeB`\x82"
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
        self._first, self._more, self._values = {0: 0}, {}, [0]

    def _extend(self, limit):
        while self._count < limit:
            self._flipped = zlib.crc32(self._key, self._flipped)
            self._plain = zlib.crc32(b"\0", self._plain)
            self._count += 1
            value = self._flipped ^ self._plain
            self._values.append(value)
            if value in self._first:
                self._more.setdefault(value, []).append(self._count)
            else:
                self._first[value] = self._count

    def lengths(self, delta, limit):
        if limit > MAX_SPLIT_CHUNK:
            return self._scan(delta, limit)
        self._extend(limit)
        found = [self._first[delta]] if delta in self._first else []
        return [m for m in found + self._more.get(delta, []) if m <= limit]

    def pairs(self, delta, limit):
        """All ``(m1, m2)``, ``m1 > m2``, where flipping bytes ``m1..m2`` from the end gives ``delta``.

        That is a stretch in the other state lying wholly inside the buffer:
        flip the last ``m1`` bytes, then flip the last ``m2`` back.
        """
        self._extend(limit)
        values, first, more, found = self._values, self._first, self._more, []
        for m2 in range(limit):
            wanted = delta ^ values[m2]
            if wanted in first:
                found += [(m1, m2) for m1 in [first[wanted]] + more.get(wanted, []) if m2 < m1 <= limit]
        return found

    def _scan(self, delta, limit):
        """Same answer for a large chunk, walking the values instead of storing them."""
        flipped = plain = 0
        found = [0] if delta == 0 else []
        key, crc32 = self._key, zlib.crc32
        for count in range(1, limit + 1):
            flipped, plain = crc32(key, flipped), crc32(b"\0", plain)
            if flipped ^ plain == delta:
                found.append(count)
        return found


def _png_chunk(view, flips, key, position, end, state, split):
    """Validate the chunk at ``position`` assuming it starts in ``state``.

    Returns ``(stop, kind, edges)`` or None. ``edges`` is empty for a chunk
    lying in one state. Otherwise each ``(low, high)`` says the state flips
    at an offset in ``(low, high]`` (one offset unless CRC-32 cannot separate
    candidates): one edge for a boundary inside the chunk, two for a short
    stretch of the other state lying wholly inside its data.
    """
    if position + 12 > end:
        return None
    length = int.from_bytes(view(position, position + 4, state), "big")
    stop = position + 12 + length
    if stop <= end:
        body = view(position + 4, stop, state)
        computed, stored = zlib.crc32(body[:-4]), int.from_bytes(body[-4:], "big")
        if computed == stored:
            return (stop, body[:4], ()) if _chunk_type(body[:4]) else None
        if split and length + 4 <= MAX_SCAN_CHUNK:
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
                return stop, kinds[min(kinds)], ((min(kinds) - 1, max(kinds)),)
            if length + 4 <= MAX_PAIR_CHUNK and _chunk_type(body[:4]):
                # A stretch shorter than the chunk: in at ``first``, out at ``second``,
                # both after the type field and before the CRC field. Ambiguous
                # answers are dropped; the trailer hash still has the last word.
                inside = [(stop - 4 - m1, stop - 4 - m2) for m1, m2 in flips.pairs(computed ^ stored, length + 4)
                          if stop - 4 - m1 >= position + 8]
                if len(inside) == 1:
                    first, second = inside[0]
                    return stop, body[:4], ((first - 1, first), (second - 1, second))
    if split:
        for j in (1, 2, 3):     # boundary inside the 4-byte length field
            mixed = view(position, position + j, state) + view(position + j, position + 4, not state)
            stop = position + 12 + int.from_bytes(mixed, "big")
            if stop > end or stop - position > MAX_SPLIT_CHUNK:
                continue
            body = view(position + 4, stop, not state)
            if zlib.crc32(body[:-4]) == int.from_bytes(body[-4:], "big") and _chunk_type(body[:4]):
                return stop, body[:4], ((position + j - 1, position + j),)
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


def _known_states(data, position, plain, key):
    """Per-byte states of ``data[position:]`` known to hold ``plain``, or None if any byte disagrees."""
    states = []
    for index, want in enumerate(plain):
        byte = data[position + index]
        if byte == want:
            states.append((position + index, False))
        elif byte == want ^ key:
            states.append((position + index, True))
        else:
            return None
    return states


def _png_header(view, data, key, position, end, template, state):
    """Read the header of a chunk whose CRC cannot be matched (short periods).

    A header equal to ``template`` (the previous header, as consecutive IDAT
    chunks are) proves the state of each of its 8 bytes. Otherwise a header
    read in one state is trusted when the header after it reads as a chunk
    too. Returns ``(stop, header, observations)`` or None.
    """
    def plausible(start):
        if start + 12 > end:
            return False
        if template is not None and _known_states(data, start, template, key) is not None:
            return True
        for encrypted in (True, False):
            header = view(start, start + 8, encrypted)
            if _chunk_type(header[4:]) and start + 12 + int.from_bytes(header[:4], "big") <= end:
                return True
        return False

    if position + 12 > end:
        return None
    if template is not None:
        states = _known_states(data, position, template, key)
        stop = position + 12 + int.from_bytes(template[:4], "big")
        if states is not None and stop <= end:
            return stop, template, states
    for encrypted in ((True, False) if state is None else (state, not state)):
        header = view(position, position + 8, encrypted)
        stop = position + 12 + int.from_bytes(header[:4], "big")
        if _chunk_type(header[4:]) and stop <= end and (stop == end or plausible(stop)):
            return stop, header, [(position, encrypted), (position + 7, encrypted)]
    # A boundary inside the header: an IDAT type proves its own bytes, and the
    # length is the one reading of its 4 bytes after which another IDAT or IEND follows.
    states = _known_states(data, position + 4, b"IDAT", key)
    if states is None:
        return None
    raw, found = bytes(data[position:position + 4]), []
    for mask in range(16):
        length = bytes(byte ^ key if mask >> index & 1 else byte for index, byte in enumerate(raw))
        stop = position + 12 + int.from_bytes(length, "big")
        if stop + 12 <= end and any(_known_states(data, stop + 4, kind, key) is not None
                                    for kind in (b"IDAT", b"IEND")):
            found.append((stop, length + b"IDAT"))
    if len(found) != 1:
        return None
    return found[0][0], found[0][1], states


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
    complete, ended, template = True, False, None
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
            header = _png_header(view, data, key, position, end, template, state)
            if header:
                stop, template, states = header
                observations += states
                position, state = stop, states[-1][1]
                continue
            position, state = _png_resync(view, flips, key, data, table, position, end, state), None
            template = None
            if position is None:
                break
            continue
        stop, kind, edges = found
        template = (stop - position - 12).to_bytes(4, "big") + kind
        observations.append((position, began))
        state = began
        for low, high in edges:
            observations += [(low, state), (high, not state)]
            state = not state
        observations.append((stop - 1, state))
        # An IEND before the end belongs to a picture embedded in metadata.
        position, ended = stop, kind == b"IEND" and stop == end
    if not complete and end >= 8 + len(IEND_CHUNK):
        observations += _known_states(data, end - len(IEND_CHUNK), IEND_CHUNK, key) or []
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


def _huffman_lookup(counts, symbols):
    """16-bit lookup tables ``(code lengths, symbols)`` for one JPEG Huffman table; 0 length = invalid code."""
    lengths, values = bytearray(65536), bytearray(65536)
    code = index = 0
    for size in range(1, 17):
        for _ in range(counts[size - 1]):
            if index >= len(symbols) or code >= 1 << size:
                return None
            start, span = code << (16 - size), 1 << (16 - size)
            lengths[start:start + span] = bytes([size]) * span
            values[start:start + span] = bytes([symbols[index]]) * span
            code, index = code + 1, index + 1
        code <<= 1
    return lengths, values


def _jpeg_layout(head):
    """``(scan start, [(dc, ac) table per block of an MCU], MCU count, restart interval)`` of a
    baseline JPEG read from plain ``head``, or None for anything else (progressive, arithmetic,
    a first scan that does not interleave a colour picture, damaged headers)."""
    tables, frame, restart, position = {}, None, 0, 2
    while True:
        if position + 4 > len(head) or head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        length = int.from_bytes(head[position + 2:position + 4], "big")
        body = bytes(head[position + 4:position + 2 + length])
        if length < 2 or len(body) != length - 2:
            return None
        if marker in (0xC0, 0xC1) and len(body) >= 6:
            count = body[5]
            sampling = {body[6 + 3 * i]: (body[7 + 3 * i] >> 4, body[7 + 3 * i] & 15) for i in range(count)}
            frame = (int.from_bytes(body[3:5], "big"), int.from_bytes(body[1:3], "big"), sampling)
        elif marker in _SOF_MARKERS:
            return None
        elif marker == 0xC4:
            cursor = 0
            while cursor + 17 <= len(body):
                counts = body[cursor + 1:cursor + 17]
                lookup = _huffman_lookup(counts, body[cursor + 17:cursor + 17 + sum(counts)])
                if lookup is None:
                    return None
                tables[body[cursor]] = lookup
                cursor += 17 + sum(counts)
        elif marker == 0xDD and len(body) >= 2:
            restart = int.from_bytes(body[:2], "big")
        elif marker == 0xDA:
            if frame is None or not body:
                return None
            width, height, sampling = frame
            count = body[0]
            if count == 1 and len(sampling) > 1:
                return None
            hmax = max(h for h, v in sampling.values()) if count > 1 else 1
            vmax = max(v for h, v in sampling.values()) if count > 1 else 1
            blocks = []
            for i in range(count):
                ident, selector = body[1 + 2 * i], body[2 + 2 * i]
                dc, ac = tables.get(selector >> 4), tables.get(0x10 | selector & 15)
                if ident not in sampling or dc is None or ac is None:
                    return None
                h, v = sampling[ident] if count > 1 else (1, 1)
                blocks += [(dc, ac)] * (h * v)
            mcus = -(-width // (8 * hmax)) * -(-height // (8 * vmax))
            return position + 2 + length, blocks, mcus, restart
        position += 2 + length


def _jpeg_walk(buf, layout, state, limit, marks=None, shared=None, trail=None, paths=None):
    """Huffman-decode the first scan of ``buf`` from ``state`` without computing pixels.

    ``state`` is ``(offset, bits, bit count, padding bits, MCU index)``. Returns
    ``(word, offset, met, blocks)``: "error" at the first impossible code,
    "limit" on reaching ``limit``, "end" when the last MCU ends exactly where
    the data does. ``marks`` collects the state at the start of every MCU.

    With ``paths`` (a candidate walk) decoding goes on past the last MCU until
    the data runs out; ``blocks`` is then the block count reached there (None
    after an impossible code). From offset ``shared`` on every candidate
    decodes the same bytes, so two walks at the same bit before the same kind
    of block go on identically: ``trail`` records ``bit -> block number``
    there, and meeting an earlier ``(trail, blocks)`` path settles the walk
    (``met`` True) by the block count this one would end with.
    """
    _, blocks, mcus, restart = layout
    per_mcu = len(blocks)
    total = mcus * per_mcu
    i, acc, nbits, pad, mcu = state
    number = mcu * per_mcu
    size = len(buf)

    def fill():
        nonlocal i, acc, nbits, pad
        while nbits < 32:
            if not pad and i < size:
                byte = buf[i]
                if byte != 0xFF:
                    i += 1
                elif i + 1 < size and buf[i + 1] == 0:
                    i += 2
                else:
                    pad = 8         # a marker: real data ends here
                    acc, nbits = acc << 8, nbits + 8
                    continue
                acc, nbits = ((acc << 8) | byte) & 0xFFFFFFFFFF, nbits + 8
            else:
                acc, nbits, pad = (acc << 8) & 0xFFFFFFFFFF, nbits + 8, pad + 8

    while paths is not None or number < total:
        index = number % per_mcu
        if index == 0:
            mcu = number // per_mcu
            if restart and mcu and mcu % restart == 0:
                fill()
                if not pad or nbits - pad >= 8 or buf[i:i + 2] != bytes([0xFF, 0xD0 + (mcu // restart - 1) % 8]):
                    return "error", i, False, None
                i, acc, nbits, pad = i + 2, 0, 0, 0
            if marks is not None:
                marks.append((i, acc, nbits, pad, mcu))
            if i >= limit:
                return "limit", i, False, None
        if nbits < 27:
            fill()
        if paths is not None:
            real = nbits - pad
            if pad and real < 8 and (acc >> pad) & ((1 << real) - 1) == (1 << real) - 1:
                # only the 1-bits padding the last byte are left: the data ends on this block boundary
                return ("end" if number == total else "error"), i, False, number
            if not pad and i - 6 > shared:
                where = i                       # the byte holding the next unread bit
                for _ in range((nbits + 7) // 8):
                    where -= 1
                    if buf[where] == 0 and buf[where - 1] == 0xFF:
                        where -= 1
                if where >= shared:
                    bit = (where, nbits % 8)
                    trail[bit] = number
                    for path, ended in paths:
                        other = path.get(bit)
                        if other is not None and (other - number) % per_mcu == 0:
                            final = None if ended is None else ended + number - other
                            return ("end" if final == total else "error"), i, True, final
        (dc_lengths, dc_values), (ac_lengths, ac_values) = blocks[index]
        peek = (acc >> (nbits - 16)) & 0xFFFF
        size_ = dc_lengths[peek]
        magnitude = dc_values[peek]
        if not size_ or magnitude > 11:
            return "error", i, False, None
        nbits -= size_ + magnitude
        k = 1
        while k < 64:
            if nbits < 27:
                fill()
            peek = (acc >> (nbits - 16)) & 0xFFFF
            size_ = ac_lengths[peek]
            if not size_:
                return "error", i, False, None
            symbol = ac_values[peek]
            run, magnitude = symbol >> 4, symbol & 15
            if magnitude:
                k += run
                if k > 63 or magnitude > 10:
                    return "error", i, False, None
                nbits -= size_ + magnitude
                k += 1
            elif run == 15:
                nbits -= size_
                k += 16
                if k > 63:
                    return "error", i, False, None
            else:
                nbits -= size_
                break
        if pad and nbits < pad:         # ran into the padding: the data ended inside a block
            return "error", i, False, None
        number += 1
    fill()
    return ("end" if pad and nbits - pad < 8 else "error"), i, False, number


def _jpeg_boundaries(data, end, key, low_edge, high_edge, checkpoint):
    """Narrow where the scramble ends inside entropy-coded data, by decoding.

    The scrambled reading decodes cleanly up to the true boundary and breaks
    after it. A candidate boundary passes only when the whole scan then
    decodes to exactly the picture's block count and ends where the data
    ends; walks that merge into an earlier one inherit its ending, which
    keeps each try short. Returns the passing offsets in
    ``(low_edge, high_edge]``, or None when the picture cannot be judged.
    """
    scrambled = bytes(data[:end]).translate(bytes(value ^ key for value in range(256)))
    layout = _jpeg_layout(scrambled)
    if layout is None or layout[0] > low_edge:
        return None
    marks = []
    status, failed, _, _ = _jpeg_walk(scrambled, layout, (layout[0], 0, 0, 0, 0), end, marks)
    if status != "error" or failed <= low_edge:
        return None
    offsets, raw, paths, found = [mark[0] for mark in marks], bytes(data[:end]), [], []
    for boundary in range(min(high_edge, failed), low_edge, -1):
        checkpoint()
        state = marks[bisect.bisect_right(offsets, boundary) - 1]
        trail = {}
        status, _, met, ended = _jpeg_walk(scrambled[:boundary] + raw[boundary:], layout, state, end + 1,
                                           shared=high_edge, trail=trail, paths=paths)
        if not met and len(paths) < JPEG_PATHS:
            paths.append((trail, ended))
        if status == "end":
            found.append(boundary)
    return found


def _hash_steps(task):
    """Worker: the ``(step, length)`` in ``[first, last] x lengths`` whose parameter hash is ``target``, or None."""
    first, last, lengths, key, target = task
    suffixes = [(length, length.to_bytes(4, "big") + bytes([key])) for length in lengths]
    sha256 = hashlib.sha256
    for step in range(first, last + 1):
        base = sha256(step.to_bytes(4, "big"))
        for length, suffix in suffixes:
            digest = base.copy()
            digest.update(suffix)
            if digest.digest() == target:
                return step, length
    return None


def _workers():
    return max(1, min(61, (os.cpu_count() or 1) - 1))     # 61: the Windows process-pool limit


def _search_steps(first, last, lengths, key, target, checkpoint, report):
    """Try every step in ``[first, last]`` with every length, on all CPU cores when the work is large.

    ``report(done)`` gets the number of steps finished so far. Returns
    ``(step, length)`` or None. Cancelling through ``checkpoint`` drops the
    queued work; a worker finishes at most its current task (about a second).
    """
    if last < first:
        return None
    per_task = max(1, TASK_HASHES // len(lengths))
    tasks = [(start, min(start + per_task - 1, last), lengths, key, target)
             for start in range(first, last + 1, per_task)]
    workers = _workers()
    if workers == 1 or (last - first + 1) * len(lengths) <= PARALLEL_HASHES:
        for done, task in enumerate(tasks):
            checkpoint()
            found = _hash_steps(task)
            if found:
                return found
            report((done + 1) * per_task)
        return None
    workers = min(workers, len(tasks))
    pool, queued = futures.ProcessPoolExecutor(max_workers=workers), iter(tasks)
    try:
        pending, finished = set(), 0
        while True:
            # Only a few tasks are handed out at a time, so stopping leaves little work running.
            for task in queued:
                pending.add(pool.submit(_hash_steps, task))
                if len(pending) >= 2 * workers:
                    break
            if not pending:
                break
            checkpoint()
            ready, pending = futures.wait(pending, timeout=0.2, return_when=futures.FIRST_COMPLETED)
            for future in ready:
                found = future.result()
                if found:
                    return found
                finished += 1
            if ready:
                report(finished * per_task)
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _solve_single(observations, key, target, checkpoint, budget, lengths=None, report=None, searched=None):
    """Like :func:`_solve` for a file whose only boundary leaves the scrambled state.

    No step lies between observations then, so steps up to MAX_STEP are
    tried, the shortest first (up to LIKELY_STEP before the rest), for every
    length the boundary allows (only ``lengths``, when given). ``budget`` is
    a one-item list of hashes still allowed, shared between calls; ``report``
    gets the fraction of that budget used. Lengths in the set ``searched``
    were already tried past the last observation and are skipped there;
    lengths this call covers there are added to it. Returns CryptorParams or None.
    """
    ordered = sorted(set(observations))
    changes = [(a, b) for (a, first), (b, second) in zip(ordered, ordered[1:]) if first != second]
    if len(changes) != 1 or not ordered[0][1] or ordered[-1][1]:
        return None
    low_edge, high_edge = changes[0]
    if lengths is None:
        if high_edge - low_edge > MAX_SINGLE_WIDTH:
            return None
        lengths = range(low_edge + 1, high_edge + 1)
    lengths = sorted(length for length in lengths if low_edge < length <= high_edge)
    if not lengths:
        return None
    low_edge, high_edge = lengths[0] - 1, lengths[-1]
    wanted = set(lengths)
    probes = list(dict.fromkeys([ordered[-1]] + ordered[::-1]))
    farthest = ordered[-1][0]
    suffixes = [length.to_bytes(4, "big") + bytes([key]) for length in range(low_edge + 1, high_edge + 1)]
    total = budget[0]
    for step in range(low_edge + 2, min(farthest, MAX_STEP) + 1):
        # Up to the last observation each step is pruned by the proven states.
        if step % 1024 == 0:
            checkpoint()
        low, high = low_edge + 1, min(high_edge, step - 1)
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
        candidates = range(low, high + 1) if len(wanted) == high_edge - low_edge else \
            [length for length in lengths if low <= length <= high]
        budget[0] -= len(candidates)
        if budget[0] < 0:
            return None
        base = hashlib.sha256(step.to_bytes(4, "big"))
        for length in candidates:
            digest = base.copy()
            digest.update(suffixes[length - low_edge - 1])
            if digest.digest() == target:
                return reference.CryptorParams(step, length, key)
    # Past it every step allows every length: plain brute force, on all cores.
    searched = set() if searched is None else searched
    lengths = [length for length in lengths if length not in searched]
    first = max(low_edge + 2, farthest + 1, high_edge + 1)
    if not lengths:
        return None
    for start, stop in ((first, min(LIKELY_STEP, MAX_STEP)), (max(first, LIKELY_STEP + 1), MAX_STEP)):
        if stop < start:
            continue
        steps = min(stop - start + 1, budget[0] // len(lengths))
        if steps <= 0:
            return None
        before = budget[0]

        def progress(done, before=before):
            if report and total > 0:
                report(min(1.0, (total - before + min(done, steps) * len(lengths)) / total))

        found = _search_steps(start, start + steps - 1, lengths, key, target, checkpoint, progress)
        budget[0] -= steps * len(lengths)
        if found:
            return reference.CryptorParams(found[0], found[1], key)
        if steps < stop - start + 1:
            return None
    searched.update(lengths)
    return None


def discover(data, end, target, detected, log, checkpoint, progress=None):
    """Return ``(CryptorParams, proof)`` for a wrapped picture.

    ``end`` is the payload size without the trailer, ``target`` the trailer
    hash and ``detected`` the result of :func:`detect`. ``checkpoint`` is
    called often and may raise to cancel; ``progress`` gets a fraction while
    a long period search runs (it can use every CPU core). Raises :class:`ImageError` when no
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
    if len(changes) == 1 and not changes[0][2]:
        log("Picture: one boundary only, so the period is longer than the file; trying longer periods.")
        budget, searched = [SINGLE_HASHES_PER_WORKER * _workers()], set()
        report = progress or (lambda fraction: None)
        if name == "jpeg":
            low_edge, high_edge = changes[0][:2]
            found = _jpeg_boundaries(data, end, key, low_edge, high_edge, checkpoint)
            if found:
                log(f"Picture: decoding narrows the boundary to {len(found):,} offset(s) "
                    f"of {high_edge - low_edge:,}.")
                params = _solve_single(attempts[0], key, target, checkpoint, budget, found, report, searched)
                if params is not None:
                    return params, "parameter_hash"
        for observations in attempts:
            params = _solve_single(observations, key, target, checkpoint, budget, None, report, searched)
            if params is not None:
                return params, "parameter_hash"
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

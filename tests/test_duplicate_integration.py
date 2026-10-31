"""Independent duplicate scanner checks using real MP4 and BDVE media."""
from pathlib import Path
import hashlib
import importlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest


TEST_ROOT = Path(__file__).resolve().parent
WORK_ROOT = TEST_ROOT.parent
SOURCE_ROOT = Path(os.environ.get('CAPCUT_TOOL_SOURCE', str(WORK_ROOT / 'src')))
FIXTURE_ROOT = Path(os.environ.get('VIDEOFIX_FIXTURES', str(WORK_ROOT / 'fixtures'))) / 'canh2-batch-input'
sys.path.insert(0, str(SOURCE_ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    while start < end:
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            size, header = struct.unpack_from('>Q', data, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError('Malformed independent test fixture')
        yield start, start + size, header, kind
        start += size


class DuplicateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = importlib.import_module('duplicates')
        cls.plain = (FIXTURE_ROOT / 'videos' / 'can_v011_CharacterL.mp4').read_bytes()
        cls.production_plain = (FIXTURE_ROOT / 'CHARACTER_LOCK_STRICT_fingers_thick_tree_trunk_FIXED.mp4').read_bytes()
        cls.production_bdve_path = next(path for path in FIXTURE_ROOT.glob('*.mp4')
                                        if path.name.endswith('_fingers_thick_tree_trunk.mp4'))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='duplicates-', dir=TEST_ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT))
        self.addCleanup(self.temporary.cleanup)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.stat().st_size, path.stat().st_mtime_ns, digest(path))
                for path in self.root.rglob('*') if path.is_file()}

    def scan_readonly(self, paths, **kwargs):
        before = self.snapshot()
        try:
            return self.scanner.scan_duplicates(paths, **kwargs)
        finally:
            self.assertEqual(self.snapshot(), before, 'Duplicate scan changed an input or created a file')

    def groups(self, result):
        return [{Path(item['path']).resolve() for item in group['files']} for group in result['groups']]

    def changed_media(self):
        changed = bytearray(self.plain)
        mdat = next(box for box in boxes(changed) if box[3] == b'mdat')
        start, end, header, _ = mdat
        position = start + header + (end - start - header) // 2
        changed[position] ^= 1
        return changed

    def metadata_variant(self, payload=b'Independent regression metadata'):
        return self.plain + struct.pack('>I4s', len(payload) + 8, b'free') + payload

    def chunk_offset_entries(self, data):
        containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'dinf'}
        found = []

        def walk(start, end):
            for position, limit, header, kind in boxes(data, start, end):
                if kind in containers:
                    walk(position + header, limit)
                elif kind in (b'stco', b'co64'):
                    payload = position + header
                    count = struct.unpack_from('>I', data, payload + 4)[0]
                    width, form = (8, '>Q') if kind == b'co64' else (4, '>I')
                    found.extend((payload + 8 + index * width, form) for index in range(count))

        walk(0, len(data))
        self.assertTrue(found, 'Golden fixture has no MP4 chunk offsets')
        return found

    def changed_chunk_reference(self):
        changed = bytearray(self.plain)
        position, form = self.chunk_offset_entries(changed)[0]
        offset = struct.unpack_from(form, changed, position)[0]
        struct.pack_into(form, changed, position, offset + 1)
        return changed

    def relocated_metadata_variant(self):
        changed = bytearray(self.plain)
        insertion = next(boxes(changed))[1]
        metadata = struct.pack('>I4s', 40, b'free') + b'Metadata relocation test'.ljust(32, b'\0')
        self.assertEqual(len(metadata), 40)
        for position, form in self.chunk_offset_entries(changed):
            offset = struct.unpack_from(form, changed, position)[0]
            if offset >= insertion:
                struct.pack_into(form, changed, position, offset + len(metadata))
        return changed[:insertion] + metadata + changed[insertion:]

    def changed_video_timing(self):
        changed = bytearray(self.plain)
        moov = next(box for box in boxes(changed) if box[3] == b'moov')
        for trak in boxes(changed, moov[0] + moov[2], moov[1]):
            if trak[3] != b'trak':
                continue
            mdia = next((box for box in boxes(changed, trak[0] + trak[2], trak[1]) if box[3] == b'mdia'), None)
            if mdia is None:
                continue
            children = list(boxes(changed, mdia[0] + mdia[2], mdia[1]))
            hdlr = next(box for box in children if box[3] == b'hdlr')
            hdlr_start = hdlr[0] + hdlr[2]
            if changed[hdlr_start + 8:hdlr_start + 12] != b'vide':
                continue
            mdhd = next(box for box in children if box[3] == b'mdhd')
            data_start = mdhd[0] + mdhd[2]
            scale_offset = data_start + (12 if changed[data_start] == 0 else 20)
            scale = struct.unpack_from('>I', changed, scale_offset)[0]
            struct.pack_into('>I', changed, scale_offset, scale * 2)
            return changed
        self.fail('No video timing box in golden fixture')

    def wrapped_variant(self, step=70001, length=17321, key=39):
        encoded = bytearray(self.production_plain)
        translation = bytes(value ^ key for value in range(256))
        for start in range(0, len(encoded), step):
            end = min(start + length, len(encoded))
            encoded[start:end] = encoded[start:end].translate(translation)
        footer = bytearray(self.production_bdve_path.read_bytes()[-68:])
        footer[-44:-12] = hashlib.sha256(struct.pack('>IIB', step, length, key)).digest()
        return encoded + footer

    def test_renamed_exact_copy_and_unicode_paths(self):
        first = self.write('C\u1ea3nh 2/\u0110\u1ea7u v\u00e0o \u2013 g\u00e0.mp4', self.plain)
        second = self.write('B\u1ea3n sao/t\u00ean kh\u00e1c.MP4', self.plain)
        result = self.scan_readonly([first, second])
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(result['unique'], 1)
        self.assertEqual(self.groups(result), [{first, second}])
        self.assertEqual(result['reclaimable_bytes'], len(self.plain))

    def test_same_size_different_encoded_bytes_are_not_duplicates(self):
        first = self.write('first.mp4', self.plain)
        second = self.write('changed.mp4', self.changed_media())
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        result = self.scan_readonly([first, second])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['duplicate_files'], 0)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['reclaimable_bytes'], 0)

    def test_repeated_input_path_is_not_a_duplicate_file(self):
        source = self.write('only.mp4', self.plain)
        result = self.scan_readonly([source, source, source.parent / '.' / source.name])
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['unique'], 1)
        self.assertEqual(result['duplicate_files'], 0)
        self.assertEqual(result['groups'], [])

    def test_metadata_only_mp4_change_matches_media(self):
        first = self.write('original.mp4', self.plain)
        second = self.write('metadata-change.mp4', self.metadata_variant())
        self.assertNotEqual(digest(first), digest(second))
        result = self.scan_readonly([first, second])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(self.groups(result), [{first, second}])

    def test_equal_encoded_payload_with_changed_playback_timing_is_not_duplicate(self):
        first = self.write('original.mp4', self.plain)
        second = self.write('twice-video-speed.mp4', self.changed_video_timing())
        result = self.scan_readonly([first, second])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['duplicate_files'], 0)

    def test_changed_chunk_reference_is_not_treated_as_metadata_only(self):
        first = self.write('original.mp4', self.plain)
        second = self.write('different-chunk-reference.mp4', self.changed_chunk_reference())
        result = self.scan_readonly([first, second])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['duplicate_files'], 0)

    def test_metadata_insertion_and_relocated_chunk_offsets_still_match(self):
        first = self.write('original.mp4', self.plain)
        second = self.write('relocated.mp4', self.relocated_metadata_variant())
        self.assertNotEqual(digest(first), digest(second))
        result = self.scan_readonly([first, second])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(self.groups(result), [{first, second}])

    def test_exact_subset_and_metadata_variant_form_one_disjoint_group(self):
        first = self.write('a.mp4', self.plain)
        second = self.write('b-copy.mp4', self.plain)
        third = self.write('c-metadata.mp4', self.metadata_variant())
        result = self.scan_readonly([first, second, third])
        self.assertEqual(result['duplicate_files'], 2)
        self.assertEqual(result['unique'], 1)
        self.assertEqual(self.groups(result), [{first, second, third}])
        self.assertEqual(result['reclaimable_bytes'], sum(path.stat().st_size for path in (first, second, third))
                         - Path(result['groups'][0]['keep']).stat().st_size)

    def test_bdve_and_restored_mp4_match_without_creating_repaired_files(self):
        wrapped = self.write('wrapped.mp4', self.production_bdve_path.read_bytes())
        plain = self.write('restored.mp4', self.production_plain)
        result = self.scan_readonly([wrapped, plain])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(self.groups(result), [{wrapped, plain}])

    def test_same_media_wrapped_with_different_bdve_profiles_matches(self):
        first = self.write('production-profile.mp4', self.production_bdve_path.read_bytes())
        second = self.write('different-profile.mp4', self.wrapped_variant())
        self.assertNotEqual(digest(first), digest(second))
        result = self.scan_readonly([first, second])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(self.groups(result), [{first, second}])

    def test_cancel_before_scan_leaves_inputs_untouched(self):
        source = self.write('original.mp4', self.plain)
        cancel = threading.Event()
        cancel.set()
        result = self.scan_readonly([source], cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['scanned'], 0)
        self.assertEqual(result['groups'], [])

    def test_cancel_after_progress_returns_partial_result_without_writes(self):
        paths = [self.write(f'copy-{index}.mp4', self.plain) for index in range(3)]
        cancel = threading.Event()
        observed = []

        def progress(fraction):
            observed.append(fraction)
            if fraction > 0:
                cancel.set()

        result = self.scan_readonly(paths, progress=progress, cancel=cancel)
        self.assertTrue(observed)
        self.assertTrue(result['cancelled'])
        self.assertLess(result['scanned'], len(paths))

    def test_missing_file_reports_error_and_scans_remaining_input(self):
        source = self.write('original.mp4', self.plain)
        missing = self.root / 'missing.mp4'
        result = self.scan_readonly([missing, source])
        self.assertEqual(result['scanned'], 1)
        self.assertTrue(result['errors'])
        self.assertTrue(any(Path(error['path']).resolve() == missing for error in result['errors']))
        self.assertFalse(result['cancelled'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

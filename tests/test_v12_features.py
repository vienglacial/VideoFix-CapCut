"""Checks for 1.2 features: picture recovery, delete-original safety, no junk files.

Self-contained: pictures are generated here (no fixtures, no third-party
packages); every test works in a disposable folder under this directory.
"""
from pathlib import Path
import hashlib
import json
import os
import random
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from contextlib import redirect_stdout
import io
from types import SimpleNamespace

TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(os.environ.get('CAPCUT_TOOL_SOURCE', str(TEST_ROOT.parent / 'src')))))
import batch_jobs
import bdve_engine
import bdve_image
import video_fix


def png(width=640, height=360, chunk=8192, seed=1):
    """A valid RGB PNG with noisy pixels, its IDAT stream split into ``chunk``-byte chunks."""
    rng = random.Random(seed)
    rows = b''.join(b'\0' + bytes(rng.getrandbits(8) if rng.random() < 0.3 else (x * 7 + y) & 255
                                  for x in range(width * 3)) for y in range(height))
    stream = zlib.compress(rows, 6)

    def block(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (bdve_image.PNG_SIGNATURE + block(b'IHDR', header)
            + b''.join(block(b'IDAT', stream[i:i + chunk]) for i in range(0, len(stream), chunk))
            + block(b'IEND', b''))


def wrap(plain, step, length, key):
    """Scramble like CapCut BDVE Type 1 and append the trailer."""
    out = bytearray(plain)
    table = bytes(value ^ key for value in range(256))
    for start in range(0, len(out), step):
        stop = min(start + length, len(out))
        out[start:stop] = out[start:stop].translate(table)
    digest = hashlib.sha256(step.to_bytes(4, 'big') + length.to_bytes(4, 'big') + bytes([key])).digest()
    crpt = struct.pack('>I4sII', 48, b'crpt', 1, 3) + digest
    return bytes(out) + struct.pack('>I4s', 68, b'bdve') + crpt + struct.pack('>I4sI', 12, b'size', 68)


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='v12-', dir=TEST_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file())


class PictureRecoveryTests(Workspace):
    PLAIN = png()

    def repair(self, wrapped, name='in.png', **kwargs):
        source = self.root / name
        source.write_bytes(wrapped)
        return bdve_engine.repair_file(source, self.root / 'out' / (name + '_FIXED.png'), **kwargs)

    def test_multi_period_png_is_byte_exact_and_hash_proven(self):
        n = len(self.PLAIN)
        for step, length, key in ((n // 5, n // 9, 0x5A), (n // 3 + 17, n // 4, 0xC3), (n // 2 + 1, n // 3, 0x01)):
            with self.subTest(step=step, length=length):
                result = self.repair(wrap(self.PLAIN, step, length, key), name=f'{step}.png')
                self.assertEqual(result['status'], 'repaired')
                self.assertEqual(result['media'], 'image')
                self.assertEqual(result['proof'], 'parameter_hash')
                self.assertEqual(result['profile'], {'step': step, 'length': length, 'key': key})
                self.assertEqual(Path(result['destination']).read_bytes(), self.PLAIN)

    def test_scrambled_stretch_shorter_than_a_chunk(self):
        # Real CapCut profile seen on 2026-11-23: 1,947 scrambled bytes per 426,738-byte
        # period, so each stretch sits inside one 8 KB chunk (two boundaries in one chunk).
        n = len(self.PLAIN)
        for step, length, key in ((n // 6 + 11, 1947, 0x7A), (n // 9, 300, 0x3C), (n // 4 + 5, 5000, 0xE1)):
            with self.subTest(step=step, length=length):
                result = self.repair(wrap(self.PLAIN, step, length, key), name=f's{step}.png')
                self.assertEqual((result['status'], result['proof']), ('repaired', 'parameter_hash'))
                self.assertEqual(result['profile'], {'step': step, 'length': length, 'key': key})
                self.assertEqual(Path(result['destination']).read_bytes(), self.PLAIN)

    def test_plain_gap_shorter_than_a_chunk(self):
        n = len(self.PLAIN)
        step = n // 5 + 3
        result = self.repair(wrap(self.PLAIN, step, step - 900, 0x55))
        self.assertEqual((result['status'], result['proof']), ('repaired', 'parameter_hash'))
        self.assertEqual(Path(result['destination']).read_bytes(), self.PLAIN)

    def test_single_boundary_inside_one_huge_chunk(self):
        plain = png(chunk=1 << 30)          # the whole picture in one IDAT chunk
        self.assertGreater(len(plain), 300_000)
        result = self.repair(wrap(plain, len(plain) + 777, len(plain) // 3, 0x21))
        self.assertEqual((result['status'], result['proof']), ('repaired', 'structure'))
        self.assertEqual(Path(result['destination']).read_bytes(), plain)

    def test_png_inside_one_period_is_proven_by_every_crc(self):
        n = len(self.PLAIN)
        for step, length in ((n + 5000, n + 100), (n + 5000, n // 2)):
            with self.subTest(length=length):
                result = self.repair(wrap(self.PLAIN, step, length, 0x77), name=f'{length}.png')
                self.assertEqual((result['status'], result['proof']), ('repaired', 'structure'))
                self.assertEqual(Path(result['destination']).read_bytes(), self.PLAIN)

    def test_wrong_extension_is_corrected(self):
        source = self.root / 'picture.mp4'
        source.write_bytes(wrap(self.PLAIN, len(self.PLAIN) // 4, len(self.PLAIN) // 7, 0x31))
        result = bdve_engine.repair_file(source, self.root / 'out' / 'picture.mp4_FIXED.mp4')
        self.assertEqual(result['status'], 'repaired')
        self.assertTrue(result['destination'].endswith('picture.mp4_FIXED.png'))

    def test_corrupted_payload_is_never_published(self):
        damaged = bytearray(wrap(self.PLAIN, len(self.PLAIN) // 4, len(self.PLAIN) // 7, 0x31))
        damaged[len(damaged) // 2] ^= 0x10
        try:
            result = self.repair(bytes(damaged))
            self.assertNotEqual(result['status'], 'repaired')
        except bdve_engine.RepairError:
            pass
        self.assertEqual(self.files(), ['in.png'])

    def test_plain_png_is_reported_healthy_without_output(self):
        result = self.repair(self.PLAIN)
        self.assertEqual((result['status'], result['media']), ('skipped_healthy', 'image'))
        self.assertEqual(self.files(), ['in.png'])

    def test_small_jpeg_inside_one_period_is_refused_not_guessed(self):
        body = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' \
               b'\xff\xc0\x00\x0b\x08\x00\x10\x00\x10\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00' \
               + bytes(random.Random(3).getrandbits(8) & 0xFE for _ in range(4000)) + b'\xff\xd9'
        source = self.root / 'small.jpg'
        source.write_bytes(wrap(body, len(body) + 900, len(body) // 2, 0x44))
        result = bdve_engine.repair_file(source, self.root / 'out' / 'small.jpg_FIXED.jpg')
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(self.files(), ['small.jpg'])


class JunkAndCleanupTests(Workspace):
    def test_failed_verification_leaves_no_temp_or_empty_folder(self):
        source = self.root / 'in.png'
        source.write_bytes(wrap(png(), 50_000, 20_000, 9))
        def reject(path):
            raise RuntimeError('decoder said no')
        with self.assertRaises(RuntimeError):
            bdve_engine.repair_file(source, self.root / 'a' / 'b' / 'in.png_FIXED.png', verify=reject)
        self.assertEqual(self.files(), ['in.png'])
        self.assertFalse((self.root / 'a').exists())

    def test_headless_writes_no_report_unless_asked(self):
        (self.root / 'in').mkdir()
        (self.root / 'in' / 'p.png').write_bytes(wrap(png(), 50_000, 20_000, 9))
        args = SimpleNamespace(paths=[str(self.root / 'in')], no_recursive=False, output_dir=None, no_verify=True)
        with redirect_stdout(io.StringIO()) as capture:
            code = video_fix.headless(args)
        summary = json.loads(capture.getvalue())
        self.assertEqual((code, summary['repaired'], summary['reports']), (0, 1, []))
        self.assertEqual(self.files(), ['in/_PHUC_HOI_CAPCUT/p.png_FIXED.png', 'in/p.png'])

    def test_pictures_are_collected_with_their_own_extension(self):
        for name in ('a.PNG', 'b.jpeg', 'c.mp4', 'd.txt'):
            (self.root / name).write_bytes(b'0')
        jobs, _ = batch_jobs.collect_jobs([self.root])
        self.assertEqual(sorted(job.destination.name for job in jobs),
                         ['a.PNG_FIXED.png', 'b.jpeg_FIXED.jpeg', 'c.mp4_FIXED.mp4'])


class DeleteOriginalTests(Workspace):
    def run_batch(self, decoder=True):
        source = self.root / 'p.png'
        source.write_bytes(wrap(png(), 50_000, 20_000, 9))
        removed = []
        def remove(path):
            removed.append(Path(path).name)
            os.unlink(path)
        jobs, _ = batch_jobs.collect_jobs([source])
        with unittest.mock.patch.object(video_fix, 'find_ffmpeg', return_value=Path('ff.exe') if decoder else None), \
             unittest.mock.patch.object(video_fix, 'verify_playback', return_value={'full_decode': True}):
            records, _ = video_fix.run_jobs(jobs, threading.Event(), lambda *e: None,
                                            check=False, delete_source=True, remove=remove)
        return records[0], removed

    def test_original_goes_only_after_a_passed_decode(self):
        record, removed = self.run_batch()
        self.assertEqual(record['source_removed']['status'], 'deleted')
        self.assertEqual(removed, ['p.png'])
        self.assertEqual(self.files(), ['_PHUC_HOI_CAPCUT/p.png_FIXED.png'])

    def test_original_is_kept_without_a_decoder(self):
        record, removed = self.run_batch(decoder=False)
        self.assertEqual((record['source_removed']['status'], removed), ('kept', []))
        self.assertIn('p.png', self.files())

    def test_changed_original_or_copy_is_kept(self):
        destination = self.root / 'fixed.png'
        destination.write_bytes(b'12345')
        source = self.root / 'src.png'
        source.write_bytes(b'abc')
        info = {'verification': {'full_decode': True}, 'size': 5,
                'source_bytes': 3, 'source_mtime_ns': source.stat().st_mtime_ns + 1}
        self.assertEqual(video_fix.remove_source(source, destination, info, os.unlink)['status'], 'kept')
        info.update(source_mtime_ns=source.stat().st_mtime_ns, size=6)
        self.assertEqual(video_fix.remove_source(source, destination, info, os.unlink)['status'], 'kept')
        self.assertTrue(source.exists())


import unittest.mock  # noqa: E402  (used through unittest.mock.patch above)

if __name__ == '__main__':
    unittest.main(verbosity=2)

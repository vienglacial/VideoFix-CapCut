"""Focused scanner edge cases; every fixture is temporary and disposable."""
from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from duplicates import scan_duplicates


class DuplicateEdges(unittest.TestCase):
    def setUp(self):
        test_workspace = Path(__file__).resolve().parent / 'scratch'
        test_workspace.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='videofix-dupes-', dir=test_workspace)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(test_workspace.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_full_file_hash_does_not_match_same_prefix(self):
        data = b'x' * (5 * 1024 * 1024)
        a, b, c = [self.root / name for name in ('a.mp4', 'b.mp4', 'c.mp4')]
        a.write_bytes(data + b'a')
        b.write_bytes(data + b'a')
        c.write_bytes(data + b'b')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = scan_duplicates([a, b, c])
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['scanned'], 3)
        self.assertEqual(result['duplicate_files'], 1)
        self.assertEqual(result['groups'][0]['kind'], 'exact')
        self.assertEqual({f['path'] for f in result['groups'][0]['files']}, {str(a), str(b)})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_cancel_mid_hash_returns_partial_result_without_error(self):
        a, b = self.root / 'a.mp4', self.root / 'b.mp4'
        a.write_bytes(b'x' * (6 * 1024 * 1024))
        b.write_bytes(a.read_bytes())
        cancel = threading.Event()
        values = []
        def progress(value):
            values.append(value)
            if 0.05 < value < 0.4:
                cancel.set()
        result = scan_duplicates([a, b], progress=progress, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(values, sorted(values))
        self.assertLess(values[-1], 1.0)

    def test_changed_file_is_excluded_from_duplicate_group(self):
        a, b = self.root / 'a.mp4', self.root / 'b.mp4'
        a.write_bytes(b'x' * (5 * 1024 * 1024))
        b.write_bytes(a.read_bytes())
        changed = False
        def progress(value):
            nonlocal changed
            if not changed and value > 0.05:
                with a.open('ab') as fixture:
                    fixture.write(b'changed')
                changed = True
        result = scan_duplicates([a, b], progress=progress)
        self.assertTrue(changed)
        self.assertEqual(result['groups'], [])
        self.assertTrue(any(e['path'] == str(a) and e['stage'] == 'exact_hash' for e in result['errors']))

    def test_symlink_is_not_followed(self):
        target = self.root / 'original.mp4'
        target.write_bytes(b'video')
        link = self.root / 'renamed.mp4'
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'Symlink creation is unavailable: {exc}')
        result = scan_duplicates([target, link])
        self.assertEqual(result['groups'], [])
        self.assertTrue(any(e['path'] == str(link) and e['stage'] == 'input' for e in result['errors']))
        self.assertEqual(target.read_bytes(), b'video')


if __name__ == '__main__':
    unittest.main()

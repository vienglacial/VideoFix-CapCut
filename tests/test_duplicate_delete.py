"""Checks for moving confirmed duplicates to the Recycle Bin."""
from pathlib import Path
import os
import sys
import tempfile
import unittest


TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path(os.environ.get('CAPCUT_TOOL_SOURCE', str(TEST_ROOT.parent / 'src')))
sys.path.insert(0, str(SOURCE_ROOT))

from duplicate_delete import delete_duplicates  # noqa: E402
from duplicates import scan_duplicates  # noqa: E402


class DuplicateDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.files = [root / name for name in ('a.mp4', 'b.mp4', 'c.mp4')]
        for path in self.files:
            path.write_bytes(b'not really an mp4 but identical bytes' * 100)
        self.group = scan_duplicates(self.files)['groups'][0]
        self.removed = []

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, path, hwnd):
        self.removed.append(path)
        path.unlink()

    def test_deletes_only_targets_and_keeps_keeper(self):
        keep = self.group['keep']
        targets = [f['path'] for f in self.group['files'] if f['path'] != keep]
        results = delete_duplicates(self.group, keep, targets, remove=self.fake)
        self.assertEqual([r['status'] for r in results], ['deleted', 'deleted'])
        self.assertTrue(Path(keep).exists())
        self.assertEqual(len(self.removed), 2)

    def test_refuses_keeper_and_outsiders(self):
        keep = self.group['keep']
        results = delete_duplicates(self.group, keep, [keep, str(Path(self.temp.name) / 'x.mp4')],
                                    remove=self.fake)
        self.assertEqual([r['status'] for r in results], ['skipped', 'skipped'])
        self.assertEqual(self.removed, [])

    def test_skips_changed_target(self):
        keep = self.group['keep']
        target = next(f['path'] for f in self.group['files'] if f['path'] != keep)
        with open(target, 'ab') as handle:
            handle.write(b'!')
        self.assertEqual(delete_duplicates(self.group, keep, [target], remove=self.fake)[0]['status'], 'skipped')
        self.assertTrue(Path(target).exists())

    def test_nothing_deleted_when_keeper_missing(self):
        keep = self.group['keep']
        targets = [f['path'] for f in self.group['files'] if f['path'] != keep]
        os.remove(keep)
        results = delete_duplicates(self.group, keep, targets, remove=self.fake)
        self.assertTrue(all(r['status'] == 'skipped' for r in results))
        self.assertTrue(all(Path(t).exists() for t in targets))

    @unittest.skipUnless(os.name == 'nt', 'Recycle Bin is Windows only')
    def test_real_recycle_bin(self):
        keep = self.group['keep']
        target = next(f['path'] for f in self.group['files'] if f['path'] != keep)
        result = delete_duplicates(self.group, keep, [target])[0]
        self.assertEqual(result['status'], 'deleted', result['reason'])
        self.assertFalse(Path(target).exists())
        self.assertTrue(Path(keep).exists())


if __name__ == '__main__':
    unittest.main()

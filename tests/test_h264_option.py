"""The extra H264 copy is made only when asked for."""
from pathlib import Path
import os
import shutil
import sys
import tempfile
import threading
import unittest


TEST_ROOT = Path(__file__).resolve().parent
WORK_ROOT = TEST_ROOT.parent
SOURCE_ROOT = Path(os.environ.get('CAPCUT_TOOL_SOURCE', str(WORK_ROOT / 'src')))
FIXTURE_ROOT = Path(os.environ.get('VIDEOFIX_FIXTURES', str(WORK_ROOT / 'fixtures'))) / 'canh2-batch-input'
sys.path.insert(0, str(SOURCE_ROOT))

from batch_jobs import collect_jobs  # noqa: E402
from h264_export import FOLDER_NAME  # noqa: E402
from playback_check import find_ffmpeg  # noqa: E402
from video_fix import run_jobs  # noqa: E402

HEVC_SAMPLE = next(iter(sorted(FIXTURE_ROOT.glob('*1080.mp4'))), None)


@unittest.skipUnless(HEVC_SAMPLE, 'HEVC BDVE fixture not available')
class H264OptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copyfile(HEVC_SAMPLE, self.root / 'clip.mp4')

    def tearDown(self):
        self.temp.cleanup()

    def run_repair(self, h264):
        jobs, _ = collect_jobs([self.root])
        records, _ = run_jobs(jobs, threading.Event(), lambda *event: None, check=False, h264=h264)
        self.assertEqual(records[0]['status'], 'repaired')
        return records[0]

    def test_default_off_creates_no_h264_file(self):
        jobs, _ = collect_jobs([self.root])
        records, _ = run_jobs(jobs, threading.Event(), lambda *event: None, check=False)
        self.assertNotIn('h264', records[0])
        self.assertEqual(list(self.root.rglob('*H264*')), [])
        self.assertFalse(any(p.name == FOLDER_NAME for p in self.root.rglob('*')))

    @unittest.skipUnless(find_ffmpeg(), 'FFmpeg not installed')
    def test_ticked_creates_verified_copy(self):
        record = self.run_repair(True)
        self.assertEqual(record['h264']['status'], 'created', record['h264'])
        self.assertTrue(Path(record['h264']['path']).is_file())
        self.assertEqual(list(self.root.rglob('*.part.mp4')), [])


if __name__ == '__main__':
    unittest.main()

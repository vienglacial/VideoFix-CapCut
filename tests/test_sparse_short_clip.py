"""Real short-clip regression: sparse constraints exceeded the old hash budget."""
from pathlib import Path
import hashlib
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('CAPCUT_TOOL_SOURCE', str(ROOT / 'src')))
import bdve_engine as engine


class SparseShortClipTests(unittest.TestCase):
    def test_uncached_short_clip_recovers_with_default_budget(self):
        source = Path(os.environ.get('VIDEOFIX_SHORT_CLIP', str(ROOT / 'fixtures' / 'sparse-short-clip.mp4')))
        if not source.is_file():
            self.skipTest('Set VIDEOFIX_SHORT_CLIP to the local short-clip fixture.')
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        before = digest(source)
        self.assertEqual(before, '4c52914614d0eaf0a012be8e9d709ac9c103c9a24c70acd8568c5bacbd5b3512')
        with engine._cache_lock:
            previous = list(engine._profile_cache)
            engine._profile_cache.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'restored.mp4'
                result = engine.repair_file(source, destination)
                self.assertEqual(result['status'], 'repaired')
                self.assertEqual(result['profile'], {'step': 530355, 'length': 387090, 'key': 154})
                # Golden output was independently fully decoded using FFmpeg.
                self.assertEqual(digest(destination), '5e644742e5dad98d225cff36e12d761c26f93dc349a1e37cde29215f38aa9516')
                self.assertEqual(result['video_info']['sample_count'], 121)
                self.assertEqual((result['video_info']['width'], result['video_info']['height']), (1920, 1080))
        finally:
            with engine._cache_lock:
                engine._profile_cache[:] = previous
        self.assertEqual(digest(source), before)


if __name__ == '__main__':
    unittest.main()

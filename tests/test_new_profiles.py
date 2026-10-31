"""Regressions for mixed-state moov and sparse early encryption boundaries."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "src"))
import bdve_engine as engine

FIXTURES = Path(os.environ.get("VIDEOFIX_FIXTURES", str(WORK / "fixtures"))) / "unsupported-samples"
CASES = {
    "keole 1.mp4": {
        "profile": {"step": 2689, "length": 916, "key": 191},
        "sha256": "88c41eb182765f642fc4a912198b04a47f862770ebc2c16bcfa3c8370f6b1d43",
        "dimensions": (1920, 1080),
    },
    "nam dau 2.mp4": {
        "profile": {"step": 1026014, "length": 951207, "key": 125},
        "sha256": "c2db012b162ffeff68a072bcb16bd8a4fde26fd16646dc453be595b23583e296",
        "dimensions": (854, 480),
    },
}


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@contextmanager
def uncached(hash_limit=2_000_000):
    with engine._cache_lock:
        old_cache = list(engine._profile_cache)
        engine._profile_cache.clear()
    old_limit = engine.MAX_DISCOVERY_HASHES
    engine.MAX_DISCOVERY_HASHES = hash_limit
    try:
        yield
    finally:
        engine.MAX_DISCOVERY_HASHES = old_limit
        with engine._cache_lock:
            engine._profile_cache[:] = old_cache


class NewProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all((FIXTURES / name).exists() for name in CASES):
            raise unittest.SkipTest("Local new-profile regression fixtures are unavailable.")

    def test_mixed_moov_and_28_byte_ftyp_discovery(self):
        source = FIXTURES / "keole 1.mp4"
        before = digest(source)
        with uncached():
            result = engine.discover_readonly_profile(source)
        self.assertEqual(result["profile"], CASES[source.name]["profile"])
        self.assertEqual(result["payload_end"], source.stat().st_size - 68)
        self.assertEqual(digest(source), before)
        # The actual source has a 28-byte ftyp and a 19KB moov split repeatedly
        # by a 2689-byte profile; a whole-moov raw/XOR assumption cannot parse it.

    def test_all_movie_boundaries_keep_discovery_under_two_million_hashes(self):
        source = FIXTURES / "nam dau 2.mp4"
        with uncached(hash_limit=2_000_000):
            result = engine.discover_readonly_profile(source)
        self.assertEqual(result["profile"], CASES[source.name]["profile"])
        # v1 exhausted twelve million hashes using only the first 260 samples.

    def test_recovery_matches_both_full_decode_verified_golden_hashes(self):
        with tempfile.TemporaryDirectory(prefix="new-profiles-", dir=WORK) as temp:
            for name, expected in CASES.items():
                with self.subTest(name=name), uncached():
                    source = FIXTURES / name
                    before = digest(source)
                    destination = Path(temp) / (source.stem + "_FIXED.mp4")
                    result = engine.repair_file(source, destination)
                    self.assertEqual(result["status"], "repaired")
                    self.assertEqual(result["profile"], expected["profile"])
                    self.assertEqual(digest(destination), expected["sha256"])
                    self.assertEqual(digest(source), before)
                    info = result["video_info"]
                    self.assertEqual((info["width"], info["height"]), expected["dimensions"])
                    self.assertEqual(info["sample_count"], 721)

    def test_length_word_spanning_a_boundary_never_claims_opposite_first_byte(self):
        key = 191
        raw = bytes.fromhex("00000020")
        encoded = bytes(x ^ key for x in raw)
        self.assertIs(engine._integer_state(raw, 0, 4, 1000, key), False)
        self.assertIs(engine._integer_state(encoded, 0, 4, 1000, key), True)
        # The middle of a word can be in another state; neither whole-word
        # interpretation is valid, so it must not become a false constraint.
        self.assertIsNone(engine._integer_state(encoded[:1] + raw[1:], 0, 4, 1000, key))
        self.assertIsNone(engine._integer_state(raw[:1] + encoded[1:], 0, 4, 1000, key))


if __name__ == "__main__":
    unittest.main(verbosity=2)

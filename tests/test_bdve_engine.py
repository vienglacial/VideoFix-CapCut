"""Real-media regression and safe-output checks for the standalone repair engine.

Run from any folder with:
    python -m unittest discover -s <this-directory> -p "test_*.py" -v

Fixtures are read-only references to the local batch snapshot. Every test works
on a disposable copy underneath this test directory. Golden hashes came from
the independently verified, fully decoded repairs, not the reusable engine.
"""
from pathlib import Path
import hashlib
import importlib.util
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
FIXTURES = json.loads((TEST_ROOT / 'fixtures.json').read_text(encoding='utf-8'))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_engine():
    sys.path.insert(0, str(SOURCE_ROOT))
    specification = importlib.util.spec_from_file_location('bdve_engine', SOURCE_ROOT / 'bdve_engine.py')
    module = importlib.util.module_from_spec(specification)
    sys.modules['bdve_engine'] = module
    specification.loader.exec_module(module)
    return module


class RepairEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regression-', dir=TEST_ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT))
        self.addCleanup(self.temporary.cleanup)

    def fixture_copy(self, index=0, name='input.mp4'):
        destination = self.root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE_ROOT / FIXTURES[index]['name'], destination)
        self.assertEqual(sha256(destination), FIXTURES[index]['source_sha256'])
        return destination

    def assert_no_extra_files(self, *allowed):
        allowed_paths = {Path(path).resolve() for path in allowed}
        actual = {path.resolve() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(actual, allowed_paths, 'Partial output or unexpected temporary file remains')

    def repair_and_preserve(self, source, destination, **kwargs):
        before = sha256(source)
        try:
            return self.engine.repair_file(source, destination, **kwargs)
        finally:
            self.assertEqual(sha256(source), before, 'Input changed during repair')

    def assert_unsupported(self, source):
        output = self.root / 'should-not-exist.mp4'
        result = self.repair_and_preserve(source, output)
        self.assertEqual(result['status'], 'unsupported')
        self.assertTrue(result.get('reason'), 'Rejection should contain a useful reason')
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def new_profile_fixture(self, step=70001, length=17321, key=39):
        # Re-wrap the previously independently decoded, byte-exact video using
        # a profile absent from the shipped cache. This exercises discovery
        # rather than reusing the four production parameters.
        plain = (FIXTURE_ROOT / 'CHARACTER_LOCK_STRICT_fingers_thick_tree_trunk_FIXED.mp4').read_bytes()
        self.assertEqual(hashlib.sha256(plain).hexdigest(), FIXTURES[0]['output_sha256'])
        encoded = bytearray(plain)
        translation = bytes(value ^ key for value in range(256))
        for start in range(0, len(encoded), step):
            end = min(start + length, len(encoded))
            encoded[start:end] = encoded[start:end].translate(translation)
        footer = bytearray((FIXTURE_ROOT / FIXTURES[0]['name']).read_bytes()[-68:])
        footer[-44:-12] = hashlib.sha256(struct.pack('>IIB', step, length, key)).digest()
        source = self.root / 'new-profile.mp4'
        source.write_bytes(encoded + footer)
        return source

    def test_all_four_known_clips_match_verified_lossless_repairs(self):
        for index, fixture in enumerate(FIXTURES):
            with self.subTest(clip=fixture['name']):
                source = self.fixture_copy(index, f'clip-{index}.mp4')
                output = self.root / f'clip-{index}_FIXED.mp4'
                progress = []
                result = self.repair_and_preserve(source, output, progress=progress.append)
                self.assertEqual(result['status'], 'repaired')
                self.assertTrue(output.is_file())
                self.assertEqual(sha256(output), fixture['output_sha256'])
                self.assertEqual(output.stat().st_size, source.stat().st_size - 68)
                self.assertTrue(progress)
                self.assertTrue(all(0 <= value <= 1 for value in progress))
                self.assertEqual(progress[-1], 1.0)

    def test_uncached_profile_is_discovered_and_restored_exactly(self):
        source = self.new_profile_fixture()
        output = self.root / 'new-profile_FIXED.mp4'
        result = self.repair_and_preserve(source, output)
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['profile'], {'step': 70001, 'length': 17321, 'key': 39})
        self.assertEqual(sha256(output), FIXTURES[0]['output_sha256'])
        self.assert_no_extra_files(source, output)

    def test_unknown_profile_discovery_can_be_canceled_without_output(self):
        source = self.new_profile_fixture(step=100003, length=17321, key=40)
        output = self.root / 'solver-canceled.mp4'
        cancel = threading.Event()
        observed = []

        def progress(fraction):
            observed.append(fraction)
            if 0.05 <= fraction < 0.35:
                cancel.set()

        with self.assertRaises(self.engine.CancelledError):
            self.repair_and_preserve(source, output, progress=progress, cancel=cancel)
        self.assertTrue(any(0.05 <= value < 0.35 for value in observed))
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_unicode_paths_and_same_directory_output(self):
        source = self.fixture_copy(name='Th\u1eed nghi\u1ec7m \u2013 C\u1ea3nh 2/\u0110\u1ea7u v\u00e0o g\u00e0 con.mp4')
        output = source.with_name('\u0110\u1ea7u v\u00e0o g\u00e0 con_FIXED.mp4')
        result = self.repair_and_preserve(source, output)
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(sha256(output), FIXTURES[0]['output_sha256'])
        self.assert_no_extra_files(source, output)

    def test_existing_destination_is_never_overwritten(self):
        source = self.fixture_copy()
        output = self.root / 'already-exists.mp4'
        sentinel = b'Do not overwrite this existing user file.\x00\xff'
        output.write_bytes(sentinel)
        with self.assertRaises(FileExistsError):
            self.repair_and_preserve(source, output)
        self.assertEqual(output.read_bytes(), sentinel)
        self.assert_no_extra_files(source, output)

    def test_source_as_destination_is_rejected_without_change(self):
        source = self.fixture_copy()
        with self.assertRaises(ValueError):
            self.repair_and_preserve(source, source)
        self.assert_no_extra_files(source)

    def test_healthy_mp4_is_skipped_without_output_copy(self):
        source = self.root / 'healthy.mp4'
        shutil.copyfile(FIXTURE_ROOT / 'videos' / 'can_v011_CharacterL.mp4', source)
        output = self.root / 'unnecessary-copy.mp4'
        result = self.repair_and_preserve(source, output)
        self.assertEqual(result['status'], 'skipped_healthy')
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_empty_file_is_rejected_without_output(self):
        source = self.root / 'empty.mp4'
        source.write_bytes(b'')
        self.assert_unsupported(source)

    def test_truncated_footer_is_rejected_without_output(self):
        source = self.fixture_copy()
        with source.open('r+b') as handle:
            handle.truncate(source.stat().st_size - 20)
        self.assert_unsupported(source)

    def test_malformed_footer_size_is_rejected_without_output(self):
        source = self.fixture_copy()
        with source.open('r+b') as handle:
            handle.seek(-4, 2)
            handle.write(struct.pack('>I', 0x7FFFFFFF))
        self.assert_unsupported(source)

    def test_unknown_cryptor_is_rejected_without_output(self):
        source = self.fixture_copy()
        with source.open('r+b') as handle:
            handle.seek(-52, 2)
            handle.write(struct.pack('>I', 99))
        self.assert_unsupported(source)

    def test_corrupt_container_with_valid_profile_cannot_publish_mp4(self):
        source = self.fixture_copy()
        output = self.root / 'invalid-container.mp4'
        with source.open('r+b') as handle:
            handle.seek(0)
            handle.write(bytes(value ^ 123 for value in struct.pack('>I', 5)))
        with self.assertRaises(self.engine.RepairError):
            self.repair_and_preserve(source, output)
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_cancel_before_work_leaves_no_output(self):
        source = self.fixture_copy()
        output = self.root / 'canceled-before.mp4'
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(self.engine.CancelledError):
            self.repair_and_preserve(source, output, cancel=cancel)
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_cancel_during_write_removes_partial_output(self):
        source = self.fixture_copy()
        output = self.root / 'canceled-mid-write.mp4'
        cancel = threading.Event()
        observed = []

        def on_progress(fraction):
            observed.append(fraction)
            if 0.40 <= fraction < 0.90:
                cancel.set()

        with self.assertRaises(self.engine.CancelledError):
            self.repair_and_preserve(source, output, progress=on_progress, cancel=cancel)
        self.assertTrue(any(0.40 <= value < 0.90 for value in observed), 'Cancellation did not reach write phase')
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_verifier_receives_complete_unpublished_candidate(self):
        source = self.fixture_copy()
        output = self.root / 'verified.mp4'
        inspected = []

        def verify(candidate):
            self.assertFalse(output.exists(), 'Destination was published before verification')
            self.assertNotEqual(candidate.resolve(), output.resolve())
            self.assertEqual(sha256(candidate), FIXTURES[0]['output_sha256'])
            inspected.append(candidate)
            return {'decoder': 'test verification', 'passed': True}

        result = self.repair_and_preserve(source, output, verify=verify)
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['verification'], {'decoder': 'test verification', 'passed': True})
        self.assertEqual(len(inspected), 1)
        self.assertFalse(inspected[0].exists(), 'Temporary candidate remained after publication')
        self.assert_no_extra_files(source, output)

    def test_verifier_failure_removes_candidate_and_never_publishes(self):
        source = self.fixture_copy()
        output = self.root / 'decoder-rejected.mp4'

        class DecoderRejected(RuntimeError):
            pass

        def reject(candidate):
            self.assertTrue(candidate.is_file())
            raise DecoderRejected('Injected decoder rejection')

        with self.assertRaises(DecoderRejected):
            self.repair_and_preserve(source, output, verify=reject)
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_cancel_after_verification_never_publishes(self):
        source = self.fixture_copy()
        output = self.root / 'canceled-after-verification.mp4'
        cancel = threading.Event()

        def verify(candidate):
            self.assertEqual(sha256(candidate), FIXTURES[0]['output_sha256'])
            cancel.set()
            return {'passed': True}

        with self.assertRaises(self.engine.CancelledError):
            self.repair_and_preserve(source, output, verify=verify, cancel=cancel)
        self.assertFalse(output.exists())
        self.assert_no_extra_files(source)

    def test_destination_created_during_repair_is_never_overwritten(self):
        source = self.fixture_copy()
        output = self.root / 'arrived-during-repair.mp4'
        sentinel = b'A different process created this file during repair.'

        def concurrent_creation(candidate):
            self.assertFalse(output.exists())
            with output.open('xb') as handle:
                handle.write(sentinel)
            return {'passed': True}

        with self.assertRaises(FileExistsError):
            self.repair_and_preserve(source, output, verify=concurrent_creation)
        self.assertEqual(output.read_bytes(), sentinel)
        self.assert_no_extra_files(source, output)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Discovery, naming and headless orchestration regression coverage."""
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


TEST_ROOT = Path(__file__).resolve().parent
WORK_ROOT = TEST_ROOT.parent
SOURCE_ROOT = Path(os.environ.get('CAPCUT_TOOL_SOURCE', str(WORK_ROOT / 'src')))
sys.path.insert(0, str(SOURCE_ROOT))
import batch_jobs
import video_fix


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='discovery-', dir=TEST_ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT))
        self.addCleanup(self.temporary.cleanup)
        self.input = self.root / 'input'
        self.input.mkdir()

    def video(self, relative, payload=b'fixture'):
        path = self.input / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_recursive_discovers_supported_extensions_at_all_levels(self):
        expected = {
            self.video('a.MP4'), self.video('sub/b.mov'), self.video('sub/deep/c.m4v'),
            self.video('other/d.mkv'), self.video('other/e.tmp'),
        }
        self.video('ignore.txt')
        jobs, issues = batch_jobs.collect_jobs([self.input])
        self.assertFalse(issues)
        self.assertEqual({job.source for job in jobs}, expected)
        self.assertEqual(len({job.destination for job in jobs}), len(expected))

    def test_previous_output_folders_are_not_rescanned_case_insensitively(self):
        original = self.video('original.mp4')
        for folder in ['_PHUC_HOI_CAPCUT', '_fixed', 'H264_compatible', 'sub/_PhUc_HoI_CaPcUt']:
            self.video(folder + '/repaired.mp4')
        jobs, issues = batch_jobs.collect_jobs([self.input])
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [original])

    def test_explicit_output_folder_is_excluded_even_with_custom_name(self):
        original = self.video('original.mp4')
        previous = self.video('sub/Result elsewhere/previous.mp4')
        jobs, issues = batch_jobs.collect_jobs([self.input], output_dir=previous.parent)
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [original])

    def test_nonrecursive_scans_only_direct_files(self):
        original = self.video('original.mp4')
        self.video('sub/deep.mp4')
        jobs, issues = batch_jobs.collect_jobs([self.input], recursive=False)
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [original])

    def test_explicit_file_is_processed_when_nonrecursive(self):
        nested = self.video('sub/deep.mp4')
        jobs, issues = batch_jobs.collect_jobs([nested], recursive=False)
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [nested])

    def test_overlapping_folder_file_and_duplicate_inputs_are_deduplicated(self):
        outer = self.video('outer.mp4')
        nested = self.video('sub/deep.mp4')
        jobs, issues = batch_jobs.collect_jobs([nested.parent, self.input, nested, self.input])
        self.assertFalse(issues)
        self.assertEqual({job.source for job in jobs}, {outer, nested})
        self.assertEqual(len(jobs), 2)

    def test_same_stem_different_extensions_never_share_destination(self):
        files = [self.video('same.mp4'), self.video('same.mov'), self.video('same.mkv')]
        jobs, issues = batch_jobs.collect_jobs(files)
        self.assertFalse(issues)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(len({os.path.normcase(str(job.destination)) for job in jobs}), 3)

    def test_output_identity_is_stable_across_order_and_subsets(self):
        mov, mp4 = self.video('same.mov'), self.video('same.mp4')
        output = self.root / 'out'
        first, _ = batch_jobs.collect_jobs([mov, mp4], output_dir=output)
        second, _ = batch_jobs.collect_jobs([mp4, mov], output_dir=output)
        single, _ = batch_jobs.collect_jobs([mp4], output_dir=output)
        first_map = {job.source: job.destination for job in first}
        second_map = {job.source: job.destination for job in second}
        self.assertEqual(first_map, second_map, 'Input order changed which source owns an output filename')
        self.assertEqual(first_map[mp4], single[0].destination, 'Subset rerun points to the other source output')

    def test_unicode_paths_keep_subfolder_layout(self):
        source = self.video('C\u1ea3nh 2/\u0110\u1ea7u v\u00e0o \u2013 g\u00e0 con.MP4')
        jobs, issues = batch_jobs.collect_jobs([self.input])
        self.assertFalse(issues)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, source)
        self.assertEqual(jobs[0].destination.parent.name, 'C\u1ea3nh 2')
        self.assertEqual(jobs[0].output_root, self.input / batch_jobs.OUTPUT_NAME)

    def test_file_and_directory_symlinks_are_not_followed_when_scanning(self):
        original = self.video('original.mp4')
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / 'hidden.mp4'
        target.write_bytes(b'hidden')
        try:
            (self.input / 'linked-folder').symlink_to(outside, target_is_directory=True)
            (self.input / 'linked.mp4').symlink_to(target)
        except OSError as error:
            self.skipTest(f'Symlink privilege unavailable: {error}')
        jobs, issues = batch_jobs.collect_jobs([self.input])
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [original])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_windows_junction_is_not_followed(self):
        original = self.video('original.mp4')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'hidden.mp4').write_bytes(b'hidden')
        junction = self.input / 'linked-junction'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                                capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            self.skipTest('Cannot create local test junction: ' + result.stderr.decode(errors='replace'))
        self.assertTrue(junction.is_junction())
        jobs, issues = batch_jobs.collect_jobs([self.input])
        self.assertFalse(issues)
        self.assertEqual([job.source for job in jobs], [original])

    def test_already_canceled_discovery_returns_no_jobs(self):
        self.video('original.mp4')
        stop = threading.Event()
        stop.set()
        jobs, issues = batch_jobs.collect_jobs([self.input], cancel=stop)
        self.assertEqual(jobs, [])
        self.assertEqual(issues, [])


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='orchestration-', dir=TEST_ROOT)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(TEST_ROOT))
        self.addCleanup(self.temporary.cleanup)

    def jobs(self, count=1):
        result = []
        for index in range(count):
            source = self.root / f'clip-{index}.mp4'
            source.write_bytes(b'input')
            output_root = self.root / '_PHUC_HOI_CAPCUT'
            result.append(batch_jobs.Job(source, output_root / f'clip-{index}_FIXED.mp4', output_root))
        return result

    def test_repair_full_check_is_supplied_before_publication(self):
        jobs = self.jobs()
        stop = threading.Event()
        events = []
        fake_ffmpeg = Path('fake-ffmpeg.exe')
        candidate = self.root / 'candidate.bdve-part'

        def repair(source, destination, **kwargs):
            self.assertEqual(kwargs['verify'](candidate), {'full_decode': True})
            return {'status': 'repaired', 'verification': {'full_decode': True}}

        with patch.object(video_fix, 'find_ffmpeg', return_value=fake_ffmpeg), \
             patch.object(video_fix, 'verify_playback', return_value={'full_decode': True}) as verify, \
             patch.object(video_fix, 'repair_file', side_effect=repair):
            records, roots = video_fix.run_jobs(jobs, stop, lambda *event: events.append(event), check=True)
        verify.assert_called_once_with(candidate, fake_ffmpeg, stop)
        self.assertEqual(records[0]['status'], 'repaired')
        self.assertTrue(records[0]['full_decode_checked'])
        self.assertEqual(roots, [jobs[0].output_root])
        self.assertIn(('verification', True), events)

    def test_healthy_input_decode_failure_is_reported_as_error(self):
        jobs = self.jobs()
        with patch.object(video_fix, 'find_ffmpeg', return_value=Path('fake.exe')), \
             patch.object(video_fix, 'repair_file', return_value={'status': 'skipped_healthy'}), \
             patch.object(video_fix, 'verify_playback', side_effect=RuntimeError('decoder failed')):
            records, roots = video_fix.run_jobs(jobs, threading.Event(), lambda *event: None)
        self.assertEqual(records[0]['status'], 'error')
        self.assertIn('decoder failed', records[0]['reason'])
        self.assertEqual(roots, [])

    def test_missing_decoder_is_reported_without_claiming_full_decode(self):
        jobs = self.jobs()
        events = []
        with patch.object(video_fix, 'find_ffmpeg', return_value=None), \
             patch.object(video_fix, 'repair_file', return_value={'status': 'repaired'}) as repair:
            records, _ = video_fix.run_jobs(jobs, threading.Event(), lambda *event: events.append(event), check=True)
        self.assertIsNone(repair.call_args.kwargs['verify'])
        self.assertFalse(records[0]['full_decode_checked'])
        self.assertIn(('verification', False), events)

    def test_one_failed_job_does_not_stop_remaining_jobs(self):
        jobs = self.jobs(3)
        with patch.object(video_fix, 'repair_file', side_effect=[RuntimeError('first failed'),
                          {'status': 'skipped_healthy'}, {'status': 'repaired'}]):
            records, roots = video_fix.run_jobs(jobs, threading.Event(), lambda *event: None, check=False)
        self.assertEqual([record['status'] for record in records], ['error', 'skipped_healthy', 'repaired'])
        self.assertEqual(roots, [jobs[2].output_root])

    def test_canceled_repair_stops_remaining_jobs(self):
        jobs = self.jobs(3)
        with patch.object(video_fix, 'repair_file', side_effect=video_fix.CancelledError('stop')) as repair:
            records, roots = video_fix.run_jobs(jobs, threading.Event(), lambda *event: None, check=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'cancelled')
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(roots, [])

    def test_headless_real_batch_saves_correct_summary_and_report(self):
        input_root = self.root / '\u0110\u1ea7u v\u00e0o'
        input_root.mkdir()
        fixture_root = Path(os.environ.get('VIDEOFIX_FIXTURES', str(WORK_ROOT / 'fixtures'))) / 'canh2-batch-input'
        encrypted = next(path for path in fixture_root.glob('*.mp4')
                         if path.name.endswith('_fingers_thick_tree_trunk.mp4'))
        shutil.copyfile(encrypted, input_root / 'encrypted.mp4')
        shutil.copyfile(fixture_root / 'videos' / 'can_v011_CharacterL.mp4', input_root / 'healthy.mp4')
        (input_root / 'unsupported.tmp').write_bytes(b'not a supported media file')
        output = self.root / 'output'
        args = SimpleNamespace(paths=[str(input_root)], no_recursive=False,
                               output_dir=str(output), no_verify=True, report=True)
        capture = io.StringIO()
        with redirect_stdout(capture):
            exit_code = video_fix.headless(args)
        summary = json.loads(capture.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual((summary['files'], summary['repaired'], summary['errors'], summary['skipped']), (3, 1, 1, 1))
        self.assertEqual(len(summary['reports']), 1)
        report = json.loads(Path(summary['reports'][0]).read_text(encoding='utf-8'))
        self.assertEqual(len(report['records']), 3)
        self.assertEqual({record['status'] for record in report['records']}, {'repaired', 'unsupported', 'skipped_healthy'})
        planned, _ = batch_jobs.collect_jobs([input_root], output_dir=output)
        expected = next(job.destination for job in planned if job.source.name == 'encrypted.mp4')
        self.assertEqual(list(output.rglob('*.mp4')), [expected])
        self.assertFalse(list(output.rglob('*.bdve-part')))


if __name__ == '__main__':
    unittest.main(verbosity=2)

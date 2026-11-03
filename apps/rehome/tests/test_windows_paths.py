from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rehome_windows_paths as paths
import rehome_apply_transaction as transaction
import snapshot
from tests import test_native_apply as fixtures


class WindowsPathTests(unittest.TestCase):
    def test_link_is_rejected_before_walking_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            outside=root/'outside';outside.mkdir()
            try:
                (source/'link').symlink_to(outside,target_is_directory=True)
            except OSError:
                self.skipTest('Creating a test symlink is unavailable')
            with patch.object(paths,'legacy_limits_required',return_value=True):
                with self.assertRaises(snapshot.SnapshotError) as caught:
                    paths.tree(source,root/'future')
            self.assertEqual('LINK_PATH',caught.exception.code)
    def test_legacy_file_directory_and_utf16_limits(self):
        prefix=str(Path.cwd().anchor)
        with patch.object(paths, 'legacy_limits_required', return_value=True):
            paths.check(prefix+'x'*(259-len(prefix)))
            with self.assertRaises(paths.PathLengthError):
                paths.check(prefix+'x'*(260-len(prefix)))
            paths.check(prefix+'x'*(248-len(prefix)), directory=True)
            with self.assertRaises(paths.PathLengthError):
                paths.check(prefix+'x'*(249-len(prefix)), directory=True)
            with self.assertRaises(paths.PathLengthError):
                paths.check(prefix+'x'*(258-len(prefix))+'\U0001f680')

    def test_long_future_rollback_path_blocks_before_transaction_creation(self):
        fixture=fixtures.NativeApplyTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        candidate=fixture.candidate()
        job=fixture.root/'Transaction'
        original=paths.tree
        def check(source,destination):
            if 'Changed' in Path(destination).parts:
                raise paths.PathLengthError('Synthetic long Changed target')
            return original(source,destination)
        with patch.object(paths,'tree',side_effect=check):
            with self.assertRaises(paths.PathLengthError):
                transaction.prepare(candidate['candidate'],job,
                    expected_apply_digest=candidate['apply_digest'],guard=lambda:None)
        self.assertFalse(job.exists())
        self.assertEqual(b'ORIGINAL_STATE',(fixture.target/'state_5.sqlite').read_bytes())


if __name__=='__main__':unittest.main()

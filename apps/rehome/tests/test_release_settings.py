import json
from pathlib import Path
import tempfile
import unittest

import rehome_backup_workflow as workflow
import rehome_backup_module as backup


class ReleaseSettingsTests(unittest.TestCase):
    def test_selected_backup_folder_persists_and_profile_is_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);profile=root/'.codex';profile.mkdir()
            local=root/'Operations';local.mkdir();destination=root/'Backups';destination.mkdir()
            original=profile/'state_5.sqlite';original.write_bytes(b'LOCAL_STATE')
            settings={'source_profile':str(profile),'local_root':str(local),'backup_root':str(root/'Old'),
                      'manual_choice':'keep'}
            path=root/'settings.json';path.write_text(json.dumps(settings),encoding='utf-8')
            updated=workflow.set_backup_folder(settings,destination,path)
            self.assertEqual(str(destination),updated['backup_root'])
            self.assertEqual(updated,json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(b'LOCAL_STATE',original.read_bytes())
            before=path.read_bytes()
            for forbidden in (profile,local,root):
                with self.assertRaises(ValueError):workflow.set_backup_folder(updated,forbidden,path)
                self.assertEqual(before,path.read_bytes())

    def test_missing_or_changed_runtime_has_fixed_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe=Path(temporary)/'codex.exe'
            settings={'runtime_version':backup.SUPPORTED_RUNTIME,'runtime_executable':str(exe),
                      'runtime_sha256':'0'*64}
            with self.assertRaises(workflow.WorkflowError) as missing:workflow._runtime(settings)
            self.assertEqual('codex_runtime_unavailable',missing.exception.code)
            exe.write_bytes(b'SYNTHETIC_PRIVATE_PAYLOAD')
            with self.assertRaises(workflow.WorkflowError) as changed:workflow._runtime(settings)
            self.assertEqual('codex_runtime_unverified',changed.exception.code)
            self.assertNotIn('SYNTHETIC_PRIVATE_PAYLOAD',changed.exception.public_message)


if __name__=='__main__':unittest.main()

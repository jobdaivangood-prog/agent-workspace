"""In-process evidence for an exact private native snapshot after quiet guards.

No source status is edited. This capability is issued by the backup workflow,
never loaded from a user-supplied JSON file. Only its hash/count/scope summary
is portable. The named-process guard is not a universal no-writer claim.
"""
from pathlib import Path
import hashlib
import os
import stat

import snapshot

_ISSUER=object()
FORMAT='rehome-closed-source-snapshot/1'
SCOPE='named_codex_processes_and_observed_descendants'


def _manifest(root):
    snapshot._safe_path(root)
    files={};directories=[];pending=[root];count=0
    while pending:
        directory=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count+=1
                if count>100000:raise ValueError('Closed snapshot entry limit exceeded')
                path=snapshot._safe_path(Path(entry.path));info=path.lstat()
                relative=path.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):directories.append(relative);pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                    digest,size=snapshot._hash_file(path);files[relative]={'sha256':digest,'bytes':size}
                else:raise ValueError('Closed snapshot contains an unsupported entry')
    allowed={'state_5.sqlite','thread_history_1.sqlite','.codex-global-state.json'}
    if not allowed<=set(files) or any(name not in allowed and not name.startswith(('sessions/','archived_sessions/')) for name in files):
        raise ValueError('Closed snapshot has unexpected native components')
    return {'files':files,'directories':sorted(directories)}


class ClosedSnapshot:
    def __init__(self,issuer,root,manifest):
        if issuer is not _ISSUER:raise ValueError('Closed snapshots must be issued by the workflow')
        self.root=root;self.manifest=manifest
        info=root.lstat();self.identity=(info.st_dev,info.st_ino)
        self.digest=hashlib.sha256(snapshot._canonical(manifest)).hexdigest()

    def assert_root(self,root):
        if Path(root).absolute()!=self.root:raise ValueError('Closed snapshot belongs to another profile')

    def verify(self,root):
        self.assert_root(root)
        info=self.root.lstat()
        if (info.st_dev,info.st_ino)!=self.identity or _manifest(self.root)!=self.manifest or hashlib.sha256(snapshot._canonical(self.manifest)).hexdigest()!=self.digest:
            raise ValueError('Closed snapshot changed after sealing')

    def summary(self):
        return {'format':FORMAT,'manifest_sha256':self.digest,'file_count':len(self.manifest['files']),
                'quiescence_scope':SCOPE,'scope':'private_native_profile_snapshot'}


def seal(root,*,original_profile,guard):
    root=Path(root).absolute();original=Path(original_profile).absolute()
    if (root==original or root.is_relative_to(original) or original.is_relative_to(root) or
            any(p.casefold()=='.codex' for p in root.parts)):
        raise ValueError('Closed snapshot must be a separate private profile')
    guard();manifest=_manifest(root);guard()
    return ClosedSnapshot(_ISSUER,root,manifest)

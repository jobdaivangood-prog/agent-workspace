"""Read-only legacy Windows path preflight; never changes system policy.

Limits follow Microsoft Learn, maximum-file-path-limitation: MAX_PATH includes
the terminating NUL; legacy directory creation reserves an 8.3 filename.
https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation
"""
from functools import lru_cache
import os
from pathlib import Path
import snapshot


class PathLengthError(ValueError):
    code = 'windows_path_too_long'


@lru_cache(maxsize=1)
def legacy_limits_required():
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            return winreg.QueryValueEx(key, 'LongPathsEnabled')[0] != 1
    except OSError:
        return True


def check(path, *, directory=False):
    if not legacy_limits_required():
        return
    path = Path(os.path.abspath(path))
    # Count Windows WCHARs, including surrogate pairs, rather than codepoints.
    units = len(str(path).encode('utf-16-le')) // 2
    if units > (248 if directory else 259):
        raise PathLengthError('A planned Windows path exceeds the legacy limit')
    for parent in path.parents:
        if len(str(parent).encode('utf-16-le')) // 2 > 248:
            raise PathLengthError('A planned Windows directory exceeds the legacy limit')


def tree(source, destination):
    """Check known descendants at a future rename target without reading bytes."""
    if not legacy_limits_required():
        return
    source = snapshot._safe_path(Path(source))
    destination = snapshot._safe_path(Path(destination),allow_missing=True)
    check(destination, directory=source.is_dir())
    if source.is_dir():
        pending=[source];count=0
        while pending:
            current=pending.pop()
            for child in current.iterdir():
                count+=1
                if count>100000:
                    raise ValueError('Windows path preflight entry limit exceeded')
                child=snapshot._safe_path(child)
                directory=child.is_dir()
                check(destination / child.relative_to(source),directory=directory)
                if directory:
                    pending.append(child)

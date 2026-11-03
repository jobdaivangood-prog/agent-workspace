"""Separate, externally pinned local Python runtime envelope (not program ZIP v1).

Run verification with an already trusted interpreter. A runtime cannot establish
trust in the interpreter which is currently executing its own verifier.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

FORMAT = 'rehome-python-runtime/1'
MAX_FILES = 4096
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_ARCHIVE = 96 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
PYDS = ('_asyncio', '_bz2', '_ctypes', '_decimal', '_elementtree', '_lzma',
        '_multiprocessing', '_overlapped', '_queue', '_socket', '_sqlite3',
        '_tkinter', '_uuid', '_wmi', '_zoneinfo', 'pyexpat', 'select',
        'unicodedata', 'winsound')
SYSTEM_DLLS = frozenset(x.lower() for x in ('KERNEL32.dll', 'ADVAPI32.dll',
    'USER32.dll', 'GDI32.dll', 'SHELL32.dll', 'OLE32.dll', 'OLEAUT32.dll',
    'WS2_32.dll', 'VERSION.dll', 'SHLWAPI.dll', 'COMCTL32.dll', 'COMDLG32.dll',
    'RPCRT4.dll', 'CRYPT32.dll', 'BCRYPT.dll', 'NTDLL.dll', 'USERENV.dll',
    'NETAPI32.dll', 'IPHLPAPI.dll', 'MSVCRT.dll', 'WINMM.dll', 'IMM32.dll',
    'UXTHEME.dll', 'DWMAPI.dll', 'WTSAPI32.dll', 'PROPSYS.dll'))
EXCLUDE = frozenset(('site-packages', '__pycache__', 'test', 'tests', 'idlelib',
                     'ensurepip', 'turtledemo', 'venv'))
PTH = b'python312.zip\n.\nDLLs\n# isolated: no import site\n'
REQUIRED = frozenset(('python.exe', 'pythonw.exe', 'python3.dll', 'python312.dll',
    'python312.zip', 'python312._pth', 'LICENSE.txt', 'tcl/tcl8.6/init.tcl',
    'tcl/tcl8.6/tclIndex', 'tcl/tk8.6/tk.tcl', 'licenses/cpython-LICENSE.txt',
    'licenses/tcl-license.terms', 'licenses/tk-license.terms',
    *(f'DLLs/{name}.pyd' for name in PYDS)))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    if not isinstance(name, str) or len(name) > 240 or '\\' in name or ':' in name:
        raise ValueError('Unsafe runtime path')
    parts = name.split('/')
    if not parts or any(p in ('', '.', '..') or p.endswith(('.', ' ')) or
            not re.fullmatch(r'[A-Za-z0-9_.+ -]+', p) or
            p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
            for p in parts):
        raise ValueError('Unsafe runtime path')
    return name


def regular(path, limit=MAX_FILE):
    path = Path(path)
    # Check every ancestor too: lstat on a child alone follows a junction parent.
    for part in (path, *path.parents):
        st = part.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Runtime links/reparse points forbidden')
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > limit:
        raise ValueError('Runtime requires bounded plain single-link files')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError('Runtime file identity changed')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != st.st_size or len(data) > limit or
            (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns)):
        raise ValueError('Runtime file changed/oversize')
    return data


def pe_imports(data):
    """Read ordinary and delay-load PE imports; reject malformed/non-x64 input."""
    try:
        pe = struct.unpack_from('<I', data, 60)[0]
        if data[:2] != b'MZ' or data[pe:pe + 4] != b'PE\0\0':
            raise ValueError('Not PE')
        machine, count = struct.unpack_from('<HH', data, pe + 4)
        size = struct.unpack_from('<H', data, pe + 20)[0]
        opt = pe + 24
        if machine != 0x8664 or struct.unpack_from('<H', data, opt)[0] != 0x20b:
            raise ValueError('Runtime requires x64 PE32+')
        sections = [struct.unpack_from('<IIII', data, opt + size + i * 40 + 8)
                    for i in range(count)]
        def offset(rva):
            for virtual_size, virtual, raw_size, raw in sections:
                if virtual <= rva < virtual + min(max(virtual_size, raw_size), raw_size):
                    return raw + rva - virtual
            raise ValueError('Invalid PE RVA')
        def cstring(rva):
            start = offset(rva)
            end = data.find(b'\0', start, start + 256)
            if end < 0: raise ValueError('Invalid PE name')
            name = data[start:end].decode('ascii')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+\.dll', name, re.I):
                raise ValueError('Invalid PE DLL import')
            return name.lower()
        result = set()
        for index, width, namefield in ((1, 20, 12), (13, 32, 4)):
            rva, length = struct.unpack_from('<II', data, opt + 112 + index * 8)
            if not rva: continue
            start = offset(rva)
            for position in range(start, start + length, width):
                block = data[position:position + width]
                if len(block) != width: raise ValueError('Truncated imports')
                if not any(block): break
                if index == 13 and struct.unpack_from('<I', block)[0] != 1:
                    raise ValueError('Unsupported delay import addressing')
                result.add(cstring(struct.unpack_from('<I', block, namefield)[0]))
            else: raise ValueError('Unterminated PE imports')
        return sorted(result)
    except (struct.error, UnicodeError, IndexError) as exc:
        raise ValueError('Malformed PE imports') from exc


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def zip_read(archive, name, limit):
    with archive.open(name) as stream:
        value = stream.read(limit + 1)
    if len(value) > limit: raise ValueError('ZIP bounded read')
    return value


def verify_stdlib(data):
    """The nested stdlib ZIP has its own strict uncompressed budget."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= MAX_FILES: raise ValueError('Stdlib file count')
        seen, total = set(), 0
        for info in infos:
            name = safe_name(info.filename)
            mode = info.external_attr >> 16
            if (name.casefold() in seen or info.is_dir() or info.flag_bits & 1 or
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG) or not name.endswith('.py') or
                    any(p.lower() in EXCLUDE for p in name.split('/'))):
                raise ValueError('Invalid stdlib member')
            seen.add(name.casefold())
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_TOTAL: raise ValueError('Stdlib size')
            # Force decompression and CRC validation before interpreter execution.
            if len(zip_read(archive, info, MAX_FILE)) != info.file_size: raise ValueError('Stdlib byte count')
    return len(infos)


def build(source, destination, licenses, *, python_version='3.12.14', tcl_tk='8.6.12', public_upstream=False):
    """Build in a fresh owned directory. Source is an explicitly trusted local install."""
    source, destination, licenses = Path(source), Path(destination), Path(licenses)
    if (python_version,tcl_tk,public_upstream) not in {
            ('3.12.14','8.6.12',False),('3.12.16','8.6.15',True)}:
        raise ValueError('Runtime version requires an explicit reviewed build route')
    if destination.exists(): raise ValueError('Runtime output must be fresh')
    for p in (destination.parent, *destination.parents):
        if not p.exists(): continue
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Runtime build destination link')
    files = {}
    def add(name, data):
        safe_name(name)
        if name.casefold() in {p.casefold() for p in files}: raise ValueError('Case collision')
        if len(data) > MAX_FILE: raise ValueError('Runtime file oversize')
        files[name] = data
    stdlib = io.BytesIO()
    with zipfile.ZipFile(stdlib, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((source / 'Lib').rglob('*.py')):
            relative = path.relative_to(source / 'Lib')
            if any(p.lower() in EXCLUDE for p in relative.parts): continue
            archive.writestr(relative.as_posix(), regular(path))
    add('python312.zip', stdlib.getvalue())
    verify_stdlib(files['python312.zip'])
    add('python312._pth', PTH)
    imports = {}
    pending = [source / n for n in ('python.exe', 'pythonw.exe', 'python312.dll', 'python3.dll')]
    pending += [source / 'DLLs' / (n + '.pyd') for n in PYDS]
    candidates = {p.name.lower(): p for root in (source, source / 'DLLs')
                  for p in root.iterdir() if p.suffix.lower() in ('.dll', '.pyd', '.exe')}
    while pending:
        path = pending.pop(0)
        name = path.relative_to(source).as_posix()
        if name in files: continue
        data = regular(path)
        add(name, data)
        imports[name] = pe_imports(data)
        for dependency in imports[name]:
            if dependency in candidates:
                pending.append(candidates[dependency])
            elif dependency not in SYSTEM_DLLS and not dependency.startswith(('api-ms-win-', 'ext-ms-win-')):
                raise ValueError('Unclassified native dependency: ' + dependency)
    for directory in ('tcl8.6', 'tk8.6', 'tcl8'):
        for path in sorted((source / 'tcl' / directory).rglob('*')):
            if path.is_dir():
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Tcl linked directory')
                continue
            if any(part.lower() in ('tests', 'test', 'demos') for part in path.relative_to(source / 'tcl').parts):
                continue
            if path.name.startswith('tcltest-'):
                continue
            if path.suffix.lower() not in ('', '.tcl', '.tm', '.enc', '.msg', '.txt', '.terms', '.gif', '.xbm', '.png'):
                continue
            add(path.relative_to(source).as_posix(), regular(path))
    add('LICENSE.txt', regular(source / 'LICENSE.txt'))
    for name in ('cpython-LICENSE.txt', 'tcl-license.terms', 'tk-license.terms'):
        add('licenses/' + name, regular(licenses / name))
    if (licenses / 'source-evidence.json').exists():
        add('licenses/source-evidence.json', regular(licenses / 'source-evidence.json'))
    if public_upstream:
        # The release builder first authenticates the upstream archive and
        # supplies its complete native-component notice directory.
        for path in sorted(licenses.iterdir()):
            relative='licenses/'+path.name
            if path.is_file() and relative not in files:
                add(relative,regular(path))
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError('Runtime bounds exceeded')
    manifest = {'format': FORMAT, 'platform': 'windows-x86_64', 'python': python_version,
        'tcl_tk': tcl_tk, 'isolation': 'python312._pth; no site',
        'redistribution': ('verified upstream archives; complete third-party notices included' if public_upstream else
                          'unverified; local testing only; native provenance/license audit pending'),
        'native_imports': imports, 'system_dll_policy': sorted(SYSTEM_DLLS),
        'files': [{'path': name, 'size': len(data), 'sha256': digest(data)}
                  for name, data in sorted(files.items())]}
    raw = _json(manifest)
    destination.mkdir(parents=True)
    runtime = destination / 'Runtime'
    runtime.mkdir()
    for name, data in files.items():
        target = runtime.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (runtime / 'runtime-manifest.json').write_bytes(raw)
    archive = destination / 'python-runtime.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for name in sorted(files): zipped.writestr(name, files[name])
        zipped.writestr('runtime-manifest.json', raw)
    archive_data = regular(archive, MAX_ARCHIVE)
    result = {'format': FORMAT, 'archive': str(archive), 'archive_sha256': digest(archive_data),
        'manifest_sha256': digest(raw), 'file_count': len(files),
        'total_size': sum(map(len, files.values())), 'archive_size': len(archive_data),
            'redistribution_verified': public_upstream, 'receiver_integrated': False, 'pc2_verified': False}
    (destination / 'build-report.json').write_bytes(_json(result))
    return result


def _manifest(raw, pin):
    if len(raw) > MAX_MANIFEST or digest(raw) != pin: raise ValueError('External manifest pin mismatch')
    value = json.loads(raw)
    fields = {'format','platform','python','tcl_tk','isolation','redistribution',
              'native_imports','system_dll_policy','files'}
    variants={('3.12.14','8.6.12','unverified; local testing only; native provenance/license audit pending'),
              ('3.12.16','8.6.15','verified upstream archives; complete third-party notices included')}
    if (set(value) != fields or value.get('format') != FORMAT or
            value.get('platform') != 'windows-x86_64' or
            (value.get('python'),value.get('tcl_tk'),value.get('redistribution')) not in variants or
            value.get('isolation') != 'python312._pth; no site' or
            value.get('system_dll_policy') != sorted(SYSTEM_DLLS) or
            not isinstance(value.get('native_imports'),dict)):
        raise ValueError('Unsupported runtime format')
    entries = value.get('files')
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('Runtime file count')
    seen, total = set(), 0
    for item in entries:
        if set(item) != {'path','size','sha256'}: raise ValueError('Runtime entry schema')
        name = safe_name(item['path'])
        if name.casefold() in seen or name.casefold() == 'runtime-manifest.json': raise ValueError('Runtime collision')
        seen.add(name.casefold())
        if type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE: raise ValueError('Runtime size')
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']): raise ValueError('Runtime digest')
        total += item['size']
    if total > MAX_TOTAL: raise ValueError('Runtime total size')
    paths = {item['path'] for item in entries}
    if not REQUIRED.issubset(paths): raise ValueError('Runtime required component missing')
    native = value['native_imports']
    expected_native = {p for p in paths if Path(p).suffix.lower() in ('.exe','.dll','.pyd')}
    if not isinstance(native,dict) or set(native) != expected_native:
        raise ValueError('Runtime native import inventory')
    basenames = {Path(p).name.lower() for p in expected_native}
    for imports in native.values():
        if not isinstance(imports,list) or imports != sorted(set(imports)):
            raise ValueError('Runtime import schema')
        for dependency in imports:
            if (not isinstance(dependency,str) or dependency != dependency.lower() or
                    not re.fullmatch(r'[a-z0-9_.-]+\.dll',dependency) or
                    (dependency not in basenames and dependency not in SYSTEM_DLLS and
                     not dependency.startswith(('api-ms-win-','ext-ms-win-')))):
                raise ValueError('Runtime unclassified dependency')
    return value


def verify_tree(root, manifest_sha256):
    """External trusted verification; executes no bundled Python or application code."""
    root = Path(root)
    value = _manifest(regular(root / 'runtime-manifest.json', MAX_MANIFEST), manifest_sha256)
    actual = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            p = Path(directory) / name
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Runtime directory link')
        for name in names:
            p = Path(directory) / name
            regular(p, MAX_MANIFEST if name == 'runtime-manifest.json' else MAX_FILE)
            actual.add(p.relative_to(root).as_posix())
    expected = {item['path'] for item in value['files']} | {'runtime-manifest.json'}
    if actual != expected: raise ValueError('Runtime exact file set mismatch')
    for item in value['files']:
        data = regular(root / item['path'])
        if len(data) != item['size'] or digest(data) != item['sha256']: raise ValueError('Runtime inventory mismatch')
        if item['path'] == 'python312.zip': verify_stdlib(data)
        if item['path'] == 'python312._pth' and data != PTH: raise ValueError('Runtime isolation contract')
    return value


def extract_verified(archive, destination, archive_sha256, manifest_sha256):
    """Bounded verification of external pins and every byte before fresh extraction."""
    data = regular(archive, MAX_ARCHIVE)
    if digest(data) != archive_sha256: raise ValueError('External archive pin mismatch')
    destination = Path(destination)
    if destination.exists(): raise ValueError('Runtime extraction must be fresh')
    # Ancestor chain must be real; no extraction through a junction.
    parent = destination.parent
    for p in (parent, *parent.parents):
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Runtime destination link')
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        infos = zipped.infolist()
        if len(infos) > MAX_FILES + 1: raise ValueError('Runtime archive count')
        names, total = set(), 0
        for info in infos:
            name = safe_name(info.filename)
            mode = info.external_attr >> 16
            if name.casefold() in names or info.is_dir() or info.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('Runtime archive nonregular/collision')
            names.add(name.casefold())
            if info.file_size > (MAX_MANIFEST if name == 'runtime-manifest.json' else MAX_FILE): raise ValueError('Runtime archive size')
            total += info.file_size
        if total > MAX_TOTAL + MAX_MANIFEST: raise ValueError('Runtime archive total')
        value = _manifest(zip_read(zipped, 'runtime-manifest.json', MAX_MANIFEST), manifest_sha256)
        expected = {i['path'] for i in value['files']} | {'runtime-manifest.json'}
        if {i.filename for i in infos} != expected: raise ValueError('Runtime archive exact file set')
        contents = {}
        for item in value['files']:
            body = zip_read(zipped, item['path'], MAX_FILE)
            if len(body) != item['size'] or digest(body) != item['sha256']: raise ValueError('Runtime archive inventory')
            if item['path'] == 'python312.zip': verify_stdlib(body)
            if item['path'] == 'python312._pth' and body != PTH: raise ValueError('Runtime isolation contract')
            contents[item['path']] = body
        contents['runtime-manifest.json'] = zip_read(zipped, 'runtime-manifest.json', MAX_MANIFEST)
    destination.mkdir()
    for name, body in contents.items():
        path = destination.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    verify_tree(destination, manifest_sha256)
    return destination


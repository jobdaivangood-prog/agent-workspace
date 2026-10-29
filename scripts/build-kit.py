"""Build portable ZIPs and three-step install documents from a clean runtime."""
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from rtf_document import render

root = Path(__file__).resolve().parents[1]
version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
output = root / 'output'
runtime = output / ('agent-workspace-' + version + '-win-x64')
runtime_zip = output / (runtime.name + '.zip')
kit = output / 'Agent-Workspace-Setup'
kit_zip = output / 'Agent-Workspace-Setup.zip'
if runtime_zip.exists() or kit.exists() or kit_zip.exists():
    raise RuntimeError('Packaging output already exists; preserve it before rebuilding')

def digest(file):
    value = hashlib.sha256()
    with file.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()

def validate_name(name):
    parts = PurePosixPath(name)
    if parts.is_absolute() or '\\' in name or any(part in ('', '.', '..') for part in parts.parts):
        raise RuntimeError('Unsafe package path: ' + name)

def seal(folder, archive, expected):
    actual = {file.relative_to(folder).as_posix() for file in folder.rglob('*') if file.is_file()}
    if actual != set(expected):
        raise RuntimeError('Unlisted/missing package files')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for name in sorted(expected):
            validate_name(name)
            file = folder / name
            if file.is_symlink():
                raise RuntimeError('Package contains a link')
            zipped.write(file, name)
    with zipfile.ZipFile(archive, 'r') as zipped:
        if set(zipped.namelist()) != set(expected):
            raise RuntimeError('ZIP file list differs')
        for name, entry in expected.items():
            checksum = hashlib.sha256()
            size = 0
            with zipped.open(name) as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(data)
                    size += len(data)
            if size != entry['bytes'] or checksum.hexdigest() != entry['sha256']:
                raise RuntimeError('ZIP content differs: ' + name)
    checksum = digest(archive)
    archive.with_suffix('.zip.sha256').write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    return checksum

manifest_file = runtime / 'checksums.json'
manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
if manifest['version'] != version:
    raise RuntimeError('Runtime version differs')
runtime_files = {}
for entry in manifest['files']:
    validate_name(entry['file'])
    file = runtime / entry['file']
    if file.is_symlink() or not file.is_file() or file.stat().st_size != entry['bytes'] or digest(file) != entry['sha256']:
        raise RuntimeError('Runtime checksum differs: ' + entry['file'])
    runtime_files[entry['file']] = entry
runtime_files['checksums.json'] = {'bytes': manifest_file.stat().st_size, 'sha256': digest(manifest_file)}
runtime_hash = seal(runtime, runtime_zip, runtime_files)

kit.mkdir()
(kit / 'MCP').mkdir()
templates = root / 'packaging/simple'
description = (templates / '0_Описание.md').read_text(encoding='utf-8').replace('__VERSION__', version)
rtf, _ = render(description)
(kit / '0_Описание.rtf').write_bytes(rtf)
for name in ('1_Установка_агента.md', '2_Установка_MCP.md'):
    text = (templates / name).read_text(encoding='utf-8').replace('__VERSION__', version).replace('__RUNTIME_ZIP_SHA256__', runtime_hash)
    if '__VERSION__' in text or '__RUNTIME_ZIP_SHA256__' in text:
        raise RuntimeError('Unresolved installation template')
    (kit / name).write_text(text, encoding='utf-8', newline='\n')
metadata = json.loads((root / 'package.json').read_text(encoding='utf-8'))
code_base = metadata['repository']['url'].removesuffix('.git') + '/blob/v' + version + '/'
passport = re.sub(r'\]\(((?:src|native|packaging|scripts)/[^)]+)\)', lambda match: '](' + code_base + match.group(1) + ')', (root / 'ainfo.md').read_text(encoding='utf-8'))
(kit / 'MCP/ainfo.md').write_text(passport, encoding='utf-8', newline='\n')
with runtime_zip.open('rb') as source, (kit / 'MCP' / runtime_zip.name).open('xb') as destination:
    for data in iter(lambda: source.read(1024 * 1024), b''):
        destination.write(data)
files = [{'file': file.relative_to(kit).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)} for file in sorted(kit.rglob('*')) if file.is_file()]
kit_manifest = kit / 'MCP/checksums.json'
kit_manifest.write_text(json.dumps({'format': 'agent-workspace-simple-setup', 'package_version': version, 'paths_relative_to': 'project_root', 'files': files}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
kit_files = {entry['file']: entry for entry in files}
kit_files['MCP/checksums.json'] = {'bytes': kit_manifest.stat().st_size, 'sha256': digest(kit_manifest)}
kit_hash = seal(kit, kit_zip, kit_files)
print(json.dumps({'runtime_zip': str(runtime_zip), 'runtime_sha256': runtime_hash, 'runtime_files': len(runtime_files), 'kit_zip': str(kit_zip), 'kit_sha256': kit_hash, 'kit_files': len(kit_files)}, ensure_ascii=True, indent=2))

"""Transport a verified environment as one ZIP without exposed hidden paths.

Extraction is only into a new staging directory, never a live Codex profile.
The original environment manifest and per-resource hashes remain authoritative.
"""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
from pathlib import Path
import stat
import tempfile
import zipfile
import zlib

import environment_adapter
import snapshot
import rehome_windows_paths as windows_paths

MAX_FILE = 256 * 1024 ** 2
MAX_TOTAL = 4 * 1024 ** 3
MAX_FILES = 100001


def _member(name):
    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    return entry


@contextmanager
def _read_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as error:
        raise ValueError("Invalid environment archive") from error


def pack_environment(source, destination):
    return _pack_environment(source, destination, backup_mode=False)


def pack_backup_environment(source, destination):
    return _pack_environment(source, destination, backup_mode=True)


def _pack_environment(source, destination, *, backup_mode):
    source = snapshot._safe_path(Path(source))
    destination = snapshot._safe_path(Path(destination), allow_missing=True)
    if (destination.exists() or destination.is_relative_to(source)
            or any(p.casefold() == '.codex' for p in destination.parts)):
        raise ValueError("Use a fresh environment archive outside its source")
    inventory = (environment_adapter.verify_backup_environment if backup_mode else environment_adapter.verify_environment)(source)
    if len(inventory['files']) + 1 > MAX_FILES:
        raise ValueError("Environment archive has too many files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(destination, 'x', allowZip64=True) as archive:
        archive.writestr(_member(environment_adapter.MANIFEST), snapshot._canonical(inventory))
        for entry in sorted(inventory['files'], key=lambda e: e['path']):
            if entry['size'] > MAX_FILE:
                raise ValueError("Environment file exceeds the existing builder limit")
            file = snapshot._safe_path(source.joinpath(*entry['path'].split('/')))
            before = file.stat()
            digest, length = hashlib.sha256(), 0
            with file.open('rb') as incoming, archive.open(_member(entry['path']), 'w', force_zip64=True) as outgoing:
                while block := incoming.read(1024 * 1024):
                    length += len(block)
                    total += len(block)
                    if length > MAX_FILE or total > MAX_TOTAL:
                        raise ValueError("Environment archive exceeds existing builder limits")
                    digest.update(block)
                    outgoing.write(block)
            if (length != entry['size'] or digest.hexdigest() != entry['sha256']
                    or snapshot._content_state(before) != snapshot._content_state(file.stat())):
                raise ValueError("Environment changed during archive creation")
    return _verify_environment_archive(destination, backup_mode=backup_mode)


def unpack_environment(archive_path, destination):
    return _unpack_environment(archive_path, destination, backup_mode=False)


def unpack_backup_environment(archive_path, destination):
    return _unpack_environment(archive_path, destination, backup_mode=True)


def _unpack_environment(archive_path, destination, *, backup_mode):
    archive_path = snapshot._safe_path(Path(archive_path))
    destination = snapshot._safe_path(Path(destination), allow_missing=True)
    if destination.exists() or any(p.casefold() == '.codex' for p in destination.parts):
        raise ValueError("Environment extraction requires a new directory")
    with _read_archive(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_FILES or sum(e.file_size for e in members) > MAX_TOTAL + MAX_FILE:
            raise ValueError("Environment archive exceeds bounded extraction limits")
        names = []
        for entry in members:
            snapshot._validate_relative(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.is_dir() or mode not in {0, stat.S_IFREG} or entry.flag_bits & 1
                    or entry.file_size > MAX_FILE):
                raise ValueError("Only bounded regular environment files are allowed")
            names.append(entry.filename)
        snapshot._check_collisions(names)
        if environment_adapter.MANIFEST not in names:
            raise ValueError("Environment archive is missing its manifest")
        for entry in members:
            windows_paths.check(destination.joinpath(*entry.filename.split('/')))
        destination.mkdir(parents=True)
        for entry in members:
            output = snapshot._safe_path(destination.joinpath(*entry.filename.split('/')), allow_missing=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with archive.open(entry) as incoming, output.open('xb') as outgoing:
                while block := incoming.read(1024 * 1024):
                    count += len(block)
                    if count > entry.file_size or count > MAX_FILE:
                        raise ValueError("Expanded environment data exceeds declared limits")
                    outgoing.write(block)
    return (environment_adapter.verify_backup_environment if backup_mode else environment_adapter.verify_environment)(destination)


def verify_environment_archive(archive_path):
    return _verify_environment_archive(archive_path, backup_mode=False)


def verify_backup_environment_archive(archive_path):
    return _verify_environment_archive(archive_path, backup_mode=True)


def _verify_environment_archive(archive_path, *, backup_mode):
    # The named cleanup target is a new owned tempfile directory. All archive
    # paths and file types are validated before any extraction; links are refused.
    with tempfile.TemporaryDirectory(prefix='rehome-environment-check-') as temporary:
        base = snapshot._safe_path(Path(temporary))
        if base.resolve().parent != Path(tempfile.gettempdir()).resolve():
            raise ValueError("Unexpected temporary verification directory")
        inventory = _unpack_environment(archive_path, base / 'Environment', backup_mode=backup_mode)
        return {'status': 'verified', 'file_count': inventory['file_count'],
                'total_bytes': inventory['total_bytes'], 'profile_applied': False}

"""Immutable, locally verified folder snapshots for the transfer prototype.

No discovery, networking, credential collection, installation, or restore is
performed here. Callers must explicitly select finished result files. A READY
file is only a publication marker: every file is checked again on receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from datetime import datetime, timezone
from typing import Mapping
import uuid


SCHEMA = "agent-workspace-snapshot/1"
META_NAME = "snapshot.json"
MANIFEST_NAME = "manifest.json"
READY_NAME = "READY.json"
CHAT_BUNDLE_PATH = "Chats/transfer.json"
_RESERVED = {META_NAME.casefold(), MANIFEST_NAME.casefold(), READY_NAME.casefold()}
_HEX = re.compile(r"^[0-9a-f]{64}$")
_DEVICE = re.compile(r"^[^\x00-\x1f]{1,128}$")
_DOS = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SnapshotError("INVALID_PATH", "Use a nonempty relative path with '/' separators")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(
        not p or p in (".", "..") or p.endswith((".", " ")) or _DOS.match(p)
        or re.search(r'[<>:"|?*\x00-\x1f]', p) for p in parts
    ):
        raise SnapshotError("INVALID_PATH", "Unsafe or nonportable snapshot path")
    if len(parts) == 1 and value.casefold() in _RESERVED:
        raise SnapshotError("RESERVED_PATH", "Snapshot metadata paths are reserved")
    return value


def _safe_path(path: Path, *, allow_missing: bool = False) -> Path:
    """Reject links and Windows reparse points in every existing component."""
    absolute = Path(os.path.abspath(path))
    chain = list(reversed(absolute.parents)) + [absolute]
    missing = False
    for component in chain:
        if missing:
            continue
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                missing = True
                continue
            raise SnapshotError("MISSING_FILE", "A snapshot path is missing") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SnapshotError("LINK_PATH", "Links and Windows reparse points are not allowed")
        if component != absolute and not stat.S_ISDIR(info.st_mode):
            raise SnapshotError("INVALID_PATH", "A parent path is not a directory")
    return absolute


def _fingerprint(info):
    # Windows Python 3.12 can expose different ctime semantics through path
    # lstat and descriptor fstat. Compare ctime only within the same API below.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _content_state(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_file(path: Path) -> tuple[str, int]:
    path = _safe_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotError("INVALID_FILE", "Expected a regular file")
    checksum = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _fingerprint(before) != _fingerprint(opened):
            raise SnapshotError("MUTATED_FILE", "File changed before opening")
        while block := stream.read(1024 * 1024):
            checksum.update(block)
            total += len(block)
        after = os.fstat(stream.fileno())
    _safe_path(path)
    if (_fingerprint(before) != _fingerprint(after)
            or _content_state(opened) != _content_state(after)
            or _content_state(before) != _content_state(path.lstat())):
        raise SnapshotError("MUTATED_FILE", "File changed during verification")
    if total != before.st_size:
        raise SnapshotError("MUTATED_FILE", "File length changed during verification")
    return checksum.hexdigest(), total


def _copy_verified(source: Path, destination: Path) -> tuple[str, int]:
    source = _safe_path(source)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotError("INVALID_FILE", "Only regular finished result files can be selected")
    _safe_path(destination, allow_missing=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(destination.parent)
    checksum = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, flags), "rb") as incoming, destination.open("xb") as outgoing:
        opened = os.fstat(incoming.fileno())
        if _fingerprint(before) != _fingerprint(opened):
            raise SnapshotError("MUTATED_FILE", "Result changed before copying")
        while block := incoming.read(1024 * 1024):
            outgoing.write(block)
            checksum.update(block)
            total += len(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
    _safe_path(source)
    if (_fingerprint(before) != _fingerprint(after)
            or _content_state(opened) != _content_state(after)
            or _content_state(before) != _content_state(source.lstat())):
        raise SnapshotError("MUTATED_FILE", "Result changed during copying")
    if total != before.st_size or _hash_file(destination) != (checksum.hexdigest(), total):
        raise SnapshotError("COPY_MISMATCH", "Copied result failed verification")
    return checksum.hexdigest(), total


def _write_json(path: Path, value) -> None:
    _safe_path(path, allow_missing=True)
    with path.open("xb") as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path):
    _safe_path(path)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SnapshotError("INVALID_JSON", "Duplicate metadata keys are not allowed")
            result[key] = value
        return result
    def reject_constant(_):
        raise SnapshotError("INVALID_JSON", "Non-finite metadata numbers are not allowed")
    try:
        with path.open("rb") as stream:
            return json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise SnapshotError("INVALID_JSON", "Invalid snapshot metadata") from None


def _validate_header(value) -> None:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise SnapshotError("INVALID_MANIFEST", "Unsupported snapshot schema")
    if type(value.get("version")) is not int or value["version"] < 1:
        raise SnapshotError("INVALID_MANIFEST", "Version must be a positive integer")
    try:
        if str(uuid.UUID(value["snapshot_id"])) != value["snapshot_id"]:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise SnapshotError("INVALID_MANIFEST", "Invalid snapshot identifier") from None
    parent = value.get("parent_digest")
    if parent is not None and (not isinstance(parent, str) or not _HEX.fullmatch(parent)):
        raise SnapshotError("INVALID_MANIFEST", "Invalid parent checksum")
    if not isinstance(value.get("device_id"), str) or not _DEVICE.fullmatch(value["device_id"]):
        raise SnapshotError("INVALID_MANIFEST", "Invalid device identifier")
    if not isinstance(value.get("created_at"), str):
        raise SnapshotError("INVALID_MANIFEST", "Missing creation time")


def _check_collisions(paths: list[str]) -> None:
    seen = set()
    for value in paths:
        folded = _validate_relative(value).casefold()
        if folded in seen:
            raise SnapshotError("PATH_COLLISION", "File paths collide on Windows")
        seen.add(folded)
    for value in seen:
        parts = value.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise SnapshotError("PATH_COLLISION", "A path is both file and directory")


def create_snapshot(destination, *, version: int, device_id: str,
                    results: Mapping[str, os.PathLike | str], chat_bundle: dict | None = None,
                    parent_digest: str | None = None) -> dict:
    """Create a NEW snapshot folder using explicitly selected finished files.

    JSON transfer data is stored at Chats/transfer.json. Its content policy is
    enforced by native_adapter, not inferred or silently redacted here.
    Failures leave an incomplete folder without READY.json; it is never applied.
    """
    header = {"schema": SCHEMA, "version": version, "snapshot_id": str(uuid.uuid4()),
              "parent_digest": parent_digest, "device_id": device_id,
              "created_at": datetime.now(timezone.utc).isoformat()}
    _validate_header(header)
    if not isinstance(results, Mapping):
        raise SnapshotError("INVALID_INPUT", "Finished results must be an explicit path mapping")
    paths = list(results)
    if chat_bundle is not None:
        if not isinstance(chat_bundle, dict):
            raise SnapshotError("INVALID_INPUT", "Chat transfer data must be an object")
        paths.append(CHAT_BUNDLE_PATH)
    _check_collisions(paths)
    sources = {relative: _safe_path(Path(source)) for relative, source in results.items()}
    root = _safe_path(Path(destination), allow_missing=True)
    if root.exists():
        raise SnapshotError("DESTINATION_EXISTS", "Use a new snapshot directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(root.parent)
    root.mkdir()
    _write_json(root / META_NAME, header)
    entries = []
    for relative in sorted(results, key=str.casefold):
        checksum, size = _copy_verified(sources[relative], root.joinpath(*relative.split("/")))
        entries.append({"path": relative, "size": size, "sha256": checksum, "kind": "result"})
    if chat_bundle is not None:
        bundle_path = root.joinpath(*CHAT_BUNDLE_PATH.split("/"))
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(bundle_path, chat_bundle)
        checksum, size = _hash_file(bundle_path)
        entries.append({"path": CHAT_BUNDLE_PATH, "size": size, "sha256": checksum, "kind": "chat_bundle"})
    entries.sort(key=lambda entry: entry["path"].casefold())
    manifest = {**header, "files": entries, "file_count": len(entries),
                "total_bytes": sum(entry["size"] for entry in entries)}
    _write_json(root / MANIFEST_NAME, manifest)
    _write_json(root / READY_NAME, {"schema": SCHEMA, "snapshot_id": header["snapshot_id"],
                                   "manifest_sha256": _digest(manifest)})
    return verify_snapshot(root)


def verify_snapshot(snapshot_path) -> dict:
    """Check every received file, even if the READY marker already exists."""
    root = _safe_path(Path(snapshot_path))
    if not root.is_dir():
        raise SnapshotError("INVALID_PATH", "Snapshot must be a directory")
    header = _read_json(root / META_NAME)
    _validate_header(header)
    manifest = _read_json(root / MANIFEST_NAME)
    _validate_header(manifest)
    if set(manifest) != {"schema", "version", "snapshot_id", "parent_digest", "device_id",
                         "created_at", "files", "file_count", "total_bytes"}:
        raise SnapshotError("INVALID_MANIFEST", "Unexpected manifest fields")
    if header != {key: manifest.get(key) for key in header} or set(header) != {
        "schema", "version", "snapshot_id", "parent_digest", "device_id", "created_at"
    }:
        raise SnapshotError("METADATA_MISMATCH", "Snapshot header and manifest differ")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise SnapshotError("INVALID_MANIFEST", "Missing file list")
    if type(manifest.get("file_count")) is not int or manifest["file_count"] != len(entries):
        raise SnapshotError("INVALID_MANIFEST", "File count mismatch")
    paths = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "kind"}:
            raise SnapshotError("INVALID_MANIFEST", "Invalid file entry")
        paths.append(entry["path"])
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise SnapshotError("INVALID_MANIFEST", "Invalid file size")
        if not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"]):
            raise SnapshotError("INVALID_MANIFEST", "Invalid file checksum")
        if entry["kind"] not in ("result", "chat_bundle"):
            raise SnapshotError("INVALID_MANIFEST", "Unknown payload kind")
        if entry["kind"] == "chat_bundle" and entry["path"] != CHAT_BUNDLE_PATH:
            raise SnapshotError("INVALID_MANIFEST", "Unknown chat transfer path")
    _check_collisions(paths)
    if type(manifest.get("total_bytes")) is not int or manifest["total_bytes"] != sum(e["size"] for e in entries):
        raise SnapshotError("INVALID_MANIFEST", "Total byte size mismatch")
    ready = _read_json(root / READY_NAME)
    checksum = _digest(manifest)
    if ready != {"schema": SCHEMA, "snapshot_id": manifest["snapshot_id"], "manifest_sha256": checksum}:
        raise SnapshotError("READY_MISMATCH", "Publication marker does not match the manifest")
    expected = set(paths) | {META_NAME, MANIFEST_NAME, READY_NAME}
    actual = set()
    for directory, subdirs, files in os.walk(root, followlinks=False):
        directory_path = _safe_path(Path(directory))
        for name in subdirs:
            _safe_path(directory_path / name)
        for name in files:
            file = _safe_path(directory_path / name)
            actual.add(file.relative_to(root).as_posix())
    if actual != expected:
        raise SnapshotError("FILE_SET_MISMATCH", "Snapshot files are missing or unlisted")
    for entry in entries:
        actual_digest, size = _hash_file(root.joinpath(*entry["path"].split("/")))
        if size != entry["size"]:
            raise SnapshotError("SIZE_MISMATCH", "A received file has an incomplete or different size")
        if actual_digest != entry["sha256"]:
            raise SnapshotError("HASH_MISMATCH", "A received file differs from the manifest")
    # Recheck metadata after reading payload, including concurrent delivery.
    if _read_json(root / MANIFEST_NAME) != manifest or _read_json(root / META_NAME) != header or _read_json(root / READY_NAME) != ready:
        raise SnapshotError("MUTATED_FILE", "Snapshot metadata changed during verification")
    return {"status": "verified", "manifest": manifest, "manifest_digest": checksum,
            "version": manifest["version"], "snapshot_id": manifest["snapshot_id"],
            "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"],
            "snapshot_file_count": len(actual),
            "snapshot_total_bytes": sum((root / name).stat().st_size for name in actual),
            "scope": "local_files_only", "nas_delivery_verified": False}


def inspect_snapshot(snapshot_path) -> dict:
    """Describe a local folder without treating a partial arrival as usable."""
    version = None
    try:
        root = _safe_path(Path(snapshot_path))
        for name in (META_NAME, MANIFEST_NAME):
            if (root / name).exists():
                value = _read_json(root / name)
                _validate_header(value)
                version = value["version"]
                break
        return verify_snapshot(root)
    except SnapshotError as error:
        incomplete = {"MISSING_FILE", "FILE_SET_MISMATCH", "SIZE_MISMATCH", "HASH_MISMATCH", "MUTATED_FILE", "INVALID_JSON"}
        return {"status": "incomplete" if error.code in incomplete else "invalid", "version": version,
                "code": error.code, "reason": str(error), "scope": "local_files_only",
                "nas_delivery_verified": False}
    except OSError:
        return {"status": "incomplete", "version": version, "code": "IO_ERROR",
                "reason": "Snapshot is not completely accessible", "scope": "local_files_only",
                "nas_delivery_verified": False}


def diagnose_snapshot(snapshot_path, *, max_issues=12) -> dict:
    """Bounded, read-only explanation; never turns a failed copy into success."""
    if type(max_issues) is not int or not 1 <= max_issues <= 100:
        raise SnapshotError("INVALID_INPUT", "Diagnostic sample size must be 1..100")
    info = inspect_snapshot(snapshot_path)
    result = {key: info.get(key) for key in ("status", "version", "code", "reason")}
    result.update(profile_applied=False, nas_delivery_verified=False)
    try:
        root = _safe_path(Path(snapshot_path))
        manifest_path = root / MANIFEST_NAME
        if manifest_path.stat().st_size > 16 * 1024 ** 2:
            raise SnapshotError("INVALID_MANIFEST", "Diagnostic manifest exceeds 16 MiB")
        manifest = _read_json(manifest_path)
        _validate_header(manifest)
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > 100000:
            raise SnapshotError("INVALID_MANIFEST", "Diagnostic inventory is missing or too large")
        paths = []
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or type(entry.get("size")) is not int or entry['size'] < 0
                    or not isinstance(entry.get("sha256"), str) or not _HEX.fullmatch(entry['sha256'])):
                raise SnapshotError("INVALID_MANIFEST", "Invalid diagnostic inventory entry")
            paths.append(entry['path'])
        _check_collisions(paths)
        actual, io_errors = set(), []
        def walk_error(error):
            io_errors.append({'code': getattr(error, 'winerror', None) or getattr(error, 'errno', None),
                              'reason': 'Directory is not completely accessible'})
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            current = _safe_path(Path(directory))
            for name in list(dirs):
                try:
                    _safe_path(current / name)
                except (SnapshotError, OSError) as error:
                    io_errors.append({'path': (current / name).relative_to(root).as_posix(), 'reason': str(error)})
                    dirs.remove(name)
            actual.update((current / name).relative_to(root).as_posix() for name in files)
        expected = set(paths) | {META_NAME, MANIFEST_NAME, READY_NAME}
        missing, unexpected = sorted(expected - actual), sorted(actual - expected)
        mismatches = []
        for entry in entries:
            if entry['path'] not in actual:
                continue
            try:
                file = _safe_path(root.joinpath(*entry['path'].split('/')))
                size = file.stat().st_size
                if size != entry['size']:
                    mismatches.append({'path': entry['path'], 'code': 'SIZE_MISMATCH',
                                       'expected_size': entry['size'], 'actual_size': size})
                elif _hash_file(file)[0] != entry['sha256']:
                    mismatches.append({'path': entry['path'], 'code': 'HASH_MISMATCH', 'size': size})
            except (SnapshotError, OSError) as error:
                io_errors.append({'path': entry['path'], 'code': getattr(error, 'code', None)
                                  or getattr(error, 'winerror', None) or getattr(error, 'errno', None), 'reason': str(error)})
        result.update(expected_payload_files=len(paths), present_payload_files=len(set(paths) & actual),
                      missing_count=len(missing), missing=missing[:max_issues],
                      unexpected_count=len(unexpected), unexpected=unexpected[:max_issues],
                      mismatch_count=len(mismatches), mismatches=mismatches[:max_issues],
                      io_error_count=len(io_errors), io_errors=io_errors[:max_issues], sample_limit=max_issues)
    except (SnapshotError, OSError) as error:
        result['diagnostic_error'] = {'code': getattr(error, 'code', None) or getattr(error, 'winerror', None)
                                      or getattr(error, 'errno', None), 'reason': str(error)}
    return result


def copy_snapshot(source, destination, *, expected_digest=None) -> dict:
    """Deliver an immutable checkpoint to a NEW local folder, preserving identity.

    This copies a file protocol, not a live profile, and makes no NAS assertion.
    READY is generated only after all expected files and metadata are checked.
    Interrupted copies remain incomplete and can never replace existing folders.
    """
    source = _safe_path(Path(source))
    destination = _safe_path(Path(destination), allow_missing=True)
    if (source == destination or source.is_relative_to(destination)
            or destination.is_relative_to(source)):
        raise SnapshotError("INVALID_PATH", "Source and destination must not overlap")
    info = verify_snapshot(source)
    if expected_digest is not None and info["manifest_digest"] != expected_digest:
        raise SnapshotError("MUTATED_FILE", "Checkpoint changed after the synchronization decision")
    if destination.exists():
        raise SnapshotError("DESTINATION_EXISTS", "Use a fresh delivery directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(destination.parent)
    destination.mkdir()
    manifest = info["manifest"]
    header = {key: manifest[key] for key in ("schema", "version", "snapshot_id", "parent_digest", "device_id", "created_at")}
    _write_json(destination / META_NAME, header)
    for entry in manifest["files"]:
        relative = entry["path"]
        result = _copy_verified(source.joinpath(*relative.split("/")), destination.joinpath(*relative.split("/")))
        if result != (entry["sha256"], entry["size"]):
            raise SnapshotError("MUTATED_FILE", "Source changed after checkpoint verification")
    _write_json(destination / MANIFEST_NAME, manifest)
    expected = {entry["path"] for entry in manifest["files"]} | {META_NAME, MANIFEST_NAME}
    actual = set()
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in dirs + files:
            path = _safe_path(Path(directory) / name)
            if path.is_file():
                actual.add(path.relative_to(destination).as_posix())
    if actual != expected:
        raise SnapshotError("FILE_SET_MISMATCH", "Files appeared or disappeared during delivery")
    if _read_json(destination / META_NAME) != header or _read_json(destination / MANIFEST_NAME) != manifest:
        raise SnapshotError("MUTATED_FILE", "Delivery metadata changed")
    for entry in manifest["files"]:
        if _hash_file(destination.joinpath(*entry["path"].split("/"))) != (entry["sha256"], entry["size"]):
            raise SnapshotError("MUTATED_FILE", "Delivered file changed before publication")
    _write_json(destination / READY_NAME, {"schema": SCHEMA, "snapshot_id": manifest["snapshot_id"],
                                         "manifest_sha256": info["manifest_digest"]})
    return verify_snapshot(destination)


def select_sync_action(local_path, received_paths, *, expected_version: int | None = None) -> dict:
    """Plan a local-copy sync action; never assert authoritative NAS freshness.

    Ancestry must be proven through the provided verified folders. If an
    intervening parent is unavailable, return conflict rather than guessing.
    """
    local = inspect_snapshot(local_path) if local_path is not None else None
    received = [inspect_snapshot(path) for path in received_paths]
    return _select_snapshot_states(local, received, expected_version=expected_version)


def _select_snapshot_states(local, received, *, expected_version=None):
    """Decide over one just-verified inventory, without rescanning every payload.

    Internal callers must obtain these states using the real file verifiers.
    A later copy still verifies the selected digest before writing anything.
    """
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise SnapshotError("INVALID_INPUT", "Expected version must be a positive integer")
    def decision(action, reason, **extra):
        return {"action": action, "reason": reason, "scope": "locally_received_copies",
                "nas_delivery_verified": False, **extra}
    if local is not None and local["status"] != "verified":
        return decision("conflict", "Local snapshot is not verified", local=local)
    local_version = local["version"] if local else 0
    complete = [entry for entry in received if entry["status"] == "verified"]
    latest_complete = max((entry["version"] for entry in complete), default=0)
    threshold = max(local_version, latest_complete)
    if any(entry["status"] == "invalid" for entry in received):
        return decision("conflict", "A received snapshot has invalid metadata")
    if any(entry["status"] != "verified" and (entry["version"] is None or entry["version"] >= threshold)
           for entry in received):
        return decision("wait", "A possibly current or newer snapshot has not arrived completely")
    highest_known = max(local_version, latest_complete)
    if expected_version is not None and highest_known < expected_version:
        return decision("wait", "The version reported by the sending PC has not arrived", expected_version=expected_version)
    all_complete = complete + ([local] if local else [])
    versions = {}
    known = {entry["manifest_digest"]: entry for entry in all_complete}
    for entry in all_complete:
        previous = versions.setdefault(entry["version"], entry["manifest_digest"])
        if previous != entry["manifest_digest"]:
            return decision("conflict", "Equal version numbers contain different snapshots", version=entry["version"])
    def proves_continuation(higher, lower):
        if higher["manifest_digest"] == lower["manifest_digest"]:
            return True
        cursor = higher
        seen = set()
        while cursor["manifest_digest"] not in seen:
            seen.add(cursor["manifest_digest"])
            parent = cursor["manifest"].get("parent_digest")
            if parent == lower["manifest_digest"]:
                return True
            next_cursor = known.get(parent)
            if next_cursor is None or next_cursor["version"] >= cursor["version"]:
                break
            cursor = next_cursor
        return False
    if len(all_complete) > 1:
        highest = max(all_complete, key=lambda entry: entry["version"])
        if any(not proves_continuation(highest, lower) for lower in all_complete):
            return decision("conflict", "The higher version is not a proven continuation of the other snapshot")
    if not complete:
        return decision("upload" if local else "wait", "No received verified snapshot is available", version=local_version or None)
    newest = max(complete, key=lambda entry: entry["version"])
    if local is None:
        return decision("download", "Initialize from the newest completely received snapshot", version=newest["version"], manifest_digest=newest["manifest_digest"])
    if local["manifest_digest"] == newest["manifest_digest"]:
        return decision("unchanged", "Version and contents match", version=local_version, manifest_digest=local["manifest_digest"])
    higher, lower = (local, newest) if local_version > newest["version"] else (newest, local)
    if not proves_continuation(higher, lower):
        return decision("conflict", "The higher version is not a proven continuation of the other snapshot")
    action = "upload" if higher is local else "download"
    return decision(action, "Verified continuation has a higher version", version=higher["version"], manifest_digest=higher["manifest_digest"])

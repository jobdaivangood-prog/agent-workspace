"""Immutable complete publications over ordinary folder synchronization.

Ordering is logical, not a claim about wall-clock time on disconnected devices.
No profile, Workspace selection, process or credential is changed here.
"""
import json
from pathlib import Path
import re
import uuid

import snapshot

FORMAT = 'rehome-publication/1'
POLICY = 'latest_complete_publication_with_local_rollback/v1'
MANIFEST = 'Publication.json'
READY = 'PUBLICATION-READY.json'
_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')


def require(value, message):
    if not value:
        raise ValueError(message)


def ident(value):
    require(isinstance(value, str) and _ID.fullmatch(value), 'Invalid publication identity')
    return value


def _verify_package(path):
    import rehome_backup_module
    import rehome_summary_history
    info=rehome_backup_module.verify_backup(path,expected_text_policy=rehome_summary_history.POLICY)
    candidate=rehome_backup_module._read(Path(path)/snapshot.CHAT_BUNDLE_PATH)
    return {**info,'session_workspace_id':candidate['source']['workspace_id']}


def _context(info,workspace_id):
    from rehome_summary_history import POLICY as summary_policy
    require(info.get('text_policy')==summary_policy and info.get('session_workspace_id')==workspace_id,
            'Publication payload is not a summary of this workspace')


def _read(path):
    path = snapshot._safe_path(Path(path))
    require(path.stat().st_size <= 4 * 1024 * 1024, 'Publication metadata exceeds limit')
    return json.loads(path.read_text(encoding='utf-8'))


def _validate(value):
    fields = {'format', 'policy', 'publication_id', 'workspace_id', 'channel_id',
              'publisher_device_id', 'logical_sequence', 'parents', 'package_manifest_sha256', 'report'}
    require(isinstance(value, dict) and set(value) == fields, 'Invalid publication fields')
    require(value['format'] == FORMAT and value['policy'] == POLICY, 'Unsupported publication contract')
    for key in ('publication_id', 'workspace_id', 'channel_id', 'publisher_device_id'):
        ident(value[key])
    require(type(value['logical_sequence']) is int and 1 <= value['logical_sequence'] < 2**53,
            'Invalid logical publication sequence')
    parents = value['parents']
    require(isinstance(parents, list) and len(parents) <= 10000 and
            all(isinstance(p, str) for p in parents) and parents == sorted(set(parents)),
            'Invalid publication parents')
    for parent in parents:
        ident(parent)
    require(value['publication_id'] not in parents, 'Self-referencing publication')
    require(isinstance(value['package_manifest_sha256'], str) and
            _SHA.fullmatch(value['package_manifest_sha256']), 'Invalid package digest')
    if value['report'] is not None:
        from rehome_session_report import make_report
        report=value['report']
        require(isinstance(report,dict) and set(report)=={'status','totals','items','timings'},'Invalid publication report')
        inventory=[{k:row[k] for k in ('category','id','name','project_id','chat_id')} for row in report['items']]
        outcomes=[{k:row[k] for k in ('category','id','status','reason')} for row in report['items']]
        require(report==make_report(inventory,outcomes,report['timings']),'Publication report totals differ')
    return value


def order(value):
    return value['logical_sequence'], value['publisher_device_id'], value['publication_id']


def verify(folder, *, workspace_id, channel_id='main', verifier=None):
    """Verify all payload bytes; READY alone never establishes completeness."""
    folder = snapshot._safe_path(Path(folder))
    ident(workspace_id); ident(channel_id)
    value = _validate(_read(folder / MANIFEST))
    require(folder.name == 'ReHome-' + value['publication_id'], 'Publication directory identity differs')
    require(value['workspace_id'] == workspace_id and value['channel_id'] == channel_id,
            'Publication belongs to another workspace/channel')
    require({p.name for p in folder.iterdir()} == {MANIFEST, READY, 'Package'},
            'Unexpected publication layout')
    digest = snapshot._hash_file(folder / MANIFEST)[0]
    require(_read(folder / READY) == {'format': FORMAT, 'manifest_sha256': digest},
            'Incomplete publication or changed manifest')
    result = (verifier or _verify_package)(folder / 'Package')
    _context(result,workspace_id)
    require(result['manifest_digest'] == value['package_manifest_sha256'], 'Publication payload differs')
    require(snapshot._hash_file(folder / MANIFEST)[0] == digest and
            _read(folder / READY) == {'format': FORMAT, 'manifest_sha256': digest},
            'Publication changed during verification')
    return {'publication': value, 'manifest_digest': digest, 'path': str(folder),
            'package': str(folder / 'Package'), 'counts': result.get('counts', {})}


def scan(root, *, workspace_id, channel_id='main', verifier=None):
    root = snapshot._safe_path(Path(root))
    ident(workspace_id); ident(channel_id)
    require(root.is_dir(), 'Publication root must be a directory')
    complete, unavailable = [], []
    entries = sorted(root.iterdir())
    require(len(entries) <= 10000, 'Publication root exceeds entry limit')
    for child in entries:
        if not child.name.startswith('ReHome-'):
            continue
        # Legacy full backups are still read by their existing route.
        if not (child / MANIFEST).exists():
            continue
        try:
            complete.append(verify(child, workspace_id=workspace_id, channel_id=channel_id,
                                   verifier=verifier))
        except (ValueError, OSError, KeyError, TypeError, snapshot.SnapshotError):
            unavailable.append({'directory': child.name, 'reason': 'publication_unavailable_or_invalid'})
    complete.sort(key=lambda row: order(row['publication']))
    return {'complete': complete, 'unavailable': unavailable,
            'latest': complete[-1] if complete else None,
            'freshness': 'Complete publications received in this local folder only'}


def publish(package, root, *, expected_package_digest, workspace_id, publisher_device_id,
            channel_id='main', verifier=None, report=None):
    """Copy a sealed package; expose publication readiness only after readback.

    Failed/incomplete output is retained for diagnosis and cannot be selected.
    Parent IDs document observations; each package is self-contained, so old
    parent payloads are not required to restore a new publication.
    """
    for value in (workspace_id, publisher_device_id, channel_id):
        ident(value)
    verifier = verifier or _verify_package
    package = snapshot._safe_path(Path(package))
    root = snapshot._safe_path(Path(root))
    require(not package.is_relative_to(root) and not root.is_relative_to(package),
            'Publication source and destination overlap')
    info = verifier(package)
    _context(info,workspace_id)
    require(info['manifest_digest'] == expected_package_digest, 'Prepared package changed')
    known = scan(root, workspace_id=workspace_id, channel_id=channel_id, verifier=verifier)
    # A repeated Finish with identical prepared bytes does not publish again.
    previous = known['latest']
    if previous and previous['publication']['package_manifest_sha256'] == expected_package_digest:
        return {**previous, 'status': 'unchanged', 'unavailable': known['unavailable']}
    sequence = max((row['publication']['logical_sequence'] for row in known['complete']), default=0) + 1
    publication_id = uuid.uuid4().hex
    value = _validate({'format': FORMAT, 'policy': POLICY, 'publication_id': publication_id,
        'workspace_id': workspace_id, 'channel_id': channel_id, 'publisher_device_id': publisher_device_id,
        'logical_sequence': sequence,
        'parents': sorted(row['publication']['publication_id'] for row in known['complete']),
        'package_manifest_sha256': expected_package_digest, 'report':report})
    folder = root / ('ReHome-' + publication_id)
    folder.mkdir()
    snapshot._write_json(folder / MANIFEST, value)
    destination = folder / 'Package'
    destination.mkdir()
    for source in sorted(package.rglob('*')):
        snapshot._safe_path(source)
        target = destination / source.relative_to(package)
        if source.is_dir():
            target.mkdir(exist_ok=True)
        else:
            snapshot._copy_verified(source, target)
    require(verifier(package)['manifest_digest'] == expected_package_digest and
            verifier(destination)['manifest_digest'] == expected_package_digest,
            'Package changed during publication')
    pin = snapshot._hash_file(folder / MANIFEST)[0]
    snapshot._write_json(folder / READY, {'format': FORMAT, 'manifest_sha256': pin})
    result = verify(folder, workspace_id=workspace_id, channel_id=channel_id, verifier=verifier)
    return {**result, 'status': 'published', 'unavailable': known['unavailable']}

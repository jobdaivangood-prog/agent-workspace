"""Backup composition using ReHome's existing verified snapshot engine.

This first module stages portable history/results, never applies a live profile.
Environment composition currently accepts the existing synthetic adapter only.
No retention, NAS reconciliation, credential store or model calls are performed.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tempfile

import environment_archive
import environment_adapter
import rehome_history_filter as filtering
import rehome_history_lineage as lineage
import rehome_review as review
import rehome_review_sources as managed
import rehome_selection as selection
import snapshot
import rehome_result_links as links

FORMAT = 'rehome-backup/1'
DESCRIPTOR = 'ReHome.json'
ENVIRONMENT = 'Environment.zip'
SUPPORTED_RUNTIME = '0.166.0-alpha.5.1'
MAX_METADATA = 128 * 1024 * 1024
ENGINE_PATH = Path(__file__).resolve().parent / 'vendor/codex_backup/backup.py'


class BackupError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise BackupError(message)


def _runtime(value):
    _require(value == SUPPORTED_RUNTIME, 'Unsupported Codex runtime; schema adaptation required')


def _new_target(path):
    value = Path(path)
    _require(value.is_absolute() and '..' not in value.parts and
             all(part.casefold() != '.codex' for part in value.parts),
             'Use an absolute new staging directory outside live .codex')
    value = snapshot._safe_path(value, allow_missing=True)
    _require(not value.exists(), 'Destination already exists; overwrite is prohibited')
    return value


def _read(path):
    _require(path.stat().st_size <= MAX_METADATA, 'Backup metadata exceeds the supported limit')
    return snapshot._read_json(path)


def _result_paths(paths, candidate):
    projects = {project['id'] for project in candidate['transfer']['projects']}
    if any(t['project_id'] is None for t in candidate['transfer']['threads']):projects.add('__projectless__')
    snapshot._check_collisions(list(paths))
    for path in paths:
        snapshot._validate_relative(path)
        parts = path.split('/')
        _require(len(parts) >= 4 and parts[0] == 'Projects' and
                 parts[1] in projects and parts[2] == 'Results',
                 'Finished files require Projects/<selected-project-id>/Results/<file>')


def _result_bindings(ready_files, profile):
    bindings = {}
    for relative, record in ready_files.items():
        _require(isinstance(record, dict) and set(record) in ({'path', 'classification'}, {'path', 'classification','aliases'}),
                 'Every result requires an explicit generated finished classification')
        selection._file_allowed(record['path'], profile)
        path = managed._local(record['path'])
        _require(not any(part.casefold() in ('.codex', '.aws', '.ssh', '.gnupg', '.azure')
                         for part in path.parts), 'Credential/profile resources are not finished results')
        binding = managed._result_binding({'path':record['path'],'classification':record['classification'], 'display_name': relative})
        if 'aliases' in record:
            _require(isinstance(record['aliases'],list) and record['aliases']==sorted(set(record['aliases'])), 'Invalid explicit source aliases')
            binding['aliases']=record['aliases']
        bindings[relative] = binding
    return bindings


def create_backup(profile, selected_thread_ids, destination, *, ready_files,
                  runtime_version, device_id, version=1, journal_decoder=None,
                  environment_stage=None,include_all_layout=False,preserve_display=False,closed_snapshot=None):
    """Create a NEW portable folder from explicit chats and finished results.

    Caller supplies an inactive source; lineage checks its read consistency.
    Closing Codex and full-profile coverage are independent pending UI steps.
    ready_files maps portable names to {path, classification}; classification
    must be the existing selected_finished_generated value. No project folder
    is scanned. A generated .py result is permitted, credential files are not.
    """
    _runtime(runtime_version)
    destination = _new_target(destination)
    profile = snapshot._safe_path(Path(profile))
    _require(not destination.is_relative_to(profile) and not profile.is_relative_to(destination),
             'Backup destination must not overlap its source profile')
    _require(isinstance(ready_files, Mapping), 'Finished files must be explicitly supplied')
    bindings = _result_bindings(ready_files, profile)
    candidate = lineage.export_candidate(profile, selected_thread_ids,
        journal_decoder=journal_decoder, text_policy=filtering.VERBATIM_TEXT,include_all_layout=include_all_layout,
        preserve_display=preserve_display,resolve_current_storage=True,closed_snapshot=closed_snapshot)
    review.validate_backup_history(candidate)
    _result_paths(ready_files, candidate)
    with tempfile.TemporaryDirectory(prefix='rehome-backup-') as temporary:
        temporary = Path(temporary)
        payload = {}
        engine = environment_adapter.load_existing_engine(ENGINE_PATH)
        _require(all(binding['bytes'] <= engine.MAX_FILE for binding in bindings.values()),
                 'Finished file exceeds existing builder limit')
        _require(sum(binding['bytes'] for binding in bindings.values()) <= engine.MAX_TOTAL,
                 'Finished results exceed existing builder limit')
        for relative, binding in bindings.items():
            # Freeze the exact classified bytes before publishing a snapshot.
            staged = temporary / 'finished' / relative
            current = _result_bindings({relative: ready_files[relative]}, profile)[relative]
            _require(current == binding, 'Finished result changed after selection')
            _require(snapshot._copy_verified(Path(binding['path']), staged) ==
                     (binding['sha256'], binding['bytes']), 'Finished result changed while copying')
            data = environment_adapter._read_stable(staged, engine.MAX_FILE)
            _require(not engine.sensitive(Path(binding['path']), data),
                     'Credential-like file cannot be included as a finished result')
            payload[relative] = staged
        descriptor = {'format': FORMAT, 'text_policy': filtering.VERBATIM_TEXT,
            'runtime_version': runtime_version, 'source_schema_version': candidate['source_schema_version'],
            'history_path': snapshot.CHAT_BUNDLE_PATH, 'result_paths': sorted(ready_files),
            'environment': 'not_included', 'live_profile_apply_enabled': False,
            'separate_source_files_included': False, 'structured_attachments_included': False}
        if bindings:
            descriptor['format'] = 'rehome-backup/2'
            descriptor['result_links_path'] = links.NAME
            snapshot._write_json(temporary / links.NAME, links.make_manifest(candidate, bindings))
            payload[links.NAME] = temporary / links.NAME
        if environment_stage is not None:
            inventory = _read(Path(environment_stage) / environment_adapter.MANIFEST)
            real = inventory.get('format') == environment_adapter.BACKUP_FORMAT
            pack = environment_archive.pack_backup_environment if real else environment_archive.pack_environment
            pack(environment_stage, temporary / ENVIRONMENT)
            payload[ENVIRONMENT] = temporary / ENVIRONMENT
            descriptor['environment'] = 'portable_resources_only' if real else 'synthetic_resources_only'
        snapshot._write_json(temporary / DESCRIPTOR, descriptor)
        payload[DESCRIPTOR] = temporary / DESCRIPTOR
        snapshot.create_snapshot(destination, version=version, device_id=device_id,
                                 results=payload, chat_bundle=candidate)
    return verify_backup(destination)


def verify_backup(backup, *, expected_digest=None,
                  expected_text_policy=filtering.VERBATIM_TEXT):
    """Verify all received bytes and the explicit history policy before staging."""
    _require(expected_text_policy == filtering.VERBATIM_TEXT, 'Unsupported backup text policy')
    root = snapshot._safe_path(Path(backup))
    info = snapshot.verify_snapshot(root)
    pin = info['manifest_digest']
    if expected_digest is not None:
        _require(pin == expected_digest, 'Backup manifest differs from selected copy')
    descriptor = _read(root / DESCRIPTOR)
    fields = {
        'format', 'text_policy', 'runtime_version', 'source_schema_version', 'history_path',
        'result_paths', 'environment', 'live_profile_apply_enabled',
        'separate_source_files_included', 'structured_attachments_included'}
    linked = descriptor.get('format') == 'rehome-backup/2' if isinstance(descriptor, dict) else False
    _require(isinstance(descriptor, dict) and set(descriptor) == fields | ({'result_links_path'} if linked else set()), 'Invalid backup descriptor')
    _require(descriptor['format'] in (FORMAT, 'rehome-backup/2') and descriptor['text_policy'] == expected_text_policy and
             descriptor['source_schema_version'] == lineage.source.CURRENT_SCHEMA_VERSION and
             descriptor['history_path'] == snapshot.CHAT_BUNDLE_PATH and
             descriptor['live_profile_apply_enabled'] is False and
             descriptor['separate_source_files_included'] is False and
             descriptor['structured_attachments_included'] is False, 'Backup contract differs')
    _runtime(descriptor['runtime_version'])
    candidate = _read(root / snapshot.CHAT_BUNDLE_PATH)
    counts = review.validate_backup_history(candidate)
    _require(candidate['text_policy'] == descriptor['text_policy'], 'History policy differs')
    results = descriptor['result_paths']
    _require(isinstance(results, list) and all(isinstance(path, str) for path in results) and
             results == sorted(set(results)), 'Invalid finished result inventory')
    _result_paths(results, candidate)
    environment = descriptor['environment']
    _require(environment in ('not_included', 'synthetic_resources_only', 'portable_resources_only'), 'Unsupported environment contract')
    expected = set(results) | {DESCRIPTOR, snapshot.CHAT_BUNDLE_PATH}
    if linked:
        _require(descriptor['result_links_path'] == links.NAME, 'Invalid result link path')
        links.validate(_read(root / links.NAME), candidate, results)
        expected.add(links.NAME)
    if environment != 'not_included':
        expected.add(ENVIRONMENT)
        verify = (environment_archive.verify_backup_environment_archive if environment == 'portable_resources_only'
                  else environment_archive.verify_environment_archive)
        verify(root / ENVIRONMENT)
    entries = info['manifest']['files']
    _require({entry['path'] for entry in entries} == expected and
             all(entry['kind'] == ('chat_bundle' if entry['path'] == snapshot.CHAT_BUNDLE_PATH else 'result')
                 for entry in entries), 'Backup payload inventory differs')
    # Bind semantic reads to the same outer snapshot, including concurrent delivery.
    _require(snapshot.verify_snapshot(root)['manifest_digest'] == pin, 'Backup changed during validation')
    return {'status': 'backup_verified', 'manifest_digest': pin, 'version': info['version'],
            'file_count': info['file_count'], 'total_bytes': info['total_bytes'],
            'counts': {**counts, 'results': len(results)}, 'text_policy': expected_text_policy,
            'environment': environment, 'profile_applied': False,
            'full_profile_coverage_verified': False, 'nas_delivery_verified': False}


def add_results(source, destination, *, ready_files, profile, expected_digest):
    """Create a new sealed copy with reviewed results; never edits the old copy."""
    source = snapshot._safe_path(Path(source))
    info = verify_backup(source, expected_digest=expected_digest)
    destination = _new_target(destination)
    _require(not destination.is_relative_to(source) and not source.is_relative_to(destination),
             'New backup must not overlap its original copy')
    profile_path=managed._local(profile,missing=True)
    _require(not destination.is_relative_to(profile_path) and not profile_path.is_relative_to(destination),
             'New backup must not overlap the working profile')
    bindings = _result_bindings(ready_files, profile)
    candidate = _read(source / snapshot.CHAT_BUNDLE_PATH)
    _result_paths(ready_files, candidate)
    descriptor = _read(source / DESCRIPTOR)
    previous = _read(source / links.NAME)['files'] if descriptor.get('result_links_path') else []
    all_bindings = {f['relative']:{'path':f['source_path'], **({'aliases':f['aliases']} if 'aliases' in f else {})} for f in previous}
    # Old-format payloads can be retained, but do not guess their source paths.
    _require(not descriptor['result_paths'] or previous, 'Old copy has results without source bindings; select them again in a new backup')
    with tempfile.TemporaryDirectory(prefix='rehome-results-') as temporary:
        temporary = Path(temporary)
        snapshot.copy_snapshot(source, temporary/'Existing', expected_digest=expected_digest)
        payload = {e['path']:temporary/'Existing'/e['path'] for e in info_manifest(source)['files']
                   if e['path'] not in (snapshot.CHAT_BUNDLE_PATH, DESCRIPTOR, links.NAME)}
        engine = environment_adapter.load_existing_engine(ENGINE_PATH)
        _require(sum(b['bytes'] for b in bindings.values()) <= engine.MAX_TOTAL, 'Results exceed builder limit')
        for relative, binding in bindings.items():
            _require(binding['bytes'] <= engine.MAX_FILE, 'Result exceeds builder limit')
            current = _result_bindings({relative:ready_files[relative]}, profile)[relative]
            _require(current == binding, 'Result changed after review')
            staged = temporary/'Selected'/relative
            _require(snapshot._copy_verified(Path(binding['path']), staged) == (binding['sha256'],binding['bytes']), 'Result changed while copying')
            _require(not engine.sensitive(Path(binding['path']), environment_adapter._read_stable(staged,engine.MAX_FILE)), 'Credential-like result prohibited')
            if relative in payload:
                _require(snapshot._hash_file(payload[relative]) == snapshot._hash_file(staged), 'Existing result differs')
                previous_binding=all_bindings[relative]
                aliases=set(previous_binding.get('aliases',[])) | set(binding.get('aliases',[]))
                aliases.add(previous_binding['path'])
                aliases.discard(binding['path'])
                if aliases:binding={**binding,'aliases':sorted(aliases)}
            payload[relative] = staged
            all_bindings[relative] = binding
        descriptor.update(format='rehome-backup/2', result_paths=sorted(all_bindings), result_links_path=links.NAME)
        snapshot._write_json(temporary/DESCRIPTOR,descriptor)
        snapshot._write_json(temporary/links.NAME,links.make_manifest(candidate,all_bindings))
        payload[DESCRIPTOR]=temporary/DESCRIPTOR
        payload[links.NAME]=temporary/links.NAME
        verify_backup(source,expected_digest=expected_digest)
        snapshot.create_snapshot(destination,version=info['version']+1,device_id='rehome-results',results=payload,
                                 chat_bundle=candidate,parent_digest=expected_digest)
    return verify_backup(destination)


def info_manifest(source):
    return _read(Path(source)/snapshot.MANIFEST_NAME)


def stage_restore(backup, destination, *, expected_digest, receiving_runtime_version,
                  expected_text_policy=filtering.VERBATIM_TEXT):
    """Receive a pinned backup into a fresh folder. This is not profile apply."""
    _runtime(receiving_runtime_version)
    _require(isinstance(expected_digest, str) and len(expected_digest) == 64 and
             all(c in '0123456789abcdef' for c in expected_digest), 'Explicit backup digest required')
    destination = _new_target(destination)
    verify_backup(backup, expected_digest=expected_digest, expected_text_policy=expected_text_policy)
    snapshot.copy_snapshot(backup, destination, expected_digest=expected_digest)
    info = verify_backup(destination, expected_digest=expected_digest, expected_text_policy=expected_text_policy)
    return {**info, 'status': 'backup_staged', 'destination': str(destination),
            'native_profile_created': False, 'profile_applied': False}

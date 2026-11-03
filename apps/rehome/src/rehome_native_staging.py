"""Build an isolated native profile from a pinned full-text backup.

No Codex process, model, credentials, live profile or external project is used.
An externally pinned, empty native schema template is mandatory. The marker
describes provenance and verified local files; it never grants launch/apply.
"""
from __future__ import annotations

import json
import hashlib
import os
import stat
from pathlib import Path
import uuid

import native_adapter as native
import readonly_source
import rehome_backup_module as backup
import rehome_review_sources as managed
import snapshot
import rehome_display_metadata as display

ROOT_MARKER = '.rehome-staging-root.json'
MARKER = 'rehome-native-staging.json'
READY = 'NativeREADY.json'
DATABASES = ('state_5.sqlite', 'thread_history_1.sqlite')
MAX_STAGE_FILES = 100000


def _require(condition, message):
    if not condition:
        raise backup.BackupError(message)


def _overlap(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def _pin(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _schema_signature(db):
    objects = [tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
               "AND name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    _require(all(native._schema_object_allowed(dict(zip(('type', 'name', 'sql'), r))) for r in objects),
             'Unexpected native schema object')
    migrations = [tuple(v.hex() if isinstance(v, bytes) else v for v in r)
                  for r in db.execute('SELECT * FROM _sqlx_migrations ORDER BY version')]
    _require(migrations, 'Native SQLx migration metadata is empty')
    return hashlib.sha256(native._json([objects, migrations]).encode('utf-8')).hexdigest()


def _template(template, pins):
    template = managed._local(template)
    native._marker(template)
    _require({p.relative_to(template).as_posix() for p in template.rglob('*')} == set(DATABASES) | {native.MARKER},
             'Schema template must contain only pinned databases and its marker')
    for path in template.iterdir():
        info = snapshot._safe_path(path).stat()
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Linked schema template files prohibited')
    _require(isinstance(pins, dict) and set(pins) == set(DATABASES), 'Exact schema template pins required')
    for filename in DATABASES:
        pin = pins[filename]
        _require(_pin(pin),
                 'Invalid schema template pin')
        path = template / filename
        snapshot._safe_path(path)
        _require(not any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')),
                 'Use a closed clean schema template without database sidecars')
        _require(snapshot._hash_file(path)[0] == pin, 'Schema template differs from external pin')
        with native._connect(path, readonly=True) as db:
            native._schema_gate(db, readonly_source._schema(backup.SUPPORTED_RUNTIME)[0]
                               if filename.startswith('state') else native.EXPECTED_HISTORY)
            _require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Invalid schema template database')
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT LIKE 'sqlite_%' AND name<>'_sqlx_migrations'")]
            _require(all(db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0] == 0 for table in tables),
                     'Schema template contains user or project records')
            _require(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'").fetchone()[0] == 1,
                     'Native SQLx migration metadata required; static test schema is prohibited')
            _schema_signature(db)
        _require(snapshot._hash_file(path)[0] == pin, 'Schema template changed while checking')
    return template


def _schema_signatures(profile):
    result = {}
    for filename in DATABASES:
        with native._connect(profile / filename, readonly=True) as db:
            result[filename] = _schema_signature(db)
    return result


def _project_map(bundle, mapping):
    needed = {p['id'] for p in bundle['projects']}
    if any(t['project_id'] is None for t in bundle['threads']):
        needed.add('__projectless__')
    _require(isinstance(mapping, dict) and set(mapping) == needed, 'Exact staged project map required')
    for path in mapping.values():
        snapshot._validate_relative(path)
        _require(path.startswith('Projects/'), 'Project roots must be inside staged Projects')
    snapshot._check_collisions(list(mapping.values()))
    return mapping


def _inventory(root):
    inventory = []
    directory_count = 0
    for directory, dirs, files in os.walk(root):
        directory_count += 1
        _require(directory_count <= MAX_STAGE_FILES, 'Native staging directory limit exceeded')
        for name in dirs:
            snapshot._safe_path(Path(directory) / name)
        for name in files:
            path = snapshot._safe_path(Path(directory) / name)
            _require(path.stat().st_nlink == 1, 'Hard-linked native staging files prohibited')
            relative = path.relative_to(root).as_posix()
            if relative == READY:
                continue
            digest, size = snapshot._hash_file(path)
            inventory.append({'path': relative, 'sha256': digest, 'size': size})
            _require(len(inventory) <= MAX_STAGE_FILES, 'Native staging file limit exceeded')
    return sorted(inventory, key=lambda entry: entry['path'])


def _layout(job, bundle, roots):
    descriptor = backup._read(job / 'Package' / backup.DESCRIPTOR)
    manifest = backup._read(job / 'Package' / snapshot.MANIFEST_NAME)
    files = {MARKER} | {'Package/' + p for p in (snapshot.META_NAME, snapshot.MANIFEST_NAME, snapshot.READY_NAME)}
    files.update('Package/' + e['path'] for e in manifest['files'])
    if descriptor['environment'] == 'portable_resources_only':
        import rehome_environment_stage
        files.update(rehome_environment_stage.layout_files(job))
    files.update('Profile/' + p for p in (*DATABASES, '.codex-global-state.json', 'session_index.jsonl', 'import-report.json'))
    metadata=backup._read(job/'Package'/snapshot.CHAT_BUNDLE_PATH).get('display_metadata')
    for t in bundle['threads']:
        files.add('Profile/'+display.journal_relative(t,metadata).as_posix())
    directories = {Path(p).relative_to(job).as_posix() for p in roots.values()}
    for relative in descriptor['result_paths']:
        parts = relative.split('/')
        target = Path(roots[parts[1]]).joinpath(*parts[2:])
        files.add(target.relative_to(job).as_posix())
        _require(snapshot._hash_file(target) == snapshot._hash_file(job / 'Package' / relative),
                 'Staged finished result differs from Package')
    for relative in files | set(directories):
        directories.update(p.as_posix() for p in Path(relative).parents if p != Path('.'))
    actual = {e['path'] for e in _inventory(job)}
    _require(actual == files, 'Unexpected or missing native staging files')
    _require({p.relative_to(job).as_posix() for p in job.rglob('*') if p.is_dir()} == directories,
             'Unexpected or missing native staging directories')


def _ui_readback(profile, bundle, roots, *, logical_profile=None,display_metadata=None):
    logical_profile=profile if logical_profile is None else logical_profile
    host = 'local:' + str(logical_profile)
    expected = {
        'local-projects': {p['id']: {**p, 'rootPaths': [roots[p['id']]], 'createdAt': 0, 'updatedAt': 0}
                           for p in bundle['projects']},
        'project-order': [p['id'] for p in bundle['projects']],
        'thread-project-assignments': {t['id']: {'projectKind': 'local', 'projectId': t['project_id']}
                                       for t in bundle['threads'] if t['project_id']},
        'thread-projectless-output-directories': {t['id']: roots['__projectless__']
                                                 for t in bundle['threads'] if t['project_id'] is None},
        'thread-workspace-root-hints': {t['id']: roots[t['project_id'] or '__projectless__'] for t in bundle['threads']},
        'thread-project-membership-host-ids': {t['id']: 'local' for t in bundle['threads']},
        'app-server-project-id-by-legacy-project-id-by-host': {host: {p['id']: p['id'] for p in bundle['projects']}},
        'app-server-projects-migration-by-host': {host: {'version': 1, 'projectsMigrated': True,
            'threadAssignmentsMigrated': True, 'pendingThreadAssignmentIds': []}}}
    if display_metadata is not None:
        for pid,values in display_metadata['projects'].items():
            expected['local-projects'][pid].update(createdAt=values['created_at_ms'],updatedAt=values['updated_at_ms'])
        expected['project-order']=display_metadata['project_order']
    _require(backup._read(profile / '.codex-global-state.json') == expected, 'Staged global state differs')
    index = [json.loads(line) for line in (profile / 'session_index.jsonl').read_bytes().splitlines()]
    expected_index=[]
    for t in bundle['threads']:
        updated='1970-01-01T00:00:00Z'
        if display_metadata is not None:
            values=display_metadata['threads'][t['id']]['values']
            updated=display.timestamp(values['updated_at_ms'],milliseconds=True) if values['updated_at_ms'] is not None else display.timestamp(values['updated_at'])
        expected_index.append({'id':t['id'],'thread_name':t['name'] or t['title'],'updated_at':updated})
    _require(index==expected_index,'Staged session index differs')
    _require(backup._read(profile / 'import-report.json') == {'synthetic_only': False,
        'codex_version': backup.SUPPORTED_RUNTIME, 'projects': len(bundle['projects']),
        'threads': len(bundle['threads']), 'native_schema_template': True, 'destination': str(logical_profile),
        'schema_fingerprint': native.SCHEMA_FINGERPRINT}, 'Staged import provenance differs')


def _readback(profile, bundle, project_roots, *, logical_profile=None,display_metadata=None):
    """Check typed rows and exact generated journals, without native execution."""
    logical_profile=profile if logical_profile is None else logical_profile
    with native._connect(profile / DATABASES[0], readonly=True) as state, \
         native._connect(profile / DATABASES[1], readonly=True) as history:
        native._schema_gate(state, readonly_source._schema(backup.SUPPORTED_RUNTIME)[0])
        native._schema_gate(history, native.EXPECTED_HISTORY)
        for db, expected_tables in ((state, native.EXPECTED_STATE), (history, native.EXPECTED_HISTORY)):
            _require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Staged database integrity differs')
            _require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Staged foreign keys differ')
            extras = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT LIKE 'sqlite_%' AND name<>'_sqlx_migrations'") if r[0] not in expected_tables]
            _require(all(db.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] == 0
                         for table in extras), 'Unexpected auxiliary database records')
        rows = {row['id']: dict(row) for row in state.execute('SELECT * FROM threads')}
        _require(set(rows) == {t['id'] for t in bundle['threads']}, 'Staged chat inventory differs')
        _require([dict(r) for r in state.execute('SELECT id,name FROM projects ORDER BY id')] ==
                 sorted(bundle['projects'], key=lambda p: p['id']), 'Staged projects differ')
        _require([dict(r) for r in state.execute('SELECT id,name FROM thread_sections ORDER BY id')] ==
                 sorted(bundle['sections'], key=lambda p: p['id']), 'Staged sections differ')
        if display_metadata is None:
            _require(not state.execute("SELECT 1 FROM projects WHERE metadata<>'{}' OR created_at_ms<>0 OR updated_at_ms<>0").fetchall()
                     and not state.execute('SELECT 1 FROM thread_sections WHERE appearance IS NOT NULL').fetchall(),
                     'Unexpected project or section metadata')
        else:
            display.verify_sql(state,history,bundle,display_metadata)
            _require(not state.execute("SELECT 1 FROM projects WHERE metadata<>'{}'").fetchall(),'Unexpected opaque project metadata')
        _require({tuple(r) for r in state.execute('SELECT * FROM project_roots')} ==
                 {(p['id'], 0, project_roots[p['id']]) for p in bundle['projects']}, 'Staged project roots differ')
        _require({tuple(r) for r in state.execute('SELECT * FROM thread_spawn_edges')} ==
                 {(r['parent'], r['child'], 'completed') for r in bundle['spawn_edges']}, 'Staged spawn edges differ')
        expected_items, expected_turns = 0, 0
        for thread in bundle['threads']:
            row = rows[thread['id']]
            for key in ('title', 'name', 'project_id', 'is_pinned', 'archived'):
                _require(row[key] == thread[key], 'Staged chat metadata differs')
            _require(row['thread_section_id'] == thread['section_id'] and row['originator'] == 'rehome-backup-staging',
                     'Staged membership/provenance differs')
            cwd = project_roots[thread['project_id'] or '__projectless__']
            _require(row['cwd'] == cwd and row['cli_version'] == backup.SUPPORTED_RUNTIME, 'Staged paths/runtime differ')
            _require(row['attached_thread_parent_id'] is None and row['source'] == 'cli' and
                     row['model_provider'] == 'openai' and row['sandbox_policy'] == native._json({'type': 'read-only'}) and
                     row['approval_mode'] == 'untrusted' and row['history_mode'] == 'paginated',
                     'Staged execution policy or attachment metadata differs')
            turns = [dict(r) for r in history.execute('SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal', (thread['id'],))]
            _require([(t['turn_id'], t['status']) for t in turns] == [(t['id'], t['status']) for t in thread['turns']],
                     'Staged turn order/status differs')
            journal = managed._local(row['rollout_path'],missing=True)
            _require(journal.is_relative_to(logical_profile) and journal.parent == logical_profile/('archived_sessions' if thread['archived'] else 'sessions'),
                     'Staged journal location differs')
            journal=profile/journal.relative_to(logical_profile)
            if display_metadata is not None:
                _require(journal.stat().st_mtime_ns==display_metadata['threads'][thread['id']]['journal_mtime_ns'],
                         'Staged journal display modification time differs')
            records = [json.loads(line) for line in journal.read_bytes().splitlines()]
            created='1970-01-01T00:00:00Z' if display_metadata is None else display.timestamp(display_metadata['threads'][thread['id']]['values']['created_at'])
            _require(records and records[0]['payload']['originator'] == 'rehome-backup-staging' and
                     records[0]['payload']['cwd'] == cwd and records[0]['payload']['id'] == thread['id'],
                     'Staged journal provenance differs')
            expected_records = [{'timestamp': created, 'ordinal': 0, 'type': 'session_meta',
                'payload': {'id': thread['id'], 'timestamp': created, 'cwd': cwd,
                    'originator': 'rehome-backup-staging', 'cli_version': backup.SUPPORTED_RUNTIME,
                    'source': 'cli', 'model_provider': 'openai', 'history_mode': 'paginated'}}]
            ordinal = 1
            for turn in thread['turns']:
                start = ordinal
                items = [json.loads(r[0]) for r in history.execute(
                    'SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY rollout_ordinal', (thread['id'], turn['id']))]
                _require(items == [native._native_item(m, append_attachment_notice=False) for m in turn['messages']],
                         'Staged visible text differs')
                for message in turn['messages']:
                    payload = {'type': 'message', 'id': message['id'], 'role': message['role'], 'content': [{
                        'type': 'input_text' if message['role'] == 'user' else 'output_text', 'text': message['text']}]}
                    if message['role'] == 'assistant':
                        payload['phase'] = message['phase']
                    stamp='1970-01-01T00:00:00Z' if display_metadata is None else display.timestamp(
                        display_metadata['threads'][thread['id']]['turns'][turn['id']]['items'][message['id']]['created_at_ms'],milliseconds=True)
                    expected_records.append({'timestamp': stamp, 'ordinal': ordinal,
                                             'type': 'response_item', 'payload': payload})
                    ordinal += 1
                if not turn['messages']:
                    ordinal += 1
                turn_row = next(t for t in turns if t['turn_id'] == turn['id'])
                expected_turn={'thread_id': thread['id'], 'turn_id': turn['id'], 'rollout_ordinal': start,
                    'status': turn['status'], 'error_json': None, 'started_at': 0, 'completed_at': 0, 'duration_ms': 0,
                    'first_user_item_id': next((m['id'] for m in turn['messages'] if m['role'] == 'user'), None),
                    'final_agent_item_id': next((m['id'] for m in reversed(turn['messages']) if m['role'] == 'assistant'
                                               and m['phase'] == 'final_answer'), None),
                    'rollout_byte_offset': None, 'rollout_end_ordinal': ordinal - 1,
                    'rollout_end_byte_offset': None, 'root_turn_id': turn['id']}
                if display_metadata is not None:expected_turn.update(display_metadata['threads'][thread['id']]['turns'][turn['id']]['values'])
                _require(turn_row==expected_turn,'Staged turn metadata differs')
                item_rows = [dict(r) for r in history.execute('SELECT * FROM thread_items WHERE thread_id=? AND turn_id=? '
                              'ORDER BY rollout_ordinal', (thread['id'], turn['id']))]
                for offset, (item_row, message) in enumerate(zip(item_rows, turn['messages'])):
                    item = native._native_item(message, append_attachment_notice=False)
                    expected_item={'thread_id': thread['id'], 'turn_id': turn['id'], 'item_id': message['id'],
                        'rollout_ordinal': start + offset, 'created_at_ms': 0, 'item_json': native._json(item),
                        'item_type': item['type'], 'updated_at_ordinal': start + offset,
                        'started_at_ms': None, 'completed_at_ms': None}
                    if display_metadata is not None:expected_item.update(display_metadata['threads'][thread['id']]['turns'][turn['id']]['items'][message['id']])
                    _require(item_row==expected_item,'Staged item metadata differs')
                expected_items += len(items)
            _require(records == expected_records, 'Staged journal messages/metadata differ')
            _require(tuple(history.execute('SELECT * FROM thread_history_projection_state WHERE thread_id=?',
                     (thread['id'],)).fetchone()) == (thread['id'], journal.stat().st_size, ordinal),
                     'Staged history projection differs')
            expected_turns += len(turns)
        _require(history.execute('SELECT COUNT(*) FROM thread_items').fetchone()[0] == expected_items and
                 history.execute('SELECT COUNT(*) FROM thread_turns').fetchone()[0] == expected_turns and
                 history.execute('SELECT COUNT(*) FROM thread_realtime_items').fetchone()[0] == 0 and
                 history.execute('SELECT COUNT(*) FROM thread_history_projection_state').fetchone()[0] == len(rows),
                 'Unexpected staged history records')
    _ui_readback(profile, bundle, project_roots,logical_profile=logical_profile,display_metadata=display_metadata)


def prepare_native(backup_path, staging_root, *, expected_digest, runtime_version,
                   schema_template, template_pins, project_map, forbidden_profiles=()):
    """Create Staging/<uuid>/Profile; caller supplies pinned template provenance.

    project_map maps each selected project ID (and __projectless__ if needed)
    to a distinct relative directory inside the new job. Only staged project
    directories are created. This API never marks real content synthetic.
    """
    backup._runtime(runtime_version)
    received = managed._local(backup_path)
    info = backup.verify_backup(received, expected_digest=expected_digest)
    _require(isinstance(expected_digest, str) and info['manifest_digest'] == expected_digest,
             'An external backup pin is required')
    template = _template(schema_template, template_pins)
    root = managed._local(staging_root, missing=True)
    _require(all(part.casefold() != '.codex' for part in root.parts), 'Live profile paths prohibited')
    known = [Path.home() / '.codex', Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Codex']
    known.extend(Path(path) for path in forbidden_profiles)
    _require(not any(_overlap(root, managed._local(path, missing=True)) for path in [received, template, *known]),
             'Staging root overlaps source/template/live profiles')
    candidate = backup._read(received / snapshot.CHAT_BUNDLE_PATH)
    bundle = candidate['transfer']
    _require(bundle['synthetic_only'] is False, 'Backup provenance must remain non-synthetic')
    native._validate_transfer(bundle, synthetic_only=False)
    _project_map(bundle, project_map)
    # Root creation follows every package/template/map check.
    if root.exists():
        _require(snapshot._read_json(root / ROOT_MARKER) == {'format': 'rehome-staging-root/1'}, 'Unowned staging root')
    else:
        root.mkdir(parents=True)
        snapshot._write_json(root / ROOT_MARKER, {'format': 'rehome-staging-root/1'})
    job = root / str(uuid.uuid4())
    job.mkdir()
    backup.stage_restore(received, job / 'Package', expected_digest=expected_digest,
                         receiving_runtime_version=runtime_version)
    candidate = backup._read(job / 'Package' / snapshot.CHAT_BUNDLE_PATH)
    bundle = candidate['transfer']
    roots = {pid: str(job.joinpath(*relative.split('/'))) for pid, relative in project_map.items()}
    for path in roots.values():
        Path(path).mkdir(parents=True)
    native._write_profile(bundle, job / 'Profile', roots, schema_template=template,
                          synthetic_only=False, runtime_version=runtime_version,display_metadata=candidate.get('display_metadata'))
    _template(template, template_pins)
    for relative in backup._read(job / 'Package' / backup.DESCRIPTOR)['result_paths']:
        parts = relative.split('/')
        target = Path(roots[parts[1]]).joinpath(*parts[2:])
        snapshot._copy_verified(job / 'Package' / relative, target)
    _readback(job / 'Profile', bundle, roots,display_metadata=candidate.get('display_metadata'))
    if backup._read(job/'Package'/backup.DESCRIPTOR)['environment'] == 'portable_resources_only':
        import rehome_environment_stage
        rehome_environment_stage.prepare(job)
    _require(not (job / 'Profile' / native.MARKER).exists(), 'Real staging must not carry synthetic marker')
    marker = {'format': 'rehome-native-staging/1', 'contains_real_data': True,
        'backup_manifest_sha256': expected_digest, 'text_policy': candidate['text_policy'],
        'runtime_version': runtime_version, 'source_schema_version': candidate['source_schema_version'],
        'schema_template_pins': template_pins, 'schema_signatures': _schema_signatures(template),
        'profile_applied': False, 'native_launch_verified': False,
        'environment_activated': False, 'project_roots': roots}
    snapshot._write_json(job / MARKER, marker)
    _require(_schema_signatures(job / 'Profile') == marker['schema_signatures'], 'Staged native schema differs')
    _layout(job, bundle, roots)
    snapshot._write_json(job / READY, {'format': 'rehome-native-staging-ready/1',
        'backup_manifest_sha256': expected_digest, 'files': _inventory(job)})
    stage_digest = snapshot._hash_file(job / READY)[0]
    try:
        return verify_native_stage(job, expected_digest=expected_digest, expected_stage_digest=stage_digest)
    except BaseException:
        (job / READY).unlink(missing_ok=True)
        raise


def verify_native_stage(job, *, expected_digest, expected_stage_digest):
    """Both pins must come from the caller's receipt outside the checked job."""
    _require(_pin(expected_digest) and _pin(expected_stage_digest), 'External backup and native staging pins required')
    job = managed._local(job)
    _require(snapshot._hash_file(job / READY)[0] == expected_stage_digest, 'Native READY differs from external pin')
    marker = backup._read(job / MARKER)
    _require(isinstance(marker, dict) and set(marker) == {'format', 'contains_real_data', 'backup_manifest_sha256',
        'text_policy', 'runtime_version', 'source_schema_version', 'schema_template_pins', 'schema_signatures',
        'profile_applied', 'native_launch_verified', 'environment_activated', 'project_roots'}, 'Invalid native staging marker')
    _require(marker.get('format') == 'rehome-native-staging/1' and marker.get('contains_real_data') is True and
             marker.get('backup_manifest_sha256') == expected_digest and marker.get('profile_applied') is False and
             marker.get('native_launch_verified') is False and marker.get('environment_activated') is False,
             'Native staging provenance differs')
    ready = backup._read(job / READY)
    _require(ready == {'format': 'rehome-native-staging-ready/1', 'backup_manifest_sha256': expected_digest,
                       'files': _inventory(job)}, 'Staged native files changed or are incomplete')
    info = backup.verify_backup(job / 'Package', expected_digest=expected_digest)
    candidate = backup._read(job / 'Package' / snapshot.CHAT_BUNDLE_PATH)
    native._validate_transfer(candidate['transfer'], synthetic_only=False)
    _require(marker['text_policy'] == candidate['text_policy'] and marker['runtime_version'] == backup.SUPPORTED_RUNTIME and
             marker['source_schema_version'] == candidate['source_schema_version'], 'Staged policy/version differs')
    _require(isinstance(marker['project_roots'], dict), 'Invalid staged roots')
    mapping = {}
    for pid, root in marker['project_roots'].items():
        local = managed._local(root)
        _require(local.is_relative_to(job / 'Projects'), 'Staged project root escaped')
        mapping[pid] = local.relative_to(job).as_posix()
    _project_map(candidate['transfer'], mapping)
    _require(set(marker['schema_template_pins']) == set(DATABASES) and
             all(_pin(p) for p in marker['schema_template_pins'].values()), 'Invalid schema template pins')
    _require(_schema_signatures(job / 'Profile') == marker['schema_signatures'], 'Staged native schema differs')
    _layout(job, candidate['transfer'], marker['project_roots'])
    _readback(job / 'Profile', candidate['transfer'], marker['project_roots'],display_metadata=candidate.get('display_metadata'))
    _require(not (job / 'Profile' / native.MARKER).exists(), 'Synthetic marker prohibited')
    _require(_inventory(job) == ready['files'], 'Native staging changed during verification')
    _require(snapshot._hash_file(job / READY)[0] == expected_stage_digest, 'Native READY changed during verification')
    return {'status': 'native_backup_staged', 'job': str(job), 'profile': str(job / 'Profile'),
            'backup_manifest_sha256': expected_digest, 'stage_digest': expected_stage_digest,
            'counts': info['counts'], 'contains_real_data': True,
            'profile_applied': False, 'native_launch_verified': False, 'environment_activated': False,
            'nas_delivery_verified': False}

"""Check a selected local projection against a stable, fully delivered journal.

No payloads are returned or logged. This checks boundaries and lineage, not a
semantic equivalence proof between every projected item and raw event.
"""
import hashlib
import json
import re
from pathlib import Path
from rehome_history_filter import unique_object


class BoundaryError(ValueError):
    pass


def journal_path(root, value):
    # Codex stores the Win32 extended spelling after resume. Normalize only an
    # absolute drive path; device namespaces and UNC locations stay rejected.
    if isinstance(value, str) and re.match(r'^\\\\\?\\[A-Za-z]:\\', value):
        value = value[4:]
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to(root):
        raise BoundaryError('Journal is outside the selected profile')
    relative = path.relative_to(root)
    if (not relative.parts or relative.parts[0] not in ('sessions', 'archived_sessions') or
            '..' in relative.parts or path.suffix != '.jsonl'):
        raise BoundaryError('Unsupported journal location or compression')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise BoundaryError('Journal links are prohibited')
    return path


def check(conn, root, thread_id):
    row = conn.execute('SELECT rollout_path,history_mode FROM threads WHERE id=?', (thread_id,)).fetchone()
    if row is None or row['history_mode'] != 'paginated':
        raise BoundaryError('Unsupported source history mode')
    path = journal_path(root, row['rollout_path'])
    projection = conn.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM history.thread_history_projection_state WHERE thread_id=?', (thread_id,)).fetchone()
    if projection is None:
        raise BoundaryError('History projection boundary missing')
    before = path.stat()
    if before.st_size > 256 * 1024 * 1024:
        raise BoundaryError('Journal exceeds this verifier limit')
    if projection[0] != before.st_size:
        raise BoundaryError('History projection has not reached the full journal')
    digest, records, previous = hashlib.sha256(), 0, -1
    with path.open('rb') as stream:
        while True:
            line = stream.readline(8 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 8 * 1024 * 1024 or not line.endswith(b'\n'):
                raise BoundaryError('Oversized or truncated journal record')
            try:
                event = json.loads(line, object_pairs_hook=unique_object)
            except (ValueError, UnicodeError):
                raise BoundaryError('Invalid journal record') from None
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise BoundaryError('Unsupported journal event')
            ordinal = event.get('ordinal')
            if type(ordinal) is not int or ordinal <= previous:
                raise BoundaryError('Journal ordinals are absent, repeated or out of order')
            if records == 0:
                meta = event.get('payload')
                if event['type'] != 'session_meta' or not isinstance(meta, dict) or meta.get('id') != thread_id or meta.get('history_mode') != 'paginated':
                    raise BoundaryError('Journal identity or history mode differs')
                if any(meta.get(k) is not None for k in ('history_base', 'forked_from_id', 'forked_from_ordinal_exclusive')):
                    raise BoundaryError('Fork history needs dependency verification')
            elif event['type'] == 'session_meta':
                raise BoundaryError('Repeated session metadata')
            digest.update(line)
            previous, records = ordinal, records + 1
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after):
        raise BoundaryError('Journal changed while checking')
    if not records or projection[1] != previous + 1:
        raise BoundaryError('Projection ordinal does not match the journal boundary')
    for table, columns in (('thread_items', ('rollout_ordinal', 'updated_at_ordinal')),
                           ('thread_turns', ('rollout_ordinal', 'rollout_end_ordinal'))):
        for column in columns:
            maximum = conn.execute(f'SELECT MAX({column}) FROM history.{table} WHERE thread_id=?', (thread_id,)).fetchone()[0]
            if maximum is not None and maximum >= projection[1]:
                raise BoundaryError('Projected history extends beyond the checked journal')
    orphan = conn.execute('SELECT 1 FROM history.thread_items i WHERE i.thread_id=? AND NOT EXISTS '
        '(SELECT 1 FROM history.thread_turns t WHERE t.thread_id=i.thread_id AND t.turn_id=i.turn_id) LIMIT 1', (thread_id,)).fetchone()
    if orphan:
        raise BoundaryError('Projected messages have no corresponding turn')
    return {'thread_id': thread_id, 'journal_sha256': digest.hexdigest(), 'journal_bytes': before.st_size,
            'journal_records': records, 'next_ordinal': projection[1], 'lineage': 'independent',
            'projection_boundary_verified': True, 'semantic_projection_equivalence_verified': False}

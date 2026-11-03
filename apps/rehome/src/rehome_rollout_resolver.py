"""Bounded read-only physical rollout-ID resolver. Metadata proof only.

A physical rollout UUID can differ from its logical SessionMeta.id and need not
be present in state.threads. history.* projection keys remain physical UUIDs.
No message items are selected, exported or semantically approved here.
"""
from pathlib import Path
import json
import os
import re
import stat
import time
import sqlite3

import rehome_history_lineage as lineage
import rehome_journal as journal
import snapshot


class RolloutResolutionError(ValueError):
    def __init__(self, code):
        super().__init__(code); self.code = code


def _require(value, code):
    if not value: raise RolloutResolutionError(code)


def _scan(root, ident, *, max_entries, max_depth, deadline):
    stack = [(root/name, 0) for name in ('sessions','archived_sessions') if (root/name).exists()]
    matches, examined, skipped_links = [], 0, 0
    # Timestamp prefix is deliberately not inferred as authority. Exact UUID
    # suffix, fixed directory scope and verified header supply the binding.
    suffixes = ('-'+ident+'.jsonl', '_'+ident+'.jsonl', '-'+ident+'.jsonl.zst', '_'+ident+'.jsonl.zst')
    while stack:
        directory, depth = stack.pop()
        _require(depth <= max_depth, 'resolver_depth_limit')
        snapshot._safe_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                examined += 1
                _require(examined <= max_entries and time.monotonic() <= deadline, 'resolver_scan_limit')
                info = entry.stat(follow_symlinks=False)
                is_link = stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
                candidate = entry.name.startswith('rollout-') and entry.name.endswith(suffixes)
                if is_link:
                    _require(not candidate, 'resolver_candidate_link')
                    skipped_links += 1; continue
                if stat.S_ISDIR(info.st_mode): stack.append((Path(entry.path),depth+1))
                elif candidate:
                    # Windows DirEntry.stat may omit nlink/file ID; obtain the
                    # complete path stat before treating an exact match as safe.
                    info = snapshot._safe_path(Path(entry.path)).lstat()
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'resolver_candidate_not_regular')
                    matches.append(Path(entry.path))
                    _require(len(matches) <= 1, 'resolver_ambiguous_physical_rollout')
    _require(len(matches) == 1, 'resolver_rollout_not_found')
    return matches[0], {'entries_examined': examined, 'unfollowed_links': skipped_links}


def resolve(profile, rollout_id, *, journal_decoder=None, max_entries=100_000, max_depth=16, max_seconds=10):
    """Resolve exactly one UUID within sessions/archive; caller still needs history checks."""
    lineage._uuid(rollout_id)
    _require(type(max_entries) is int and 1 <= max_entries <= 100_000 and type(max_depth) is int and
             0 <= max_depth <= 32 and type(max_seconds) in (int,float) and 0 < max_seconds <= 30,
             'resolver_invalid_limits')
    deadline = time.monotonic()+max_seconds
    try:
        with lineage.source._snapshot(profile, lineage.source.CURRENT_SCHEMA_VERSION) as (conn, _, root):
            physical, scan = _scan(root, rollout_id, max_entries=max_entries, max_depth=max_depth, deadline=deadline)
            logical = Path(str(physical)[:-4]) if physical.suffix == '.zst' else physical
            data, evidence = journal.read_header(root, logical, decoder=journal_decoder)
            header = json.loads(data, object_pairs_hook=lineage.filtering.unique_object)
            _require(isinstance(header,dict) and header.get('type') == 'session_meta' and
                     type(header.get('ordinal')) is int and header['ordinal'] >= 0 and
                     isinstance(header.get('payload'),dict), 'resolver_header_unsupported')
            meta = header['payload']; owner = lineage._uuid(meta.get('id'))
            _require(meta.get('history_mode') == 'paginated', 'resolver_history_mode_unsupported')
            base = meta.get('history_base')
            if base is not None:
                _require(isinstance(base,dict) and set(base) == {'thread_id','end_ordinal_exclusive','end_byte_offset'},
                         'resolver_base_unsupported')
                lineage._uuid(base['thread_id'])
                _require(all(type(base[k]) is int and base[k] > 0 for k in ('end_ordinal_exclusive','end_byte_offset')),
                         'resolver_base_unsupported')
            provenance = meta.get('forked_from_id')
            if provenance is not None: lineage._uuid(provenance)
            version = meta.get('cli_version','unspecified')
            _require(isinstance(version,str) and re.fullmatch(r'[0-9A-Za-z.+-]{1,64}',version) is not None,
                     'resolver_writer_version_unsupported')
            projection = conn.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM history.thread_history_projection_state WHERE thread_id=?',
                                      (rollout_id,)).fetchone()
            _require(projection is not None and all(type(n) is int and n >= 0 for n in projection),
                     'resolver_projection_missing')
            turns = conn.execute('SELECT COUNT(*),MIN(rollout_ordinal),MAX(rollout_end_ordinal) FROM history.thread_turns WHERE thread_id=?',
                                 (rollout_id,)).fetchone()
            item_count = conn.execute('SELECT COUNT(*) FROM history.thread_items WHERE thread_id=?', (rollout_id,)).fetchone()[0]
            _require(conn.execute('SELECT id FROM threads WHERE id=?',(owner,)).fetchone() is not None,
                     'resolver_logical_owner_missing')
            second, second_scan = _scan(root, rollout_id, max_entries=max_entries,max_depth=max_depth,deadline=deadline)
            _require(second == physical, 'resolver_changed_during_read')
            _, after = journal.read_header(root, logical, decoder=journal_decoder)
            _require(evidence == after, 'resolver_changed_during_read')
            return {'format':'rehome-rollout-metadata-binding/1', 'status':'storage_metadata_resolved',
                'physical_rollout_id':rollout_id, 'logical_session_id':owner,
                'projection_storage_key':rollout_id, 'relative_physical_path':str(physical.relative_to(root)),
                'header_ordinal':header['ordinal'], 'writer_version':version,
                'history_base':base, 'fork_provenance_id':provenance, 'header_evidence':evidence,
                'projection_next_byte_offset':projection[0], 'projection_next_ordinal':projection[1],
                'own_turn_count':turns[0], 'own_turn_min_start':turns[1], 'own_turn_max_end':turns[2],
                'projected_item_count':item_count, 'scan':scan, 'repeat_scan':second_scan,
                'history_semantics_verified':False, 'message_text_read':False, 'read_only':True,
                'publication_ready':False, 'profile_applied':False, 'user_approved':False,
                'atomic_source_snapshot':False}
    except RolloutResolutionError: raise
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):
        raise RolloutResolutionError('resolver_source_unsupported_or_changed') from None


def walk_metadata(profile, first_rollout_id, *, first_cutoff=None, journal_decoder=None, max_chain=128):
    """Walk storage references only, reporting structural bounds without exporting messages."""
    _require(type(max_chain) is int and 1 <= max_chain <= 128,'resolver_invalid_chain_limit')
    seen, bindings, ident, cutoff = set(), [], first_rollout_id, first_cutoff
    while ident is not None:
        _require(ident not in seen and len(seen) < max_chain,'resolver_cycle_or_chain_limit')
        seen.add(ident)
        binding = resolve(profile,ident,journal_decoder=journal_decoder)
        if cutoff is not None:
            _require(set(cutoff)=={'end_ordinal_exclusive','end_byte_offset'} and
                all(type(n) is int and n > 0 for n in cutoff.values()),'resolver_invalid_cutoff')
            _require(binding['header_ordinal'] < cutoff['end_ordinal_exclusive'] <= binding['projection_next_ordinal'] and
                     cutoff['end_byte_offset'] <= binding['projection_next_byte_offset'],'resolver_cutoff_exceeds_projection')
            physical=Path(profile)/binding['relative_physical_path']
            logical=Path(str(physical)[:-4]) if physical.suffix=='.zst' else physical
            previous,last,count,gaps=None,None,0,0
            with journal.open_journal(Path(profile),logical,byte_limit=cutoff['end_byte_offset'],decoder=journal_decoder) as stream:
                while line:=stream.readline(journal.MAX_LINE_READ):
                    event=json.loads(line,object_pairs_hook=lineage.filtering.unique_object)
                    ordinal=event.get('ordinal')
                    _require(type(ordinal) is int,'resolver_prefix_ordinal_unsupported')
                    gaps += previous is not None and ordinal != previous+1
                    previous=ordinal;count+=1
                    last={'ordinal':ordinal,'type':event.get('type'),
                          'terminal_kind':event.get('payload',{}).get('type') if event.get('type')=='event_msg' else None}
            binding['requested_prefix_structure']={'record_count':count,'ordinal_gaps':gaps,
                'end_ordinal_matches':previous==cutoff['end_ordinal_exclusive']-1,
                'last_record_type':last['type'] if last['type'] in ('session_meta','event_msg','response_item','turn_context') else 'unknown',
                'last_event_kind':last['terminal_kind'] if last['terminal_kind'] in ('task_complete','turn_aborted','item_completed','token_count','turn_diff') else 'other_or_absent',
                'complete_terminal_boundary':gaps==0 and previous==cutoff['end_ordinal_exclusive']-1 and
                    last['type']=='event_msg' and last['terminal_kind'] in ('task_complete','turn_aborted')}
            # Metadata coverage can describe a non-terminal edge; this is never
            # promoted to a history-semantic success by this resolver.
        bindings.append(binding)
        base=binding['history_base']
        if base is None: ident,cutoff=None,None
        else:
            ident=base['thread_id'];cutoff={k:base[k] for k in ('end_ordinal_exclusive','end_byte_offset')}
    return {'format':'rehome-storage-chain-metadata/1','status':'storage_metadata_chain_resolved',
            'depth':len(bindings),'bindings':bindings,'reached_standalone_storage':True,
            'history_semantics_verified':False,'publication_ready':False,'profile_applied':False,
            'user_approved':False,'real_message_text_saved':False,'atomic_source_snapshot':False}

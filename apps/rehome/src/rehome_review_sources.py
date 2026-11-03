"""Synthetic managed local review bindings. Never a transport/publication manifest.

The caller supplies the managed root independently of the pinned review. No
decoder discovery, profile writes, file copies, server or approval are provided.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import stat
import sqlite3
import uuid
from datetime import datetime, timezone

import rehome_review as review
import snapshot

DEFAULT_ROOT = Path(__file__).resolve().parent / 'output/ReHome/managed-review'
KNOWN_EXCLUDED_ROOTS = (Path.home() / 'Documents/SynologyDrive',
                        Path(__file__).resolve().parent / 'output/ReHome/receiver-program-08')
CLASSIFICATION = 'selected_finished_generated'
REAL_MANIFEST_LIMIT = 4*1024*1024


def real_review_limits():
    return {**review.REVIEW_LIMITS, 'review-manifest.json': REAL_MANIFEST_LIMIT}


class SourceReviewError(review.ReviewIntegrityError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _local(value, *, missing=False):
    path = Path(value)
    review._require(path.is_absolute() and '..' not in path.parts and
                    not str(path).startswith(('\\\\', '//')) and
                    all(':' not in p and not p.endswith((' ', '.')) for p in path.parts[1:]),
                    'Explicit local absolute path required')
    return snapshot._safe_path(path, allow_missing=missing)


def _normal(path):
    return os.path.normcase(str(path))


def _identity(path):
    info = snapshot._safe_path(path).lstat()
    review._require(stat.S_ISDIR(info.st_mode) and info.st_ino > 0, 'Managed root is not an ordinary directory')
    return [info.st_dev, info.st_ino]


def _overlap(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def _guard_location(managed_root, destination, *, new=False):
    root, destination = _local(managed_root), _local(destination, missing=new)
    for forbidden in KNOWN_EXCLUDED_ROOTS:
        review._require(not _overlap(root, forbidden), 'Managed root overlaps excluded sync/published directory')
    review._require(destination.parent == root and str(uuid.UUID(destination.name)) == destination.name,
                    'Review must be a UUID child of the caller managed root')
    review._require(not new or not destination.exists(), 'Managed destination already exists')
    return root, destination, _identity(root)


def _guard_root(managed_root, profile, destination, *, new=False):
    root, destination, identity = _guard_location(managed_root, destination, new=new)
    # Prepared integrity remains readable even if the source later disappears;
    # actual source availability is checked only by verify_sources.
    profile = _local(profile, missing=True)
    review._require(not _overlap(root, profile), 'Managed root overlaps source profile')
    return root, profile, destination, identity


def _result_binding(item):
    review._exact(item, {'path', 'display_name', 'classification'})
    review._require(item['classification'] == CLASSIFICATION, 'Explicit generated finished result classification required')
    path = _local(item['path'])
    name = snapshot._validate_relative(item['display_name'])
    before = path.lstat()
    review._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_ino > 0,
                    'Result requires a regular file without hard links')
    checksum, size = snapshot._hash_file(path)
    after = _local(path).lstat()
    review._require(snapshot._fingerprint(before) == snapshot._fingerprint(after) and
                    snapshot._content_state(before) == snapshot._content_state(after) and after.st_nlink == 1,
                    'Result changed during read')
    return {'path': _normal(path), 'display_name': name, 'classification': CLASSIFICATION,
            'physical_identity': [before.st_dev, before.st_ino], 'bytes': size, 'sha256': checksum}


def _exclusions(value):
    review._require(isinstance(value, dict))
    result = {}
    for key, entry in value.items():
        review._require(isinstance(key, str) and len(key.split('/')) == 3)
        origin, turn, message = key.split('/')
        review.lineage.native._thread_ident(origin)
        review._require(bool(turn) and bool(message))
        review._exact(entry, {'message_sha256', 'text_ranges'})
        review._sha(entry['message_sha256'])
        review._require(isinstance(entry['text_ranges'], dict) and bool(entry['text_ranges']))
        for block, ranges in entry['text_ranges'].items():
            review._require(isinstance(block, str) and re.fullmatch('0|[1-9][0-9]*', block) is not None)
            review._require(isinstance(ranges, list) and bool(ranges))
            end = 0
            for pair in ranges:
                review._require(isinstance(pair, list) and len(pair) == 2 and
                                all(type(n) is int for n in pair) and end <= pair[0] < pair[1] <= 2**63-1)
                end = pair[1]
        # Contains identities, message digest, text block indices and Unicode
        # codepoint ranges only; never stores the removed material.
        result[key] = entry
    return result


def _render(candidate, results, *, real_local=False):
    labels = {}
    for thread in candidate['transfer']['threads']:
        for turn_index, turn in enumerate(thread['turns'], 1):
            for message_index, message in enumerate(turn['messages'], 1):
                labels[(thread['id'], turn['id'], message['id'])] = (
                    (thread['name'] or thread['title']) + ' · ход ' + str(turn_index) +
                    ' · сообщение ' + str(message_index))
    rows = []
    for entry in candidate['filtering']:
        attachments, ranges = entry['omitted_attachment_blocks'], entry['excluded_text_ranges']
        if attachments or ranges:
            label = labels[tuple(entry[k] for k in ('target_thread_id', 'turn_id', 'message_id'))]
            rows.append('<li>' + review._escape(label) + ' · вложений: ' + str(attachments) +
                        ' · диапазонов: ' + str(ranges) +
                        ' · причина: исходное вложение / явно выбранный фрагмент исходного материала</li>')
    section = ('<section><h2>Применённые исключения</h2><ul>' + ''.join(rows) +
               '</ul><p>Готовые результаты — явно выбранные созданные файлы.</p>'
               '<p>Состав подготовлен. Актуальность источников проверяется отдельно на момент чтения. '
               'Согласование пользователя пока отсутствует.</p>' +
               ('<p>Выбранные реальные чаты. Остальные чаты не входят в эту проверку. Согласование и публикация отключены.</p></section>'
                if real_local else '<p>Синтетическая проверка. Согласование и публикация отключены.</p></section>'))
    page = review.render(candidate, results, synthetic_only=False) if real_local else review.render(candidate, results)
    head, footer = page.rsplit('<footer class="muted">', 1)
    return head + section + '<footer class="muted">' + footer


def _summary_results(bindings):
    return [{k: entry[k] for k in ('display_name', 'sha256', 'bytes')} for entry in bindings]


def _write_new(path, data):
    with path.open('xb') as stream:
        stream.write(data)


def prepare(profile, selected_ids, destination, *, managed_root, ready_results=(), exclusions=None,
            journal_decoder=None):
    """Prepare four new local files; synthetic guard deliberately remains mandatory.

    The trusted caller creates/selects the root; production CLI uses DEFAULT_ROOT.
    Failure leaves an incomplete, unverifiable directory, never a success marker.
    """
    profile = review.context_probe._guard_profile_for_launch(_local(profile))
    root, profile, destination, identity = _guard_root(managed_root, profile, destination, new=True)
    exclusions = _exclusions({} if exclusions is None else exclusions)
    candidate = review.lineage.export_candidate(profile, selected_ids, exclusions=exclusions,
                                                journal_decoder=journal_decoder)
    review._validate_candidate(candidate)
    bindings = [_result_binding(item) for item in ready_results]
    results = _summary_results(bindings)
    snapshot._check_collisions([r['display_name'] for r in results])
    payloads = {'candidate.json': snapshot._canonical(candidate), 'results.json': snapshot._canonical(results),
                'review.html': _render(candidate, results).encode('utf-8')}
    manifest = {'format': 'rehome-managed-local-review/2', 'synthetic_only': True,
                'publication_ready': False, 'profile_applied': False, 'user_approved': False,
                'candidate_sha256': review.lineage.filtering.digest(candidate),
                'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest()} for name, data in payloads.items()],
                'local_bindings': {'managed_root': _normal(root), 'managed_root_identity': identity,
                    'profile': _normal(profile), 'source_instance_id': candidate['source_instance_id'],
                    'source_schema_version': candidate['source_schema_version'],
                    'selected_thread_ids': candidate['selected_thread_ids'], 'dependency_ids': candidate['dependency_ids'],
                    'exclusions': exclusions, 'exclusions_sha256': review.lineage.filtering.digest(exclusions),
                    'results': bindings}}
    payloads['review-manifest.json'] = snapshot._canonical(manifest)
    review._require(all(0 < len(data) <= review.REVIEW_LIMITS[name] for name, data in payloads.items()),
                    'Managed review exceeds bounded file limits')
    review._require(_identity(root) == identity, 'Managed root changed before write')
    destination.mkdir()
    created_identity = _identity(destination)
    for name, data in payloads.items():
        review._require(_identity(root) == identity and _identity(destination) == created_identity,
                        'Managed root/destination changed during write')
        _write_new(snapshot._safe_path(destination / name, allow_missing=True), data)
    pin = hashlib.sha256(payloads['review-manifest.json']).hexdigest()
    verify_prepared(destination, pin, managed_root=root)
    review._require(_identity(root) == identity and _identity(destination) == created_identity,
                    'Managed root/destination changed after write')
    return {'status': 'managed_local_review_prepared', 'path': str(destination / 'review.html'),
            'manifest_sha256': pin, 'synthetic_only': True, 'publication_ready': False,
            'profile_applied': False, 'user_approved': False}


def _read_verified(destination, pin, managed_root, *, real_local=False):
    review._sha(pin)
    # Reject untrusted destination/root before opening any review file.
    _, destination, initial_root_identity = _guard_location(managed_root, destination)
    limits = real_review_limits() if real_local else review.REVIEW_LIMITS
    initial = review._directory_state(destination, limits=limits)
    contents = {name: review._read_prepared(destination, name, initial[1][name], limits=limits) for name in limits}
    review._require(hashlib.sha256(contents['review-manifest.json']).hexdigest() == pin, 'Review differs from caller pin')
    manifest = review._json_bytes(contents['review-manifest.json'])
    review._exact(manifest, {'format', 'synthetic_only', 'publication_ready', 'profile_applied', 'user_approved',
                             'candidate_sha256', 'files', 'local_bindings'} | ({'selection_sha256'} if real_local else set()))
    review._require(manifest['format'] == ('rehome-real-local-review/3' if real_local else 'rehome-managed-local-review/2') and
                    manifest['synthetic_only'] is (not real_local) and
                    all(manifest[k] is False for k in ('publication_ready', 'profile_applied', 'user_approved')))
    review._sha(manifest['candidate_sha256'])
    binding = manifest['local_bindings']
    review._exact(binding, {'managed_root', 'managed_root_identity', 'profile', 'source_instance_id',
                            'source_schema_version', 'selected_thread_ids', 'dependency_ids', 'exclusions',
                            'exclusions_sha256', 'results'} | ({'selection'} if real_local else set()))
    if real_local:
        import rehome_selection as selection
        selected = selection.validate(binding['selection'])
        review._require(selection.pin(selected) == manifest['selection_sha256'])
        for key in ('profile', 'source_instance_id', 'source_schema_version', 'selected_thread_ids', 'exclusions'):
            review._require(selected[key] == binding[key], 'Selection binding differs')
        review._require(selected['inspection'] is not None and selected['inspection']['unresolved_count'] == 0 and
                        selected['inspection']['status'] == 'resolved_for_local_filter')
        review._require(all(item['classification'] == 'generated_finished' for item in selected['files']))
        review._require([(item['path'], item['display_name']) for item in selected['files']] ==
                        [(item['path'], item['display_name']) for item in binding['results']])
        review._require([item['source_binding'] for item in selected['files']] == binding['results'])
    root, profile, destination, identity = _guard_root(managed_root, binding['profile'], destination)
    review._require(identity == initial_root_identity, 'Managed root replaced before manifest binding check')
    review._require(binding['managed_root'] == _normal(root) and binding['managed_root_identity'] == identity and
                    binding['profile'] == _normal(profile), 'Caller root or local binding differs')
    candidate = review._json_bytes(contents['candidate.json'])
    counts = review._validate_candidate(candidate)
    if real_local:selection.validate_candidate_binding(binding['selection'],candidate)
    review._require(review.lineage.filtering.digest(candidate) == manifest['candidate_sha256'])
    for key in ('source_instance_id', 'source_schema_version', 'selected_thread_ids', 'dependency_ids'):
        review._require(binding[key] == candidate[key], 'Source binding differs from candidate')
    exclusions = _exclusions(binding['exclusions'])
    review._require(binding['exclusions_sha256'] == review.lineage.filtering.digest(exclusions))
    used = set()
    for item in candidate['filtering']:
        key = '/'.join(item[k] for k in ('source_thread_id', 'turn_id', 'message_id'))
        if key in exclusions:
            entry = exclusions[key]
            review._require(entry['message_sha256'] == item['message_sha256'] and
                            sum(map(len, entry['text_ranges'].values())) == item['excluded_text_ranges'])
            used.add(key)
        else:
            review._require(item['excluded_text_ranges'] == 0)
    review._require(used == set(exclusions), 'Unused exclusion binding')
    review._require(isinstance(binding['results'], list))
    for item in binding['results']:
        review._exact(item, {'path', 'display_name', 'classification', 'physical_identity', 'bytes', 'sha256'})
        # Validate syntax only here; reading result payloads belongs to verify_sources.
        path = Path(item['path'])
        review._require(path.is_absolute() and '..' not in path.parts and not str(path).startswith(('\\\\', '//')) and
                        all(':' not in p and not p.endswith((' ', '.')) for p in path.parts[1:]) and
                        item['path'] == _normal(path) and item['classification'] == CLASSIFICATION)
        review._require(isinstance(item['physical_identity'], list) and len(item['physical_identity']) == 2 and
                        all(type(n) is int for n in item['physical_identity']) and item['physical_identity'][1] > 0)
        snapshot._validate_relative(item['display_name']); review._sha(item['sha256']); review._count(item['bytes'])
    results = review._json_bytes(contents['results.json'])
    review._require(results == _summary_results(binding['results']))
    snapshot._check_collisions([r['display_name'] for r in results])
    page = _render(candidate, results, real_local=True) if real_local else _render(candidate, results)
    review._require(contents['review.html'] == page.encode('utf-8'), 'Review page differs')
    review._require(isinstance(manifest['files'], list) and len(manifest['files']) == 3)
    seen = set()
    for entry in manifest['files']:
        review._exact(entry, {'path', 'sha256'})
        name = entry['path']
        review._require(name in review._CONTENT_FILES and name not in seen)
        seen.add(name); review._sha(entry['sha256'])
        review._require(hashlib.sha256(contents[name]).hexdigest() == entry['sha256'])
    review._require(review._directory_state(destination, limits=limits) == initial and _identity(root) == identity,
                    'Managed review changed during verification')
    counts['results'] = len(results)
    return manifest, candidate, counts, initial


def verify_prepared(destination, expected_manifest_sha256, *, managed_root):
    """Verify v2 syntax/composition with caller root; never claim source freshness."""
    try:
        manifest, candidate, counts, _ = _read_verified(destination, expected_manifest_sha256, managed_root)
        return {'status': 'managed_local_review_verified', 'manifest_sha256': expected_manifest_sha256,
                'candidate_sha256': manifest['candidate_sha256'], 'counts': counts, 'synthetic_only': True,
                'source_freshness_verified': False, 'result_sources_rechecked': False,
                'publication_ready': False, 'profile_applied': False, 'user_approved': False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, sqlite3.Error):
        raise SourceReviewError('invalid_review', 'Managed review cannot be verified; prepare/review again') from None


def _verify_sources(destination, expected_manifest_sha256, *, managed_root, profile, journal_decoder=None,
                    real_local=False):
    """Re-export exact composition and rehash results. Fresh only during this read.

    Profile is supplied independently, compared to the pinned local binding.
    Missing/changing/active/unknown sources never produce a fresh result.
    """
    try:
        manifest, candidate, counts, initial = _read_verified(destination, expected_manifest_sha256, managed_root,
                                                              real_local=real_local)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, sqlite3.Error):
        raise SourceReviewError('invalid_review', 'Pinned managed review is invalid; prepare/review again') from None
    try:
        binding = manifest['local_bindings']
        profile = (review.lineage.source._source_root(_local(profile)) if real_local else
                   review.context_probe._guard_profile_for_launch(_local(profile)))
        review._require(_normal(profile) == binding['profile'], 'Source profile differs')
        observed = review.lineage.export_candidate(profile, binding['selected_thread_ids'],
                    exclusions=binding['exclusions'], journal_decoder=journal_decoder)
        review._require(snapshot._canonical(observed) == snapshot._canonical(candidate), 'Source composition is stale')
        for entry in binding['results']:
            current = _result_binding({k: entry[k] for k in ('path', 'display_name', 'classification')})
            review._require(current == entry, 'Result changed, replaced or missing')
        # Re-export after result reads, then recheck pinned files/identities. This
        # reduces cross-read races; it cannot promise future filesystem stability.
        again = review.lineage.export_candidate(profile, binding['selected_thread_ids'],
                    exclusions=binding['exclusions'], journal_decoder=journal_decoder)
        review._require(snapshot._canonical(again) == snapshot._canonical(candidate), 'Source changed during result reads')
        for entry in binding['results']:
            review._require(_result_binding({k: entry[k] for k in ('path', 'display_name', 'classification')}) == entry,
                            'Result changed during source verification')
        _, _, _, final = _read_verified(destination, expected_manifest_sha256, managed_root, real_local=real_local)
        review._require(final == initial, 'Review replaced during source verification')
        return {'status': 'sources_verified_at_individual_reads', 'verification_completed_at_utc': datetime.now(timezone.utc).isoformat(),
                'manifest_sha256': expected_manifest_sha256, 'candidate_sha256': manifest['candidate_sha256'],
                'counts': counts, 'synthetic_only': not real_local, 'source_freshness_verified': True,
                'result_sources_rechecked': True, 'user_approved': False, 'publication_ready': False,
                'profile_applied': False, 'future_publication_authorized': False,
                'atomic_source_snapshot': False,
                'freshness_scope': 'Individual bounded reads and observed inter-phase changes; no source locks. '
                    'Publication must reverify and capture exact payload bytes.'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, sqlite3.Error):
        raise SourceReviewError('stale_or_unsupported_retry',
                               'Source/result missing, changed, active or unsupported; retry diagnostic and prepare review again') from None


def verify_sources(destination, expected_manifest_sha256, *, managed_root, profile, journal_decoder=None):
    return _verify_sources(destination, expected_manifest_sha256, managed_root=managed_root,
                           profile=profile, journal_decoder=journal_decoder)

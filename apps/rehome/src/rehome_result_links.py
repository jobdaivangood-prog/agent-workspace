"""Bound local Markdown links; original transcripts are never modified in place."""
import copy
import hashlib
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from urllib.parse import unquote, urlsplit

FORMAT = 'rehome-result-links/1'
NAME = 'ResultLinks.json'
MAX_FILES = 4096
MAX_OCCURRENCES = 100000


def _require(value, message='Invalid result link manifest'):
    if not value:
        raise ValueError(message)


def _destination(href):
    if not isinstance(href, str) or not href or any(ord(c) < 32 for c in href):
        return None
    value = href[1:-1] if href.startswith('<') and href.endswith('>') else href
    if value.startswith('file:'):
        parsed = urlsplit(value)
        if parsed.netloc not in ('', 'localhost') or parsed.query:
            return None
        value = parsed.path
        suffix = '#' + parsed.fragment if parsed.fragment else ''
    elif value.startswith('sandbox:/'):
        value, suffix = value[len('sandbox:'):], ''
    else:
        value, suffix = value, ''
    # Codex file references can carry line/column suffixes.
    match = re.search(r'(:[0-9]+(?::[0-9]+)?|#page=[0-9]+)$', value)
    if match:
        value, suffix = value[:match.start()], match.group() + suffix
    # Encoded delimiters belong to the filename, never to navigation syntax.
    if href.lstrip('<').startswith(('file:', 'sandbox:/')):
        value = unquote(value)
    if re.match(r'^/[A-Za-z]:[/\\]', value):
        value = value[1:]
    if re.match(r'^[A-Za-z]:[/\\]', value):
        if '..' in value.replace('\\', '/').split('/'):
            return None
        path = ntpath.normpath(value).replace('\\', '/')
        return path, 'windows:' + path.casefold(), suffix
    if value.startswith('/') and not value.startswith('//') and '\\' not in value:
        if '..' in value.split('/'):
            return None
        return value, 'posix:' + value, suffix
    return None


def normalize_href(href):
    parsed = _destination(href)
    return parsed[1] if parsed else None


def source_key(path):
    """Identity of a raw filesystem path; percent sequences and suffixes are literal."""
    if not isinstance(path,str) or any(ord(c)<32 for c in path):return None
    if re.match(r'^[A-Za-z]:[/\\]',path):
        return 'windows:'+ntpath.normpath(path).replace('\\','/').casefold()
    if path.startswith('/') and not path.startswith('//') and '\\' not in path:
        return 'posix:'+path
    return None


def extract(text):
    """Return inline Markdown destinations outside fenced and inline code."""
    result, offset, fence, code = [], 0, None, 0
    for line in text.splitlines(keepends=True):
        opening = re.match(r'^\s{0,3}(`{3,}|~{3,})([^\r\n]*)', line)
        if opening and (fence is not None or not code):
            marker = opening.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not opening.group(2).strip():
                fence = None
            offset += len(line)
            continue
        if fence is not None or line.startswith(('    ', '\t')):
            offset += len(line)
            continue
        index = 0
        while index < len(line):
            if line[index] == '\\':
                index += 2
                continue
            if line[index] == '`':
                end = index
                while end < len(line) and line[end] == '`': end += 1
                width = end-index
                if not code: code = width
                elif code == width: code = 0
                index = end
                continue
            if code or not line.startswith('](', index) or '[' not in line[:index]:
                index += 1
                continue
            start = index+2
            while start < len(line) and line[start] in ' \t': start += 1
            end = start
            if start < len(line) and line[start] == '<':
                end = line.find('>', start+1)
                if end < 0: index += 2; continue
                end += 1
                tail = line[end:]
                if not re.match(r'\s*(?:["\'][^\r\n]*["\']\s*)?\)', tail):
                    index += 2; continue
            else:
                depth = 0
                while end < len(line):
                    char = line[end]
                    if char == '(' and (end == start or line[end-1] != '\\'): depth += 1
                    elif char == ')' and (end == start or line[end-1] != '\\'):
                        if depth == 0: break
                        depth -= 1
                    if char in '\r\n': break
                    end += 1
                if end >= len(line) or line[end] != ')': index += 2; continue
                title = re.search(r'\s+["\'][^\r\n]*["\']\s*$', line[start:end])
                if title: end = start+title.start()
                while end > start and line[end-1].isspace(): end -= 1
            href = line[start:end]
            parsed = _destination(href)
            if parsed:
                path, key, suffix = parsed
                result.append(dict(start=offset+start, end=offset+end, href=href,
                                   path=path, key=key, suffix=suffix))
            index = max(end, index+2)
        offset += len(line)
    return result


def collect(candidate):
    occurrences = []
    for thread in candidate['transfer']['threads']:
        for turn in thread['turns']:
            for message in turn['messages']:
                if message['role'] != 'assistant': continue
                for link in extract(message['text']):
                    occurrences.append({**link, 'thread_id':thread['id'], 'turn_id':turn['id'],
                        'message_id':message['id'], 'project_id':thread['project_id'],
                        'text_sha256':hashlib.sha256(message['text'].encode('utf-8')).hexdigest()})
                    _require(len(occurrences) <= MAX_OCCURRENCES, 'Too many result references')
    return occurrences


def make_manifest(candidate, bindings):
    _require(len(bindings) <= MAX_FILES, 'Too many selected results')
    projects = {p['id'] for p in candidate['transfer']['projects']}
    if any(t['project_id'] is None for t in candidate['transfer']['threads']):projects.add('__projectless__')
    files, targets = [], {}
    for relative, binding in sorted(bindings.items()):
        parts = relative.split('/')
        _require(len(parts) >= 4 and parts[0] == 'Projects' and parts[1] in projects and
                 parts[2] == 'Results' and not any(p in ('', '.', '..') for p in parts))
        source = binding['path']
        key = source_key(source)
        _require(key is not None, 'Selected result needs an absolute local source path')
        aliases = binding.get('aliases', [])
        _require(isinstance(aliases,list) and aliases == sorted(set(aliases)) and len(aliases)<=1000)
        for alias_key in [key,*[source_key(href) for href in aliases]]:
            _require(alias_key is not None, 'Invalid explicit result source alias')
            identity = (parts[1], alias_key)
            _require(identity not in targets or targets[identity] == relative, 'Result source has ambiguous destinations')
            targets[identity] = relative
        files.append({'relative':relative, 'source_path':source, **({'aliases':aliases} if aliases else {})})
    occurrences = []
    for occurrence in collect(candidate):
        relative = targets.get((occurrence['project_id'] or '__projectless__', occurrence['key']))
        if relative:
            occurrences.append({**occurrence, 'relative':relative})
    return {'format':FORMAT, 'files':files, 'occurrences':occurrences}


def validate(manifest, candidate, result_paths):
    _require(isinstance(manifest, dict) and set(manifest) == {'format','files','occurrences'})
    _require(manifest['format'] == FORMAT and isinstance(manifest['files'], list) and
             isinstance(manifest['occurrences'], list) and len(manifest['files']) <= MAX_FILES)
    bindings = {}
    for file in manifest['files']:
        _require(isinstance(file, dict) and set(file) in ({'relative','source_path'}, {'relative','source_path','aliases'}))
        _require(isinstance(file['relative'], str) and isinstance(file['source_path'], str))
        _require(file['relative'] not in bindings)
        bindings[file['relative']] = {'path':file['source_path'], **({'aliases':file['aliases']} if 'aliases' in file else {})}
    _require(sorted(bindings) == sorted(result_paths), 'Result link inventory differs from payload')
    _require(manifest == make_manifest(candidate, bindings), 'Result reference provenance differs')


def derive(candidate, manifest, roots):
    validate(manifest, candidate, [f['relative'] for f in manifest['files']])
    derived, ledger = copy.deepcopy(candidate), []
    grouped = {}
    for occurrence in manifest['occurrences']:
        key = tuple(occurrence[k] for k in ('thread_id','turn_id','message_id'))
        parts = occurrence['relative'].split('/')
        target = Path(roots[parts[1]]).joinpath(*parts[2:]).as_posix()
        _require(not any(c in target for c in '<>\r\n'), 'Unrepresentable result link destination')
        # Codex URI-decodes clicked file references. A canonical file URI keeps
        # literal percent sequences distinct from spaces, including on reimport.
        pure = PureWindowsPath(target) if re.match(r'^[A-Za-z]:/', target) else PurePosixPath(target)
        replacement = '<' + pure.as_uri() + occurrence['suffix'] + '>'
        grouped.setdefault(key, []).append((occurrence, replacement))
        ledger.append({**occurrence, 'replacement':replacement, 'target_path':target})
    for thread in derived['transfer']['threads']:
        for turn in thread['turns']:
            for message in turn['messages']:
                key = (thread['id'], turn['id'], message['id'])
                text = message['text']
                for occurrence, replacement in sorted(grouped.get(key, []), key=lambda p:p[0]['start'], reverse=True):
                    text = text[:occurrence['start']] + replacement + text[occurrence['end']:]
                message['text'] = text
    return derived, ledger

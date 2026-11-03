"""Feed existing builder only bounded, validated source-routing metadata.

The builder's sources() reads files before returning its inventory. For a real
profile it instead sees a private projection containing paths only: no raw
configuration, SKILL bodies, credentials or marketplace payloads are written.
Returned paths are mapped back to the explicit original roots, never executed.
"""
import json
from pathlib import Path
import re
import tempfile
import tomllib
import hashlib
import os
import stat

import environment_adapter as adapter
import rehome_review_sources as managed
import snapshot

MAX_METADATA_FILES = 10000
MAX_ENTRIES = 100000
MAX_DEPTH = 40


def _read(path, limit):
    path = managed._local(path)
    if path.stat().st_nlink != 1:
        raise adapter.EnvironmentError('Hard-linked routing metadata prohibited')
    return adapter._read_stable(path, limit)


def _routing(path):
    path=managed._local(path,missing=True)
    if not path.exists(): return ('routing','missing')
    info=path.stat()
    return ('routing','directory' if path.is_dir() else 'file',info.st_dev,info.st_ino)


def verify_metadata_bindings(bindings):
    for path,binding in bindings.items():
        if binding is not None and binding[0] == 'routing':
            actual=_routing(path)
        else:
            actual=snapshot._hash_file(path) if path.exists() else None
        if actual != binding:
            raise adapter.EnvironmentError('Environment source changed during collection')


def guarded_sources(engine, home, codex_home):
    config = codex_home / 'config.toml'
    raw = _read(config, engine.MAX_FILE) if config.exists() else None
    cfg = tomllib.loads(raw.decode('utf-8-sig')) if raw is not None else {}
    if len(cfg.get('projects',{}))>MAX_METADATA_FILES:
        raise adapter.EnvironmentError('Source routing metadata limit exceeded')
    watched = {config: (hashlib.sha256(raw).hexdigest(), len(raw)) if raw is not None else None}
    total_metadata = len(raw or b'')
    with tempfile.TemporaryDirectory(prefix='rehome-source-routing-') as temporary:
        proxy_home = Path(temporary) / 'home'
        proxy_codex = proxy_home / '.codex'
        proxy_codex.mkdir(parents=True)

        def projected(path):
            path = managed._local(path, missing=True)
            if path.is_relative_to(codex_home):
                return proxy_codex / path.relative_to(codex_home)
            if path.is_relative_to(home):
                return proxy_home / path.relative_to(home)
            raise adapter.EnvironmentError('Source routing path is outside explicit home')

        def original(path):
            path = Path(path)
            if path.is_relative_to(proxy_codex):
                return codex_home / path.relative_to(proxy_codex)
            if path.is_relative_to(proxy_home):
                return home / path.relative_to(proxy_home)
            raise adapter.EnvironmentError('Unexpected projected builder source')

        project_lines = []
        for path in cfg.get('projects', {}):
            project = managed._local(path, missing=True)
            # Builder has always excluded external-home project references.
            # Retain them as absent projected roots, with no read through links.
            if not project.is_relative_to(home):
                continue
            for observed in (project,project/'.agents/skills',project/'.codex/skills'):
                watched[observed]=_routing(observed)
            proxy = projected(project)
            if project.is_dir():
                proxy.mkdir(parents=True, exist_ok=True)
            project_lines.append('[projects.' + json.dumps(str(proxy)) + ']')
        (proxy_codex / 'config.toml').write_text('\n'.join(project_lines), encoding='utf-8')
        skills = codex_home / 'skills'
        if skills.exists():
            managed._local(skills)
            for path in _skill_metadata(skills):
                data = _read(path, engine.MAX_FILE)
                total_metadata += len(data)
                if total_metadata > engine.MAX_TOTAL:
                    raise adapter.EnvironmentError('Routing metadata size limit exceeded')
                watched[path] = (hashlib.sha256(data).hexdigest(), len(data))
                routes = []
                for found in re.findall(r'`([^`]*engineering-workspace)`', data.decode('utf-8-sig')):
                    candidate = Path(found)
                    if candidate.is_absolute() and candidate.is_relative_to(home):
                        routes.append('`' + str(projected(candidate)) + '`')
                output = projected(path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text('\n'.join(routes), encoding='utf-8')
        market = home / '.agents/plugins/marketplace.json'
        if market.exists():
            data = _read(market, engine.MAX_FILE)
            watched[market] = (hashlib.sha256(data).hexdigest(), len(data))
            parsed = json.loads(data)
            total_metadata+=len(data)
            if total_metadata>engine.MAX_TOTAL or len(parsed.get('plugins',[]))>MAX_METADATA_FILES:
                raise adapter.EnvironmentError('Source routing metadata limit exceeded')
            plugins = []
            for item in parsed.get('plugins', []):
                source = item.get('source', {})
                route = source if isinstance(source, str) else source.get('path') if source.get('source') == 'local' else None
                if route:
                    candidate = managed._local(home / route, missing=True)
                    if candidate.is_relative_to(home):
                        plugins.append({'source': str(projected(candidate))})
            output = projected(market)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'plugins': plugins}), encoding='utf-8')
        else:
            watched[market] = None
        specs, _ = engine.sources(proxy_home, proxy_codex)
        for spec in specs:
            spec['source'] = original(spec['source'])
        # Bind the discovery pass to the bytes checked before proxy creation.
        verify_metadata_bindings(watched)
    return specs, cfg, watched


def _skill_metadata(skills):
    count=0
    with os.scandir(skills) as entries:
        for entry in entries:
            count+=1
            if count>MAX_METADATA_FILES:
                raise adapter.EnvironmentError('Source routing metadata limit exceeded')
            if entry.is_dir(follow_symlinks=False):
                path=Path(entry.path)/'SKILL.md'
                if path.exists(): yield path


def discover_resources(specs, home, engine):
    """Bound all enumeration and raw sizes before creating an output folder."""
    discovered, directories, seen, total = {}, {}, 0, 0
    payloads = []
    from snapshot import _check_collisions
    for spec in specs:
        root = managed._local(spec['source'], missing=True)
        candidates, excluded = [], []
        discovered[spec['id']] = (candidates, excluded)
        if spec['kind'] in {'context', 'data', 'custom-plugin'}:
            excluded.append({'component': spec['id'], 'path': '', 'reason': 'broad_context_or_external_source_not_packaged'})
            continue
        if not root.is_relative_to(home):
            continue
        if not root.exists():
            directories[root] = None
            continue
        items = [(root.parent, [], [root.name])] if root.is_file() else _walk_bounded(root)
        for current, dirs, files in items:
            current = managed._local(current)
            if root.is_dir() and len(current.relative_to(root).parts) > MAX_DEPTH:
                raise adapter.EnvironmentError('Environment discovery depth limit exceeded')
            if root.is_dir():
                directories[current] = tuple(sorted(dirs+files))
            seen += len(dirs) + len(files) + 1
            if seen > MAX_ENTRIES:
                raise adapter.EnvironmentError('Environment discovery entry limit exceeded')
            skip = {'.git', '__pycache__'} if spec['kind'] == 'plugin-cache' else set(engine.SKIP_DIRS)
            if spec['kind'] == 'tool':
                skip |= {'npm', 'tools', 'state'}
            if spec['kind'] == 'runtime':
                skip.add('ocr')
            keep = []
            for name in dirs:
                reason = None
                try:
                    managed._local(current/name)
                except ValueError:
                    reason = 'link_or_reparse'
                if name in skip and reason is None:
                    reason = 'dependency_excluded' if name in adapter._DEPS else 'temporary_or_local_state'
                if reason:
                    excluded.append({'component': spec['id'], 'path': (current/name).relative_to(root).as_posix(), 'reason': reason})
                else:
                    keep.append(name)
            dirs[:] = keep
            for name in sorted(files):
                path = current/name
                relative = path.relative_to(root).as_posix() if root.is_dir() else root.name
                try:
                    engine.safe_name(relative)
                    info = managed._local(path).stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > engine.MAX_FILE:
                        raise adapter.EnvironmentError('Unsafe resource')
                except (ValueError, OSError):
                    excluded.append({'component': spec['id'], 'path': relative, 'reason': 'unsafe_link_missing_or_large_file'})
                    continue
                total += info.st_size
                if total > engine.MAX_TOTAL:
                    raise adapter.EnvironmentError('Environment raw resource size limit exceeded')
                payloads.append('payload/' + spec['id'] + '/' + relative)
                candidates.append((path, relative))
    _check_collisions(payloads)
    return discovered, directories


def verify_discovery(directories):
    count=0
    for path, names in directories.items():
        managed._local(path, missing=names is None)
        actual=None
        if path.is_dir():
            values=[]
            with os.scandir(path) as entries:
                for entry in entries:
                    count+=1
                    if count>MAX_ENTRIES:
                        raise adapter.EnvironmentError('Environment discovery entry limit exceeded')
                    values.append(entry.name)
            actual=tuple(sorted(values))
        if actual != names or (names is None and path.exists()):
            raise adapter.EnvironmentError('Environment source inventory changed during collection')


def _walk_bounded(root):
    pending=[root]
    count=0
    while pending:
        current=pending.pop()
        managed._local(current)
        if len(current.relative_to(root).parts)>MAX_DEPTH:
            raise adapter.EnvironmentError('Environment discovery depth limit exceeded')
        dirs,files=[],[]
        with os.scandir(current) as entries:
            for entry in entries:
                count+=1
                if count>MAX_ENTRIES:
                    raise adapter.EnvironmentError('Environment discovery entry limit exceeded')
                (dirs if entry.is_dir(follow_symlinks=False) else files).append(entry.name)
        yield current,dirs,files
        pending.extend(current/name for name in reversed(dirs))

"""Local Codex backup engine. Never sends files or credentials to a network service."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import shutil
import stat
import sys
import tempfile
import tomllib
import zipfile
from datetime import datetime, timezone, timedelta
from password_prompt import PasswordCancelled, ask_password_window

ROOT = Path(__file__).resolve().parent
MAX_TOTAL = 4 * 1024**3
MAX_FILE = 256 * 1024**2
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'npm-cache',
             'cache', '.sandbox', '.sandbox-secrets'}
SECRET_NAMES = {'auth.json', 'credentials.json', 'secrets.json', '.npmrc', '.pypirc',
                'cookies.json', 'cookies.txt', 'token.json', 'tokens.json'}
SECRET_SUFFIX = {'.pem', '.key', '.p12', '.pfx'}
KEY_PATTERN = re.compile(rb'(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,})')
ASSIGNED_SECRET = re.compile(rb'''(?im)["']?(?:password|passwd|api_key|apikey|access_token|refresh_token|client_secret|authorization|cookie)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']''')


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sensitive(path, data):
    name = path.name.lower()
    if name in SECRET_NAMES or name.startswith('.env') and not name.endswith(('.example', '.sample', '.template')) or path.suffix.lower() in SECRET_SUFFIX:
        return True
    # Text heuristics are not meaningful for executables and compiled libraries.
    if b'\x00' in data:
        return False
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    if KEY_PATTERN.search(data):
        return True
    for match in ASSIGNED_SECRET.finditer(data):
        value = match.group(1).lower()
        if not any(marker in value for marker in (b'example', b'placeholder', b'your_', b'your-', b'${', b'<', b'os.environ', b'process.env', b'redacted', b'getenv')):
            return True
    return False


def linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def safe_name(name):
    if not isinstance(name, str) or '\\' in name or '\x00' in name:
        raise ValueError('Недопустимое имя в архиве')
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(p in {'.', '..'} or ':' in p or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Недопустимый путь в архиве')
    if any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in parts):
        raise ValueError('Зарезервированное имя Windows')
    if str(PurePosixPath(name)) != name:
        raise ValueError('Неоднозначный путь в архиве')
    return name


def project_inventory(codex_home, cfg):
    """Names and paths only; never traverse project folders or export chat state."""
    projects = {}

    def add(raw, name=None, source='config.toml'):
        if not isinstance(raw, str) or not raw.strip():
            return
        path = PureWindowsPath(raw) if '\\' in raw or re.match(r'^[A-Za-z]:', raw) else PurePosixPath(raw)
        key = str(path).casefold() if isinstance(path, PureWindowsPath) else str(path)
        if key not in projects:
            projects[key] = dict(name=name if isinstance(name, str) and name else path.name,
                                 folder_name=path.name, path=raw, source=source)

    state_file = codex_home / '.codex-global-state.json'
    if state_file.exists():
        state = json.loads(state_file.read_text('utf-8-sig'))
        local = state.get('local-projects', {})
        if not isinstance(local, dict):
            raise ValueError('Неизвестный формат списка проектов Codex; бекап не заменён.')
        for item in local.values():
            if isinstance(item, dict) and isinstance(item.get('rootPaths'), list):
                for raw in item['rootPaths']:
                    add(raw, item.get('name'), 'local-projects')
    for raw in cfg.get('projects', {}):
        add(raw)
    return sorted(projects.values(), key=lambda item: (item['name'].casefold(), item['path']))


def sources(home, codex_home):
    """Bounded inventory of Codex-specific locations; no entire disk traversal."""
    docs = home / 'Documents' / 'Codex'
    config = codex_home / 'config.toml'
    cfg = tomllib.loads(config.read_text('utf-8-sig')) if config.exists() else {}
    specs = [dict(id='user-skills', source=codex_home / 'skills', kind='skills', target='codex/skills'),
             dict(id='agents-skills', source=home / '.agents' / 'skills', kind='skills', target='home/.agents/skills'),
             dict(id='plugin-packages', source=codex_home / 'plugins' / 'cache', kind='plugin-cache', target=None),
             dict(id='skill-health-data', source=docs / 'skill-health-data', kind='data', target='home/Documents/Codex/skill-health-data'),
             dict(id='agent-reach', source=docs / 'agent-reach', kind='tool', target='home/Documents/Codex/agent-reach'),
             dict(id='engineering-runtime', source=docs / 'engineering-runtime', kind='runtime', target='home/Documents/Codex/engineering-runtime')]
    skills = codex_home / 'skills'
    external = set()
    if skills.exists():
        for path in skills.glob('*/SKILL.md'):
            for found in re.findall(r'`([^`]*engineering-workspace)`', path.read_text('utf-8-sig')):
                candidate = Path(found)
                if candidate.is_absolute() and candidate.is_relative_to(home):
                    external.add(candidate)
    for index, path in enumerate(sorted(external)):
        specs.append(dict(id=f'engineering-context-{index}', source=path, kind='context', target='home/' + path.relative_to(home).as_posix()))
    # Project skill roots referenced in the user's own Codex config.
    for index, raw in enumerate(cfg.get('projects', {})):
        project = Path(raw)
        if project.exists():
            for sub in ('.agents/skills', '.codex/skills'):
                specs.append(dict(id=f'project-{index}-{sub.split("/")[0][1:]}-skills', source=project / sub,
                                  kind='project-reference', target=None))
    personal_market = home / '.agents' / 'plugins' / 'marketplace.json'
    if personal_market.exists():
        market = json.loads(personal_market.read_text('utf-8-sig'))
        for index, item in enumerate(market.get('plugins', [])):
            source = item.get('source', {})
            raw = source if isinstance(source, str) else source.get('path') if source.get('source') == 'local' else None
            if raw:
                path = (home / raw).resolve()
                if path.is_relative_to(home):
                    specs.append(dict(id=f'custom-plugin-{index}', source=path, kind='custom-plugin', target=None))
    return specs, cfg


def scrub_config(raw):
    """Do not export live MCP env values or authentication headers in the main ZIP."""
    result = []
    section = ''
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped.lower()
        is_value = '=' in line and not stripped.startswith('#')
        if is_value and ('.env]' in section or 'headers' in section or re.search(r'(?i)token|password|secret|api_key|authorization|cookie', line.split('=', 1)[0]) or sensitive(Path('config-line'), line.encode())):
            key = line.split('=', 1)[0].strip()
            result.append(f'{key} = "REQUIRES_SECRET_FILE"')
        else:
            result.append(line)
    return '\n'.join(result) + '\n'


def secret_files(home, codex_home):
    candidates = [codex_home / 'auth.json', codex_home / 'config.toml']
    for folder in (home / 'Documents' / 'Codex' / 'agent-reach' / 'state',
                   home / 'Documents' / 'Codex' / 'agent-reach' / 'source'):
        if folder.exists():
            # Source traversal is filtered again by content/name, avoiding node dependencies.
            for current, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not linked(Path(current) / d)]
                for name in files:
                    path = Path(current) / name
                    if folder.name == 'state' or path.name.lower() in SECRET_NAMES or path.name.startswith('.env'):
                        candidates.append(path)
    for folder in (codex_home, home / '.agents'):
        if folder.exists():
            candidates.extend(folder.glob('*.env'))
            candidates.extend(folder.glob('.env*'))
    return candidates


def encrypt_vault(payload, password, backup_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if len(password) < 12:
        raise ValueError('Пароль файла секретов: минимум 12 символов')
    salt, nonce = os.urandom(16), os.urandom(12)
    header = dict(format='codex-secrets', version=1, backup_id=backup_id,
                  kdf='scrypt-32768-8-1', salt=base64.b64encode(salt).decode(), nonce=base64.b64encode(nonce).decode())
    key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
    ciphertext = AESGCM(key).encrypt(nonce, json_bytes(payload), json_bytes(header))
    return json_bytes(dict(header=header, ciphertext=base64.b64encode(ciphertext).decode()))


def decrypt_vault(data, password, backup_id):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidTag
    try:
        envelope = json.loads(data)
        h = envelope['header']
        if h['format'] != 'codex-secrets' or h['version'] != 1 or h['kdf'] != 'scrypt-32768-8-1' or h['backup_id'] != backup_id:
            raise ValueError('Файл секретов относится к другому бекапу или формату')
        salt, nonce = base64.b64decode(h['salt'], validate=True), base64.b64decode(h['nonce'], validate=True)
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError('Повреждён заголовок файла секретов')
        key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
        plain = AESGCM(key).decrypt(nonce, base64.b64decode(envelope['ciphertext'], validate=True), json_bytes(h))
        payload = json.loads(plain)
        if not isinstance(payload, dict) or not isinstance(payload.get('files'), list):
            raise ValueError('Повреждён состав файла секретов')
        return payload
    except InvalidTag:
        raise ValueError('Неверный пароль или повреждён файл секретов') from None


@contextmanager
def backup_lock(output):
    # Keep the lock file: deleting it can allow two processes to lock different files.
    with (output / '.codex-backup.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('В этой папке уже создаётся бекап. Дождитесь завершения.') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def create_backup(output, home=None, codex_home=None, password=None, progress=lambda s: None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if password is not None and len(password) < 12:
        raise ValueError('Пароль файла секретов: минимум 12 символов')
    with backup_lock(output):
        return _create_backup(output, home, codex_home, password, progress)


def prune_backups(output, keep):
    """Delete only this tool's dated backup files, directly in the output folder."""
    pattern = re.compile(r'Codex-(?:backup-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-[0-9a-f]{6}\.zip|secrets-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-[0-9a-f]{6}\.cbs)')
    removed, warnings = [], []
    try:
        candidates = list(output.iterdir())
    except OSError as error:
        return removed, [f'Новый бекап сохранён, старые не удалены: {error}']
    for path in candidates:
        if path in keep or not pattern.fullmatch(path.name):
            continue
        try:
            if linked(path) or not path.is_file():
                continue
            path.unlink()
            removed.append(path.name)
        except OSError as error:
            warnings.append(f'Новый бекап сохранён, не удалось удалить {path.name}: {error}')
    return removed, warnings


def _create_backup(output, home=None, codex_home=None, password=None, progress=lambda s: None):
    home = Path(home or Path.home()).resolve()
    codex_home = Path(codex_home or os.environ.get('CODEX_HOME', home / '.codex')).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if password is not None and len(password) < 12:
        raise ValueError('Пароль файла секретов: минимум 12 символов')
    backup_id = datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d_%H-%M-%S') + '-' + secrets.token_hex(3)
    filename = output / f'Codex-backup-{backup_id}.zip'
    temporary = output / (filename.name + '.partial')
    vault_path = output / f'Codex-secrets-{backup_id}.cbs'
    instruction_path = output / 'RESTORE-WITH-CODEX.md'
    instruction_temporary = output / f'RESTORE-WITH-CODEX-{backup_id}.partial'
    specs, cfg = sources(home, codex_home)
    projects = project_inventory(codex_home, cfg)
    manifest = dict(format='codex-backup', version=1, backup_id=backup_id, created_at=datetime.now(timezone(timedelta(hours=5))).isoformat(),
                    original_home=str(home), original_codex_home=str(codex_home), components=[], files={}, excluded=[], plugins=[], projects=projects,
                    secrets=dict(required=bool(password), filename=vault_path.name if password else None),
                    limitations=['Системные хранилища паролей и облачные токены не экспортируются.',
                                 'Окружения venv, node_modules, micromamba OCR и кэш установщиков пересоздаются.',
                                 'Пакеты плагинов сохранены; установка и доступ к сервисам проверяются отдельно.',
                                 'Обследованы известные каталоги, навыки указаных в config проектов и личный marketplace.'])
    secret_payload = dict(files=[], original_home=str(home), original_codex_home=str(codex_home))
    secret_seen = set()

    def preserve_secret(path, data, archive_name=None, target=None):
        key = str(path)
        if password and key not in secret_seen:
            secret_seen.add(key)
            if target is None and archive_name:
                target = 'home/Documents/Codex/recovered-references/' + archive_name
            elif target is None:
                if path.is_relative_to(codex_home):
                    target = 'codex/' + path.relative_to(codex_home).as_posix()
                elif path.is_relative_to(home):
                    target = 'home/' + path.relative_to(home).as_posix()
            secret_payload['files'].append(dict(source=key, archive_name=archive_name, target=target, data=base64.b64encode(data).decode(), sha256=sha(data)))

    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add(name, data):
                safe_name(name)
                if name in manifest['files']:
                    raise ValueError('Повторное имя файла при сборке')
                archive.writestr(name, data)
                manifest['files'][name] = dict(sha256=sha(data), size=len(data))

            for spec in specs:
                path = spec['source']
                component = {k: str(v) if isinstance(v, Path) else v for k, v in spec.items()}
                component['present'] = path.exists()
                component['count'] = 0
                manifest['components'].append(component)
                if not path.exists() or linked(path):
                    continue
                progress('Сохраняю: ' + spec['id'])
                for current, dirs, files in os.walk(path, followlinks=False):
                    # Installed plugin packages may need their bundled node_modules/cache.
                    excluded_dirs = {'.git', '__pycache__'} if spec['kind'] == 'plugin-cache' else SKIP_DIRS
                    removed = [d for d in dirs if d in excluded_dirs or linked(Path(current) / d)]
                    dirs[:] = [d for d in dirs if d not in removed]
                    if spec['kind'] == 'tool':
                        removed += [d for d in dirs if d in {'npm', 'tools', 'state'}]
                        dirs[:] = [d for d in dirs if d not in {'npm', 'tools', 'state'}]
                    if spec['kind'] == 'runtime':
                        removed += [d for d in dirs if d in {'ocr'}]
                        dirs[:] = [d for d in dirs if d not in {'ocr'}]
                    for directory in removed:
                        manifest['excluded'].append(dict(path=str(Path(current) / directory), reason='окружение, временные данные или ссылка; state сохраняется в файле секретов'))
                    for name in sorted(files):
                        source = Path(current) / name
                        rel = source.relative_to(path).as_posix()
                        archive_name = f'payload/{spec["id"]}/{rel}'
                        if linked(source) or source.stat().st_size > MAX_FILE:
                            manifest['excluded'].append(dict(path=str(source), reason='ссылка или слишком большой файл'))
                            continue
                        before = source.stat()
                        data = source.read_bytes()
                        after = source.stat()
                        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                            raise ValueError('Файл изменился во время сборки: ' + str(source))
                        if sensitive(source, data):
                            secret_target = spec['target'] + '/' + rel if spec['target'] else None
                            preserve_secret(source, data, archive_name, secret_target)
                            manifest['excluded'].append(dict(path=str(source), reason='перенесён в файл секретов' if password else 'требуется отдельный файл секретов'))
                            continue
                        add(archive_name, data)
                        component['count'] += 1
            settings = codex_home / 'config.toml'
            if settings.exists():
                raw = settings.read_bytes()
                preserve_secret(settings, raw)
                add('payload/settings/config.redacted.toml', scrub_config(raw.decode('utf-8-sig')).encode())
            for reference in [codex_home / 'AGENTS.md', home / '.agents' / 'plugins' / 'marketplace.json']:
                if reference.exists() and not linked(reference):
                    data = reference.read_bytes()
                    if sensitive(reference, data):
                        preserve_secret(reference, data)
                    else:
                        add('payload/settings/' + reference.name, data)
            for path in secret_files(home, codex_home):
                if path.exists() and not linked(path) and path.stat().st_size <= MAX_FILE:
                    preserve_secret(path, path.read_bytes())
            cache = codex_home / 'plugins' / 'cache'
            if cache.exists():
                for market in sorted(cache.iterdir()):
                    if market.is_dir() and not linked(market):
                        for plugin in sorted(market.iterdir()):
                            if plugin.is_dir() and not linked(plugin):
                                state = cfg.get('plugins', {}).get(f'{plugin.name}@{market.name}', {}).get('enabled')
                                manifest['plugins'].append(dict(name=plugin.name, marketplace=market.name,
                                    versions=[p.name for p in plugin.iterdir() if p.is_dir()], enabled=state))
            # Capture portable dependency names, not an unportable virtual environment.
            site = home / 'Documents' / 'Codex' / 'agent-reach' / 'venv' / 'Lib' / 'site-packages'
            dependencies = []
            if site.exists():
                for metadata in site.glob('*.dist-info/METADATA'):
                    text = metadata.read_text('utf-8', errors='replace')
                    package, version = re.search(r'^Name: (.+)$', text, re.M), re.search(r'^Version: (.+)$', text, re.M)
                    if package and version:
                        dependencies.append(package[1] + '==' + version[1])
            add('dependencies/agent-reach-requirements.txt', ('\n'.join(sorted(dependencies)) + '\n').encode())
            add('PROJECTS.json', json_bytes(dict(format='codex-project-list', version=1, projects=projects,
                                                scope='Названия и пути локальных проектов; без содержимого и чатов.')))
            for name in ('backup.py', 'password_prompt.py', 'start.ps1', 'README.md', 'RESTORE.md', 'RESTORE-WITH-CODEX.md', 'requirements.txt'):
                app_file = ROOT / name
                if app_file.exists():
                    add('recovery/' + name, app_file.read_bytes())
            add('START-HERE.md', (ROOT / 'RESTORE.md').read_bytes())
            instruction = (ROOT / 'RESTORE-WITH-CODEX.md').read_bytes()
            add('RESTORE-WITH-CODEX.md', instruction)
            manifest['secrets']['count'] = len(secret_payload['files']) if password else None
            if password:
                vault = encrypt_vault(secret_payload, password, backup_id)
                vault_path.write_bytes(vault)
                manifest['secrets']['sha256'] = sha(vault)
            archive.writestr('manifest.json', json_bytes(manifest))
        checked = verify_backup(temporary)
        if password:
            vault_for(checked, vault_path, password)
        instruction_temporary.write_bytes(instruction)
        instruction_temporary.replace(instruction_path)
        temporary.replace(filename)
    except BaseException:
        temporary.unlink(missing_ok=True)
        vault_path.unlink(missing_ok=True)
        instruction_temporary.unlink(missing_ok=True)
        raise
    # Commit is complete. Cleanup failure must never remove the new valid pair.
    removed, warnings = prune_backups(output, {filename, vault_path})
    return dict(archive=str(filename), vault=str(vault_path) if password else None,
                files=len(manifest['files']), plugins=len(manifest['plugins']),
                excluded=len(manifest['excluded']), size=filename.stat().st_size, backup_id=backup_id,
                removed_old_files=removed, warnings=warnings, projects=len(projects), instruction=str(instruction_path))


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 50000 or sum(f.file_size for f in infos) > MAX_TOTAL:
            raise ValueError('Архив превышает допустимый размер')
        seen = set()
        for info in infos:
            name = safe_name(info.filename)
            normalized = name.casefold()
            if normalized in seen or stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE:
                raise ValueError('Дубли, ссылки или большой файл в архиве')
            seen.add(normalized)
        if 'manifest.json' not in archive.namelist() or archive.getinfo('manifest.json').file_size > 16 * 1024**2:
            raise ValueError('Нет допустимого manifest.json')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('format') != 'codex-backup' or manifest.get('version') != 1:
            raise ValueError('Неизвестный формат бекапа')
        expected = manifest.get('files')
        if not isinstance(expected, dict) or set(archive.namelist()) != set(expected) | {'manifest.json'}:
            raise ValueError('Состав архива отличается от реестра')
        for name, entry in expected.items():
            if archive.getinfo(name).file_size != entry['size']:
                raise ValueError('Размер файла не совпадает с реестром')
            digest = hashlib.sha256()
            with archive.open(name) as file:
                for block in iter(lambda: file.read(1024**2), b''):
                    digest.update(block)
            if digest.hexdigest() != entry['sha256']:
                raise ValueError('Контрольная сумма не совпала: ' + name)
        return manifest


def vault_for(manifest, vault_path, password):
    if manifest['secrets']['required'] and not vault_path:
        raise ValueError('Для этого бекапа выберите отдельный файл секретов и введите пароль')
    if not vault_path:
        return None
    data = Path(vault_path).read_bytes()
    expected = manifest['secrets'].get('sha256')
    if expected and sha(data) != expected:
        raise ValueError('Файл секретов повреждён или выбран другой файл')
    return decrypt_vault(data, password or '', manifest['backup_id'])


def prepare_restore(archive_path, destination, vault_path=None, password=None):
    manifest = verify_backup(archive_path)
    # Unlock before creating any directory: a missing/wrong secret file leaves no partial restore.
    vault = vault_for(manifest, vault_path, password)
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Папка подготовки уже существует; выберите новую')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for name in manifest['files']:
            path = destination / safe_name(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, path.open('wb') as target:
                shutil.copyfileobj(source, target)
    # No decrypted secrets are written in preparation mode.
    (destination / 'manifest.json').write_bytes(json_bytes(manifest))
    plan = dict(backup_id=manifest['backup_id'], prepared=str(destination), secret_files=len(vault['files']) if vault else 0,
                components=[dict(id=c['id'], files=c['count'], target=c['target']) for c in manifest['components'] if c['present']],
                plugins=manifest['plugins'], projects=manifest.get('projects', []), limitations=manifest['limitations'])
    (destination / 'RESTORE-PLAN.json').write_bytes(json_bytes(plan))
    return plan


def apply_restore(archive_path, target_home, target_codex=None, vault_path=None, password=None):
    """Explicit CLI-only apply, never automatically called by the web panel."""
    manifest = verify_backup(archive_path)
    vault = vault_for(manifest, vault_path, password)
    home = Path(target_home).resolve()
    codex = Path(target_codex or home / '.codex').resolve()
    rollback = home / ('Codex-rollback-' + secrets.token_hex(6))
    operations = []
    component_map = {c['id']: c for c in manifest['components']}

    def remap(data, target):
        if target.suffix.lower() not in {'.md', '.py', '.ps1', '.json', '.toml', '.yaml', '.yml', '.txt', '.js', '.cjs', '.mjs', '.sh', '.cmd', '.bat', '.html', '.css'}:
            return data
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return data
        mappings = [(manifest['original_codex_home'], str(codex)), (manifest['original_home'], str(home))]
        for old, new in mappings:
            for source, destination in [(old.replace('\\', '\\\\'), new.replace('\\', '\\\\')), (old, new), (old.replace('\\', '/'), new.replace('\\', '/'))]:
                text = re.sub(re.escape(source) + r'(?=[/\\\\]|["\'`\s]|$)', lambda m: destination, text, flags=re.I)
        return text.encode('utf-8')

    def target_for(raw):
        if not raw:
            return None
        prefix, rest = raw.split('/', 1)
        safe_name(rest)
        base = codex if prefix == 'codex' else home if prefix == 'home' else None
        if base is None:
            raise ValueError('Неизвестный корень восстановления')
        path = base / rest
        if not path.resolve().is_relative_to(base):
            raise ValueError('Целевой путь выходит за корень')
        return path

    with zipfile.ZipFile(archive_path) as archive:
        for name in manifest['files']:
            parts = name.split('/')
            if len(parts) < 3 or parts[0] != 'payload' or parts[1] not in component_map:
                continue
            component = component_map[parts[1]]
            rel = '/'.join(parts[2:])
            # System skills and plugin caches are reference snapshots, not live installs.
            if component['target'] and not (component['kind'] == 'skills' and rel.startswith('.system/')):
                target = target_for(component['target'] + '/' + rel)
                operations.append((target, remap(archive.read(name), target)))
        # Settings remain a candidate for manual merge, preserving trust/permission decisions.
        if 'payload/settings/config.redacted.toml' in manifest['files']:
            candidate = codex / 'config.recovered.toml'
            operations.append((candidate, remap(archive.read('payload/settings/config.redacted.toml'), candidate)))
    if vault:
        for item in vault['files']:
            target = target_for(item.get('target'))
            if target:
                data = base64.b64decode(item['data'], validate=True)
                if sha(data) != item['sha256']:
                    raise ValueError('Повреждён секретный файл')
                if target == codex / 'config.toml':
                    target = codex / 'config.recovered.toml'
                    data = remap(data, target)
                operations.append((target, data))
    # Dedupe config candidate, with the encrypted original taking precedence.
    operations = list(dict(operations).items())
    changed = []
    journal = []
    rollback.mkdir(parents=True)
    try:
        for index, (target, data) in enumerate(operations):
            if target.exists() and target.read_bytes() == data:
                continue
            old = rollback / str(index)
            if target.exists():
                if linked(target):
                    raise ValueError('Нельзя заменять ссылку')
                shutil.copy2(target, old)
            target.parent.mkdir(parents=True, exist_ok=True)
            journal.append(dict(target=str(target), old=str(old) if old.exists() else None))
            (rollback / 'journal.json').write_bytes(json_bytes(journal))
            target.write_bytes(data)
            changed.append(str(target))
    except Exception:
        rollback_restore(rollback)
        raise
    return dict(changed=len(changed), rollback=str(rollback), plugins='Переустановить из каталога по manifest.json; кэш не устанавливается автоматически',
                settings='config.recovered.toml — кандидат для объединения, не активные настройки')


def rollback_restore(folder):
    folder = Path(folder).resolve()
    journal_path = folder / 'journal.json'
    if not journal_path.exists():
        return
    for item in reversed(json.loads(journal_path.read_bytes())):
        target = Path(item['target'])
        if item['old']:
            old = Path(item['old'])
            if not old.resolve().is_relative_to(folder):
                raise ValueError('Недопустимый путь отката')
            shutil.copy2(old, target)
        else:
            target.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Бекап Codex: ZIP и отдельный зашифрованный файл секретов')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('--output', default=str(Path.home() / 'Documents/ReHome/Backups'))
    create.add_argument('--with-secrets', action='store_true')
    create.add_argument('--password-window', action='store_true')
    check = sub.add_parser('verify'); check.add_argument('archive')
    restore = sub.add_parser('restore'); restore.add_argument('archive')
    restore.add_argument('--vault'); restore.add_argument('--destination')
    restore.add_argument('--apply', action='store_true'); restore.add_argument('--target-home')
    restore.add_argument('--target-codex')
    restore.add_argument('--password-window', action='store_true')
    rollback = sub.add_parser('rollback'); rollback.add_argument('folder')
    args = parser.parse_args()
    if args.action == 'restore':
        if args.apply and not args.target_home:
            parser.error('--apply требует явный --target-home')
        if not args.apply and not args.destination:
            parser.error('Укажите --destination для подготовки')
        manifest = verify_backup(args.archive)
        if manifest['secrets']['required'] and not args.vault:
            args.vault = input('Путь к отдельному файлу секретов (.cbs): ').strip().strip('"')
            if not args.vault:
                parser.error('Для этого ZIP требуется отдельный файл секретов')
    password = None
    if args.action == 'create' and args.with_secrets or args.action == 'restore' and args.vault:
        if args.password_window:
            password = ask_password_window(confirm=args.action == 'create')
        else:
            import getpass
            import warnings
            with warnings.catch_warnings():
                # Refuse getpass's visible-input fallback when no secure terminal exists.
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Пароль отдельного файла секретов: ')
                if args.action == 'create' and password != getpass.getpass('Повторите пароль: '):
                    parser.error('Пароли не совпадают')
    if args.action == 'create':
        result = create_backup(args.output, password=password)
    elif args.action == 'verify':
        m = verify_backup(args.archive); result = dict(valid=True, files=len(m['files']), backup_id=m['backup_id'])
    elif args.action == 'restore':
        if args.apply:
            if not args.target_home:
                parser.error('--apply требует явный --target-home')
            result = apply_restore(args.archive, args.target_home, args.target_codex, args.vault, password)
        else:
            if not args.destination:
                parser.error('Укажите --destination для подготовки')
            result = prepare_restore(args.archive, args.destination, args.vault, password)
    else:
        rollback_restore(args.folder); result = dict(rolled_back=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except PasswordCancelled as error:
        print(str(error), file=sys.stderr)
        sys.exit(130)
    except KeyboardInterrupt:
        print('\nОперация прервана сигналом отмены (например, Ctrl+C). Это не ошибка совпадения паролей.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

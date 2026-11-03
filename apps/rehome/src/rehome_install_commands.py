"""Opt-in installation of one session-command route; existing rules are backed up."""
import hashlib
from pathlib import Path
import re
import uuid

import snapshot

START='<!-- rehome-session-commands:v1:start -->'
END='<!-- rehome-session-commands:v1:end -->'
OLD_START='<!-- agent-workspace-session-commands:v1:start -->'
OLD_END='<!-- agent-workspace-session-commands:v1:end -->'


def install(codex_root,launcher,data_root):
    codex_root=snapshot._safe_path(Path(codex_root))
    launcher=snapshot._safe_path(Path(launcher))
    data_root=snapshot._safe_path(Path(data_root))
    if launcher.name!='Start-ReHome.ps1' or launcher.parent!=data_root:
        raise ValueError('Use the launcher of this installed ReHome distribution')
    instructions=Path(__file__).resolve().parent.parent/'Assets/SESSION-COMMANDS.md'
    if not instructions.exists():instructions=Path(__file__).resolve().parent.parent/'resources/SESSION-COMMANDS.md'
    block=instructions.read_text(encoding='utf-8').strip()
    if not block.startswith(START) or not block.endswith(END):raise ValueError('Session instructions are incomplete')
    path=codex_root/'AGENTS.md'
    snapshot._safe_path(path,allow_missing=True)
    original=path.read_bytes() if path.exists() else b''
    content=original.decode('utf-8-sig')
    for start,end in ((START,END),(OLD_START,OLD_END)):
        if content.count(start)!=content.count(end) or content.count(start)>1:
            raise ValueError('Ambiguous existing session command block')
        if start in content:
            content=re.sub(re.escape(start)+r'[\s\S]*?'+re.escape(end),'',content,count=1)
    updated=(content.rstrip()+'\n\n'+block+'\n').lstrip('\n').encode('utf-8')
    backup=None
    if updated!=original:
        folder=data_root/'AgentRulesBackups';folder.mkdir(exist_ok=True)
        backup=folder/('AGENTS-'+uuid.uuid4().hex+'.md')
        with backup.open('xb') as stream:stream.write(original)
        temporary=codex_root/('.rehome-agents-'+uuid.uuid4().hex)
        with temporary.open('xb') as stream:stream.write(updated)
        temporary.replace(path)
    location={'format':'rehome-session-location/1','launcher':str(launcher),
              'launcher_sha256':snapshot._hash_file(launcher)[0]}
    target=codex_root/'rehome-session-location.json'
    snapshot._safe_path(target,allow_missing=True)
    temporary=codex_root/('.rehome-location-'+uuid.uuid4().hex)
    snapshot._write_json(temporary,location);temporary.replace(target)
    return {'status':'installed','instructions':str(path),'previous_rules':str(backup) if backup else None,
            'workspace_database_changed':False,'restart_or_reload_instructions_required':True}

"""Typed real-environment metadata and reversible resource path projection."""
import hashlib
import json
import os
from pathlib import Path
import re

import environment_adapter as adapter

MAX_REBINDINGS = 10000
HEX = re.compile(r'^[0-9a-f]{64}$')
IDENT = re.compile(r'^binding-[0-9a-f]{20}$')


def require(condition, message):
    if not condition:
        raise adapter.EnvironmentError(message)


def project_resource(data, home, codex_home, component, relative, namespace):
    if b'\x00' in data:
        return data, [], []
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return data, [], []
    require('${REHOME_' + namespace + '_' not in text, 'Resource collides with generated path namespace')
    replacements, refs = [], []

    def replace(original, scope, style):
        require(len(replacements) < MAX_REBINDINGS, 'Resource path binding limit exceeded')
        token = '${REHOME_' + namespace + '_' + str(len(replacements)) + '}'
        entry = {'token': token, 'original': original, 'scope': scope, 'style': style}
        replacements.append(entry)
        if scope is None:
            field = f'resource.{component}.{relative}.{len(refs)}'
            ident = 'binding-' + hashlib.sha256(field.encode()).hexdigest()[:20]
            refs.append({'id': ident, 'field': field, 'kind': 'resource_path', 'requires_local_setup': True})
        return token

    for label, root in (('CODEX_HOME', codex_home), ('HOME', home)):
        variants = ((str(root).replace('\\', '\\\\'), 'JSON'), (root.as_posix(), 'POSIX'), (str(root), 'NATIVE'))
        for old, style in variants:
            pattern = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(old) + r'''(?=$|[\\/\s"'`,;\)\]\}])''',
                                 re.I if os.name == 'nt' else 0)
            text = pattern.sub(lambda match: replace(match.group(), label, style), text)
    # Each match is replaced separately; literals unrelated to this namespace
    # remain unchanged, including the legacy HOME/CODEX_HOME placeholder forms.
    pattern = adapter._ABSOLUTE_TEXT
    def unknown(match):
        value = match.group().rstrip(',;)]}')
        return replace(value, None, 'LOCAL') + match.group()[len(value):]
    text = pattern.sub(unknown, text)
    return text.encode('utf-8'), replacements, refs


def resource_bytes(data, entry, namespace, *, home=None, codex_home=None, return_original=False):
    """Validate projection/reversibility, then optionally bind known local roots."""
    bindings = entry['rebindings']
    require(isinstance(bindings, list) and len(bindings) <= MAX_REBINDINGS, 'Invalid resource bindings')
    require(isinstance(entry['original_size'], int) and entry['original_size'] >= 0 and
            isinstance(entry['original_sha256'], str) and HEX.fullmatch(entry['original_sha256']), 'Invalid original resource binding')
    if not bindings:
        original = data
        require(entry['tokens'] == [], 'Unexpected resource tokens')
        result = data
    else:
        text = data.decode('utf-8')
        original_text, result = text, text
        tokens = []
        for index, binding in enumerate(bindings):
            token = '${REHOME_' + namespace + '_' + str(index) + '}'
            require(isinstance(binding, dict) and set(binding) == {'token', 'original', 'scope', 'style'} and
                    binding['token'] == token and text.count(token) == 1, 'Invalid resource token projection')
            value, scope, style = binding['original'], binding['scope'], binding['style']
            require(isinstance(value, str) and 0 < len(value) <= 8192 and not any(ord(c) < 32 for c in value) and
                    adapter._absolute(value), 'Invalid original resource path')
            require((scope is None and style == 'LOCAL') or (scope in ('HOME', 'CODEX_HOME') and
                    style in ('JSON', 'POSIX', 'NATIVE')), 'Invalid resource binding type')
            original_text = original_text.replace(token, value)
            replacement = token
            if home is not None and scope is not None:
                root = codex_home if scope == 'CODEX_HOME' else home
                replacement = str(root).replace('\\', '\\\\') if style == 'JSON' else root.as_posix() if style == 'POSIX' else str(root)
            result = result.replace(token, replacement)
            tokens.append(token)
        require(entry['tokens'] == tokens, 'Resource tokens differ from projection')
        original = original_text.encode('utf-8')
        result = result.encode('utf-8') if home is not None else data
    require(len(original) == entry['original_size'] and hashlib.sha256(original).hexdigest() == entry['original_sha256'],
            'Resource projection is not reversible')
    return original if return_original else result


def validate_config(config):
    require(isinstance(config, dict) and set(config) == {'selected', 'local_bindings', 'secret_refs', 'omitted_settings',
            'active_config_written', 'authorization_exported'} and config['active_config_written'] is False and
            config['authorization_exported'] is False, 'Invalid typed settings contract')
    refs = {}
    for field, flag in (('local_bindings', 'requires_local_setup'), ('secret_refs', 'requires_reentry')):
        require(isinstance(config[field], list), 'Invalid settings references')
        for item in config[field]:
            keys = {'id', 'field', flag} | ({'kind'} if field == 'local_bindings' else set())
            require(isinstance(item, dict) and set(item) == keys and item[flag] is True and
                    isinstance(item['field'], str) and 0 < len(item['field']) <= 8192 and
                    item['id'] == 'binding-' + hashlib.sha256(item['field'].encode()).hexdigest()[:20] and
                    item['id'] not in refs, 'Invalid or duplicate typed binding')
            if field == 'local_bindings':
                require(item['kind'] in {'executable', 'working_directory', 'argument_path', 'agent_definition', 'resource_path'},
                        'Invalid local binding kind')
            refs[item['id']] = field

    def reference(value):
        require(isinstance(value, dict) and len(value) == 1, 'Opaque setting must be a reference')
        key = next(iter(value))
        expected = {'local_binding': 'local_bindings', 'secret_ref': 'secret_refs'}
        require(key in expected and refs.get(value[key]) == expected[key], 'Unresolved typed setting reference')

    selected = config['selected']
    allowed = {'model', 'model_provider', 'personality', 'model_reasoning_effort', 'model_verbosity', 'mcp_servers', 'plugins', 'agents',
               'desktop', 'check_for_update_on_startup', 'hide_agent_reasoning'}
    require(isinstance(selected, dict) and set(selected) <= allowed and
            {'mcp_servers', 'plugins', 'agents'} <= set(selected), 'Invalid selected settings')
    for key in ('model', 'model_provider', 'personality'):
        require(key not in selected or isinstance(selected[key], str) and adapter._SIMPLE.fullmatch(selected[key]), 'Invalid scalar setting')
    for key, choices in (('model_reasoning_effort', {'none','minimal','low','medium','high','xhigh','max','ultra'}),
                         ('model_verbosity', {'low','medium','high'})):
        require(key not in selected or selected[key] in choices, 'Invalid enum setting')
    for key in ('check_for_update_on_startup', 'hide_agent_reasoning'):
        require(key not in selected or type(selected[key]) is bool, 'Invalid boolean setting')
    if 'desktop' in selected:
        require(isinstance(selected['desktop'], dict) and set(selected['desktop']) == {'integratedTerminalShell'} and
                selected['desktop']['integratedTerminalShell'] in {'commandPrompt','powershell','powershell7','windowsPowerShell'},
                'Invalid desktop setting')
    for section in ('mcp_servers', 'plugins', 'agents'):
        require(isinstance(selected[section], dict), 'Invalid settings section')
        for name, item in selected[section].items():
            require(isinstance(name, str) and adapter._name(name, None) == name and isinstance(item, dict), 'Invalid settings entry')
            if section == 'plugins':
                require(set(item) <= {'install_required', 'enabled'} and item.get('install_required') is True and
                        ('enabled' not in item or type(item['enabled']) is bool), 'Invalid plugin setting')
            elif section == 'agents':
                require(set(item) <= {'config_file'}, 'Invalid agent setting')
                if 'config_file' in item: reference(item['config_file'])
            else:
                require(set(item) <= {'requires_local_setup','enabled','required','startup_timeout_sec','tool_timeout_sec',
                                     'command','cwd','url','args','env','http_headers','auth'} and
                        item.get('requires_local_setup') is True, 'Invalid MCP setting')
                for key, value in item.items():
                    if key in {'enabled','required'}: require(type(value) is bool, 'Invalid MCP flag')
                    elif key in {'startup_timeout_sec','tool_timeout_sec'}: require(type(value) in (float,int) and 0 <= value <= 3600, 'Invalid MCP timeout')
                    elif key == 'command' and isinstance(value,str): require(adapter._SIMPLE.fullmatch(value), 'Invalid MCP command')
                    elif key in {'command','cwd','url'}: reference(value)
                    elif key == 'args':
                        require(isinstance(value,list), 'Invalid MCP arguments')
                        for ref in value: reference(ref)
                    elif key in {'env','http_headers'}:
                        require(isinstance(value,dict), 'Invalid MCP fields')
                        for label, ref in value.items():
                            require(adapter._name(label,None) == label, 'Invalid MCP field')
                            reference(ref)
                    elif key == 'auth': require(value in {'oauth','chatgpt'}, 'Invalid MCP auth reference')
    require(isinstance(config['omitted_settings'], list) and all(isinstance(k,str) and adapter._name(k,None) == k
            for k in config['omitted_settings']), 'Invalid omitted settings inventory')


def validate_manifest(inventory, root):
    keys = {'format','synthetic_only','created_at','engine_sha256','components','files','skills','plugins','agents','excluded',
            'dependencies','config','limits','file_count','total_bytes','skill_counts','token_namespace'}
    require(set(inventory) == keys and isinstance(inventory['token_namespace'],str) and
            re.fullmatch('[0-9a-f]{32}',inventory['token_namespace']), 'Invalid real environment manifest fields')
    require(isinstance(inventory['engine_sha256'],str) and HEX.fullmatch(inventory['engine_sha256']) and
            isinstance(inventory['created_at'],str) and isinstance(inventory['limits'],list) and
            all(isinstance(v,str) for v in inventory['limits']), 'Invalid environment provenance')
    validate_config(inventory['config'])
    components = {c['id']:c for c in inventory['components']}
    reasons = {'outside_explicit_home','broad_context_or_external_source_not_packaged','link_or_reparse',
               'dependency_excluded','temporary_or_local_state','unsafe_link_missing_or_large_file','sensitive_content_omitted'}
    require(isinstance(inventory['excluded'],list) and isinstance(inventory['dependencies'],list), 'Invalid exclusion inventory')
    for item in inventory['excluded']:
        require(isinstance(item,dict) and set(item) == {'component','path','reason'} and item['component'] in components and
                isinstance(item['path'],str) and len(item['path']) <= 8192 and item['reason'] in reasons,
                'Invalid environment exclusion')
    for item in inventory['dependencies']:
        require(isinstance(item,dict) and set(item) in ({'component','path','reason'}, {'component','path','reason','bindings'}) and
                isinstance(item['path'],str) and len(item['path']) <= 8192 and item['reason'] in
                {'dependency_excluded','unresolved_resource_paths','agent_definition_missing'}, 'Invalid environment dependency')
        require(item['component'] in components or item['component'] == 'agent-definition', 'Unknown dependency component')
    files = inventory['files']
    expected_resource_refs=[]
    expected_dependencies=[e.copy() for e in inventory['excluded'] if e['reason']=='dependency_excluded']
    for entry in files:
        require(set(entry) == {'path','component','relative_path','target','size','sha256','tokens','unresolved_bindings',
                              'original_sha256','original_size','rebindings'}, 'Invalid real resource fields')
        data = adapter._read_stable(root/entry['path'], entry['size'])
        resource_bytes(data, entry, inventory['token_namespace'])
        require(isinstance(entry['unresolved_bindings'],list) and len(entry['unresolved_bindings']) ==
                sum(b['scope'] is None for b in entry['rebindings']), 'Resource unresolved binding count differs')
        resource_refs=[]
        for index,binding in enumerate(b for b in entry['rebindings'] if b['scope'] is None):
            field=f"resource.{entry['component']}.{entry['relative_path']}.{index}"
            resource_refs.append({'id':'binding-'+hashlib.sha256(field.encode()).hexdigest()[:20],
                                  'field':field,'kind':'resource_path','requires_local_setup':True})
        ids=[r['id'] for r in resource_refs]
        require(entry['unresolved_bindings']==ids,'Unresolved resource bindings differ')
        expected_resource_refs.extend(resource_refs)
        if ids:
            expected_dependencies.append({'component':entry['component'],'path':entry['relative_path'],
                                          'reason':'unresolved_resource_paths','bindings':ids})
    require([r for r in inventory['config']['local_bindings'] if r['kind']=='resource_path']==expected_resource_refs,
            'Resource config bindings differ')
    for dependency in inventory['dependencies']:
        if dependency['reason']=='agent_definition_missing':
            require(dependency['component']=='agent-definition' and dependency['path'] in inventory['config']['selected']['agents'] and
                    'config_file' in inventory['config']['selected']['agents'][dependency['path']], 'Invalid missing agent dependency')
            expected_dependencies.append(dependency)
    canonical=lambda items: sorted(json.dumps(v,sort_keys=True) for v in items)
    require(canonical(inventory['dependencies'])==canonical(expected_dependencies),'Resource dependency bindings differ')
    for component in inventory['components']:
        require(set(component) == {'id','kind','present','file_count','total_bytes','restore_mode'} and type(component['present']) is bool,
                'Invalid environment component metadata')
        entries = [e for e in files if e['component'] == component['id']]
        require(component['file_count'] == len(entries) and component['total_bytes'] == sum(e['size'] for e in entries),
                'Environment component counts differ')
        require(component['kind'] not in {'context','data','custom-plugin'} or not entries,
                'Broad context or external source files prohibited')
    require(isinstance(inventory['skills'],list) and isinstance(inventory['plugins'],list) and isinstance(inventory['agents'],list),
            'Invalid environment summaries')
    skills = inventory['skills']
    seen = set()
    for skill in skills:
        require(set(skill) == {'name','component','path','file_count','resource_dirs','restore_mode','discoverable_after_copy','status'},
                'Invalid skill summary fields')
        component, folder = skill['component'], skill['path']
        require((component,folder) not in seen and isinstance(folder,str) and isinstance(skill['name'],str), 'Invalid skill summary identity')
        seen.add((component,folder))
        prefix = folder+'/' if folder else ''
        entries = [e for e in files if e['component'] == component and e['relative_path'].startswith(prefix)]
        excluded = [e for e in inventory['excluded'] if e['component'] == component and e['path'].startswith(prefix)]
        descriptor = next((e for e in entries if e['relative_path'] == prefix+'SKILL.md'),None)
        data = b'' if descriptor is None else resource_bytes(adapter._read_stable(root/descriptor['path'],descriptor['size']),
                    descriptor,inventory['token_namespace'],return_original=True)
        try:
            match = re.search(r'''(?m)^name:\s*['"]?([^'"\r\n]+)''',data.decode('utf-8-sig'))
        except UnicodeDecodeError:
            match = None
        name = adapter._name(match.group(1).strip() if match else None,folder.split('/')[-1] or 'unnamed-skill')
        require(skill['name'] == name,'Skill name differs from descriptor')
        kind,target,_ = adapter._component_policy(component)
        discoverable = kind == 'skills' and target in {'codex/skills','home/.agents/skills'}
        status = 'reference-only' if not discoverable else 'requires_local_setup' if excluded or any(e['unresolved_bindings'] for e in entries) else 'copied'
        require(skill['file_count'] == len(entries) and skill['status'] == status and
                skill['restore_mode'] == ('copy' if discoverable else 'reference-only') and
                skill['discoverable_after_copy'] is discoverable and skill['resource_dirs'] ==
                sorted({e['relative_path'][len(prefix):].split('/')[0] for e in entries if '/' in e['relative_path'][len(prefix):]}),
                'Skill summary differs from resources')
    counts = {'discovered':len(skills),'copy_ready':sum(s['status']=='copied' for s in skills),
              'requires_local_setup':sum(s['status']=='requires_local_setup' for s in skills),
              'reference_only':sum(s['status']=='reference-only' for s in skills)}
    require(inventory['skill_counts'] == counts, 'Skill counts differ')
    descriptors = {(e['component'],e['relative_path'].rsplit('/',1)[0] if '/' in e['relative_path'] else '')
                   for e in files if e['relative_path'].split('/')[-1] == 'SKILL.md'}
    descriptors.update((e['component'],e['path'].rsplit('/',1)[0] if '/' in e['path'] else '')
                       for e in inventory['excluded'] if e['path'].split('/')[-1] == 'SKILL.md')
    require(seen == descriptors, 'Skill descriptor inventory differs')
    expected_agents = [{'component':e['component'],'path':e['relative_path'],
                       'kind':adapter._component_policy(e['component'])[0]} for e in files
                       if adapter._component_policy(e['component'])[0] in {'agents','agent-reference','instructions','rules'}]
    require(inventory['agents'] == expected_agents,'Agent definition summary differs')
    expected_plugins = []
    for entry in files:
        kind = adapter._component_policy(entry['component'])[0]
        relative = entry['relative_path']
        if kind in {'plugin-cache','custom-plugin'} and (relative.endswith(('.codex-plugin/plugin.json','.claude-plugin/plugin.json')) or relative == 'plugin.json'):
            raw = resource_bytes(adapter._read_stable(root/entry['path'],entry['size']),entry,inventory['token_namespace'],return_original=True)
            try: meta = json.loads(raw)
            except (ValueError,UnicodeError): meta = {}
            name = adapter._name(meta.get('name',meta.get('id')),'unidentified-plugin')
            version = meta.get('version')
            expected_plugins.append({'id':name,'version':version if isinstance(version,str) and adapter._SIMPLE.fullmatch(version) else None,
                'component':entry['component'],'manifest_path':entry['path'],'restore_mode':'reference-only','install_required':True})
    for name in inventory['config']['selected']['plugins']:
        if not any(p['id'] == name for p in expected_plugins):
            expected_plugins.append({'id':name,'version':None,'component':None,'manifest_path':None,
                                     'restore_mode':'reinstall-required','install_required':True})
    require(canonical(inventory['plugins']) == canonical(expected_plugins),'Plugin summary differs')

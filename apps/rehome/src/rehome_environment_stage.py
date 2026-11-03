"""Inert real environment materials and typed activation plan inside a new job."""
import hashlib
from pathlib import Path
import zipfile

import environment_adapter as adapter
import environment_archive as archive
import rehome_environment_contract as contract
import snapshot

ENVIRONMENT = 'Environment'
MATERIALS = 'EnvironmentMaterials'
PLAN = 'EnvironmentActivationPlan.json'
MATERIAL_MARKER = 'rehome-environment-materials.json'


def _output(entry):
    target = entry['target']
    if target is None:
        return 'references/' + entry['component'] + '/' + entry['relative_path']
    scope, relative = target.split('/', 1)
    return ('home/codex-profile/' if scope == 'codex' else 'home/') + relative


def material_inventory(source, materials):
    inventory = adapter.verify_backup_environment(source)
    home, codex = materials/'home', materials/'home/codex-profile'
    expected = {}
    for entry in inventory['files']:
        raw = adapter._read_stable(source/entry['path'], entry['size'])
        data=contract.resource_bytes(raw, entry, inventory['token_namespace'], home=home, codex_home=codex)
        expected[_output(entry)] = (hashlib.sha256(data).hexdigest(),len(data))
    return inventory, expected


def verify_materials(source, materials):
    materials = snapshot._safe_path(Path(materials))
    inventory, expected = material_inventory(source, materials)
    expected_files = set(expected) | {MATERIAL_MARKER, 'home/codex-profile/config.candidate.json'}
    for relative, binding in expected.items():
        contract.require(snapshot._hash_file(materials/relative) == binding,
                         'Rebound environment resource differs from pinned package')
    contract.require(snapshot._read_json(materials/'home/codex-profile/config.candidate.json') == inventory['config'],
                     'Environment settings candidate differs')
    contract.require(snapshot._read_json(materials/MATERIAL_MARKER) == {'format':'rehome-environment-materials/1',
        'synthetic_only':False,'profile_applied':False,'environment_activated':False}, 'Environment materials marker differs')
    files, directories = set(), set()
    for path in materials.rglob('*'):
        snapshot._safe_path(path)
        contract.require(path.stat().st_nlink == 1 if path.is_file() else True, 'Linked materials prohibited')
        (directories if path.is_dir() else files).add(path.relative_to(materials).as_posix())
    wanted_dirs = {'home','home/codex-profile'}
    for relative in expected_files:
        wanted_dirs.update(p.as_posix() for p in Path(relative).parents if p != Path('.'))
    contract.require(files == expected_files and directories == wanted_dirs, 'Environment materials inventory differs')
    return inventory


def activation_plan(job, inventory):
    result = {'format':'rehome-environment-activation-plan/1','synthetic_only':False,
        'environment_archive_sha256': snapshot._hash_file(job/'Package/Environment.zip')[0],
        'environment_manifest_sha256':snapshot._hash_file(job/ENVIRONMENT/adapter.MANIFEST)[0],
        'profile_applied':False,'environment_activated':False,'resources':[],
        'settings_candidate':MATERIALS+'/home/codex-profile/config.candidate.json',
        'plugins':inventory['plugins'],'local_bindings':inventory['config']['local_bindings'],
        'secret_refs':inventory['config']['secret_refs'],'dependencies':inventory['dependencies'],
        'omitted_settings':inventory['config']['omitted_settings'],
        'exclusions':inventory['excluded'],'skill_counts':inventory['skill_counts'],
        'requires_local_setup':bool(inventory['excluded'] or inventory['dependencies'] or inventory['plugins'] or
                                   inventory['config']['local_bindings'] or inventory['config']['secret_refs'])}
    for entry in inventory['files']:
        result['resources'].append({'path':MATERIALS+'/'+_output(entry),'target':entry['target'],
            'status':'reference_only' if entry['target'] is None else 'requires_local_setup' if entry['unresolved_bindings'] else 'prepared',
            'activated':False})
    return result


def prepare(job):
    job = Path(job)
    archive.unpack_backup_environment(job/'Package/Environment.zip',job/ENVIRONMENT)
    adapter.restore_backup_environment(job/ENVIRONMENT,job/MATERIALS)
    inventory = verify_materials(job/ENVIRONMENT,job/MATERIALS)
    snapshot._write_json(job/PLAN,activation_plan(job,inventory))
    return verify(job)


def verify(job):
    job = snapshot._safe_path(Path(job))
    inventory = verify_materials(job/ENVIRONMENT,job/MATERIALS)
    # Bind unpacked bytes to the exact outer-pinned archive, including manifest.
    with zipfile.ZipFile(job/'Package/Environment.zip') as package:
        expected = set()
        for item in package.infolist():
            snapshot._validate_relative(item.filename)
            expected.add(item.filename)
            digest, size = hashlib.sha256(),0
            with package.open(item) as stream:
                while block := stream.read(1024*1024):
                    size += len(block)
                    contract.require(size <= archive.MAX_FILE,'Environment expansion limit exceeded')
                    digest.update(block)
            contract.require(snapshot._hash_file(job/ENVIRONMENT/item.filename) == (digest.hexdigest(),size),
                             'Unpacked environment differs from pinned archive')
    contract.require({p.relative_to(job/ENVIRONMENT).as_posix() for p in (job/ENVIRONMENT).rglob('*') if p.is_file()} == expected,
                     'Unpacked environment inventory differs from archive')
    contract.require(snapshot._read_json(job/PLAN) == activation_plan(job,inventory),'Environment activation plan differs')
    return {'environment_prepared':True,'environment_activated':False,'files':len(inventory['files']),
            'requires_local_setup':activation_plan(job,inventory)['requires_local_setup']}


def layout_files(job):
    """Return exact verified additions to native staging's fixed allowlist."""
    verify(job)
    return {PLAN} | {p.relative_to(job).as_posix() for folder in (ENVIRONMENT,MATERIALS)
                     for p in (job/folder).rglob('*') if p.is_file()}

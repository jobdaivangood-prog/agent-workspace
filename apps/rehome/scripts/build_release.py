"""Build ReHome from this checkout and checksum-pinned public upstream inputs.

No installed Codex, user profile, credentials or private development output is
read. Each destination is new. Windows x64 and a trusted Python 3.12 are needed.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import native_adapter as native
import rehome_app_package as app
import rehome_native_staging as staging
import rehome_python_runtime as runtime
import snapshot
from build_icon import write_icon

VERSION='0.1.40-beta.1'
PYTHON_ARCHIVE='cpython-3.12.16+20261026-x86_64-pc-windows-msvc-pgo-full.tar.zst'
PYTHON_SHA='37b4eb2818fee605fefa75ed156c4f2e1aaeaebaf662f9b30177c842ee16c48f'
ZSTD_ARCHIVE='zstd-v1.5.7-win64.zip'
ZSTD_SHA='acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9'
ZSTD_DLL_SHA='8f07e1112ed283e5cd2798833e9a3c32d8961381bc36da04af57a1b0ca9bd40b'
ZSTD_EXE_SHA='8076aae03feac7c66b319579e82172eed168deed2a3f25e5e2d3c60f55e84111'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def fetch(cache,name,url,pin):
    path=cache/name
    if not path.exists():
        with urllib.request.urlopen(url,timeout=120) as incoming,path.open('xb') as out:
            shutil.copyfileobj(incoming,out)
    if digest(path)!=pin:raise ValueError('Upstream archive checksum differs: '+name)
    return path


def materialize_python(archive,tool,destination):
    tarpath=destination.parent/'python-upstream.tar'
    with tarpath.open('xb') as stream:
        subprocess.run([str(tool),'-d','--stdout',str(archive)],stdout=stream,check=True)
    destination.mkdir()
    total=0
    with tarfile.open(tarpath) as tar:
        for entry in tar:
            if not (entry.name.startswith('python/install/') or entry.name.startswith('python/licenses/') or entry.name=='python/PYTHON.json'):continue
            relative=PurePosixPath(entry.name).relative_to('python')
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe upstream path')
            if entry.isdir():continue
            if not entry.isfile() or entry.size>64*1024*1024:raise ValueError('Unsafe upstream member')
            if entry.name.endswith(('.pdb','.pyc')):continue
            total+=entry.size
            if total>512*1024*1024:raise ValueError('Upstream extraction limit')
            output=destination.joinpath(*relative.parts);output.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(entry) as incoming,output.open('xb') as stream:shutil.copyfileobj(incoming,stream)
    metadata=json.loads((destination/'PYTHON.json').read_text(encoding='utf-8'))
    if metadata['python_version']!='3.12.16' or metadata['target_triple']!='x86_64-pc-windows-msvc':
        raise ValueError('Unexpected Python upstream build')


def create_schema(destination):
    spec=json.loads((ROOT/'resources/schema-spec.json').read_text(encoding='utf-8'))
    if spec['runtime_sha256']!=app.NATIVE_SHA:raise ValueError('Schema runtime differs')
    destination.mkdir()
    native.mark_synthetic_profile(destination)
    for name,description in spec['databases'].items():
        if name not in staging.DATABASES:raise ValueError('Unexpected schema database')
        with sqlite3.connect(destination/name) as db:
            for entry in description['objects']:
                if not native._schema_object_allowed(entry):raise ValueError('Unexpected schema object')
                db.execute(entry['sql'])
            rows=[[bytes.fromhex(v['bytes_hex']) if isinstance(v,dict) else v for v in row]
                  for row in description['migration_rows']]
            if rows:db.executemany('INSERT INTO _sqlx_migrations VALUES('+','.join('?' for _ in rows[0])+')',rows)
    pins={name:digest(destination/name) for name in staging.DATABASES}
    staging._template(destination,pins)
    return pins


def instructions_html(source):
    output=[]
    for line in source.splitlines():
        value=html.escape(line)
        value=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',value)
        value=re.sub(r'`([^`]+)`',r'<code>\1</code>',value)
        def link(match):
            url=match.group(2)
            if not url.startswith('https://'):
                url='https://github.com/jobdaivangood-prog/agent-workspace/blob/rehome-v0.1.40-beta.1/apps/rehome/docs/'+url
            return '<a href="'+html.escape(url,quote=True)+'">'+match.group(1)+'</a>'
        value=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,value)
        if line.startswith('# '):output.append('<h1>'+value[2:]+'</h1>')
        elif line.startswith('## '):output.append('<h2>'+value[3:]+'</h2>')
        elif line.startswith('### '):output.append('<h3>'+value[4:]+'</h3>')
        elif line.strip():output.append('<p>'+value+'</p>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>ReHome — инструкция</title>'
        '<style>body{margin:0;background:#f3f6fb;color:#182a46;font:16px/1.65 "Segoe UI",sans-serif}'
        'article{max-width:850px;margin:48px auto;padding:40px;background:white;border-radius:20px}'
        'h1{font-size:32px}h2{margin-top:36px;color:#245cdb}code{background:#edf2fa;padding:2px 5px;border-radius:4px}'
        'p{margin:10px 0}.brand{font-size:13px;color:#5d6c82}</style><article><div class="brand">ReHome '+VERSION+
        '</div>'+''.join(output)+'</article></html>')


def build(output,cache):
    if os.name!='nt':raise ValueError('Build on Windows x64')
    output=output.absolute();cache=cache.absolute()
    if output.exists():raise ValueError('Use a new build directory')
    output.mkdir(parents=True);cache.mkdir(parents=True,exist_ok=True)
    python=fetch(cache,PYTHON_ARCHIVE,'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'+PYTHON_ARCHIVE,PYTHON_SHA)
    zstd=fetch(cache,ZSTD_ARCHIVE,'https://github.com/facebook/zstd/releases/download/v1.5.7/'+ZSTD_ARCHIVE,ZSTD_SHA)
    tools=output/'build-tools';tools.mkdir()
    with zipfile.ZipFile(zstd) as archive:
        for member,name,pin in [('zstd-v1.5.7-win64/zstd.exe','zstd.exe',ZSTD_EXE_SHA),
                                ('zstd-v1.5.7-win64/dll/libzstd.dll','libzstd.dll',ZSTD_DLL_SHA)]:
            data=archive.read(member)
            if hashlib.sha256(data).hexdigest()!=pin:raise ValueError('Upstream zstd component differs')
            (tools/name).write_bytes(data)
    upstream=output/'upstream';materialize_python(python,tools/'zstd.exe',upstream)
    licenses=output/'runtime-notices';shutil.copytree(ROOT/'third_party',licenses)
    # All native notices come from the same authenticated build source, not
    # from a locally installed interpreter or remembered license text.
    for file in (upstream/'licenses').iterdir():shutil.copyfile(file,licenses/file.name)
    shutil.copyfile(upstream/'install/LICENSE.txt',licenses/'python-WINDOWS-NOTICES.txt')
    shutil.copyfile(upstream/'install/tcl/tk8.6/license.terms',licenses/'tk-license.terms')
    rt=runtime.build(upstream/'install',output/'runtime-build',licenses,python_version='3.12.16',tcl_tk='8.6.15',public_upstream=True)
    runtime.verify_tree(output/'runtime-build/Runtime',rt['manifest_sha256'])
    for entry in json.loads((output/'runtime-build/Runtime/runtime-manifest.json').read_text(encoding='utf-8'))['files']:
        if Path(entry['path']).suffix.lower() in ('.exe','.dll','.pyd'):
            if digest(upstream/'install'/entry['path'])!=entry['sha256']:raise ValueError('Native runtime differs from upstream')
    template=output/'schema';pins=create_schema(template)
    assets=output/'assets';assets.mkdir()
    write_icon(assets/'ReHome.ico')
    shutil.copyfile(ROOT/'resources/ReHome.svg',assets/'ReHome.svg')
    (assets/'Instructions.html').write_text(instructions_html((ROOT/'docs/USER-GUIDE.ru.md').read_text(encoding='utf-8')),encoding='utf-8')
    shutil.copyfile(ROOT/'COPYRIGHT',assets/'COPYRIGHT.txt')
    shutil.copyfile(ROOT/'THIRD-PARTY-NOTICES.md',assets/'THIRD-PARTY-NOTICES.txt')
    files={}
    modules=json.loads((ROOT/'resources/app-modules.json').read_text(encoding='utf-8'))
    for name in modules:files['App/'+name+'.py']=ROOT/'src'/(name+'.py')
    for name in ('backup.py','password_prompt.py','provenance.json'):
        files['App/vendor/codex_backup/'+name]=ROOT/'src/vendor/codex_backup'/name
    for file in (output/'runtime-build/Runtime').rglob('*'):
        if file.is_file():files['Runtime/'+file.relative_to(output/'runtime-build/Runtime').as_posix()]=file
    for file in template.iterdir():files['Schema/'+file.name]=file
    files['Dependencies/libzstd.dll']=tools/'libzstd.dll'
    files['Dependencies/LICENSE-zstd.txt']=ROOT/'third_party/LICENSE-zstd.txt'
    for file in assets.iterdir():files['Assets/'+file.name]=file
    bundle=output/'ReHome';bundle.mkdir()
    snapshot_info=snapshot.create_snapshot(bundle/'Program',version=1,device_id='rehome-public-release',results=files)
    pin=snapshot_info['manifest_digest']
    launcher=bundle/'Start-ReHome.ps1'
    launcher.write_text(app.POWERSHELL.replace('__PROGRAM_PIN__',pin),encoding='utf-8-sig')
    csharp=(ROOT/'native/ReHome.cs').read_text(encoding='utf-8').replace('__PROGRAM_PIN__',pin).replace('__LAUNCHER_PIN__',digest(launcher))
    generated=output/'ReHome.generated.cs';generated.write_text(csharp,encoding='utf-8-sig')
    compiler=Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler),'/nologo','/target:winexe','/platform:x64','/optimize+',
                    '/reference:System.Windows.Forms.dll','/win32icon:'+str(assets/'ReHome.ico'),
                    '/out:'+str(bundle/'ReHome.exe'),str(generated)],check=True)
    (bundle/'README.md').write_text('# ReHome '+VERSION+'\n\n'
        'Откройте ReHome.exe из этой локальной папки. Python устанавливать не нужно.\n\n'
        '[Инструкция](Program/Assets/Instructions.html) · '
        '[Права](Program/Assets/COPYRIGHT.txt) · '
        '[Сторонние компоненты](Program/Assets/THIRD-PARTY-NOTICES.txt)\n\n'
        'Windows 10/11 x64; поддерживается Codex 0.166.0-alpha.5.1. '
        'Папка приложения должна находиться вне Synology/OneDrive; копии могут храниться в Synology Drive.\n\n'
        'Исходники и документация: https://github.com/jobdaivangood-prog/agent-workspace/tree/rehome-v0.1.40-beta.1/apps/rehome\n',encoding='utf-8')
    archive=output/('ReHome-'+VERSION+'-windows-x64.zip')
    expected={}
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as zipped:
        for file in sorted(bundle.rglob('*')):
            if file.is_file():
                name='ReHome/'+file.relative_to(bundle).as_posix();expected[name]=digest(file)
                zipped.write(file,name)
    with zipfile.ZipFile(archive) as zipped:
        if len(zipped.namelist())!=len(expected) or set(zipped.namelist())!=set(expected):raise ValueError('Archive layout differs')
        for name,pinvalue in expected.items():
            if hashlib.sha256(zipped.read(name)).hexdigest()!=pinvalue:raise ValueError('Archive member differs')
    result={'format':'rehome-public-build/1','version':VERSION,'program_manifest_sha256':pin,
            'archive':archive.name,'archive_sha256':digest(archive),'archive_bytes':archive.stat().st_size,
            'runtime_manifest_sha256':rt['manifest_sha256'],'python':'3.12.16','tcl_tk':'8.6.15',
            'decoder_sha256':ZSTD_DLL_SHA,'codex_sha256':app.NATIVE_SHA,'schema_pins':pins,
            'native_upstream_bytes_verified':True,'user_settings_included':False,'codex_executable_included':False,
            'private_profiles_or_backups_read':False,'pc2_final_bundle_verified':False}
    (output/'build-info.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    (output/'SHA256SUMS.txt').write_text(result['archive_sha256']+'  '+archive.name+'\n',encoding='ascii')
    print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cache',type=Path,default=ROOT/'output/upstream-cache')
    args=parser.parse_args();build(args.output,args.cache)

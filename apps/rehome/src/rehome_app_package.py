"""Freeze a local two-action candidate using existing snapshot/runtime builders."""
import json
import hashlib
import os
from pathlib import Path
import socket
import stat
import uuid

import rehome_backup_module as backup
import rehome_native_staging as staging
import snapshot

NATIVE_SHA='3097c909fb6d5cd63510d44aceb1d42df9f6e85722a3e447e477def1ecf60e16'


def _package_resources(program, *, verify_files=True):
    """Read package-bound resource pins from the launcher-verified manifest.

    The launcher authenticates manifest.json against the external Program pin
    before calling configure(). This function cannot independently establish
    that external trust because its public API has no expected-pin argument.
    It does validate the manifest contract, READY binding, and any resource
    files that are present before returning paths or pins for settings.
    """
    program=Path(os.path.abspath(program))
    try:program.lstat()
    except FileNotFoundError:return None
    snapshot._safe_path(program)
    if not stat.S_ISDIR(program.lstat().st_mode):
        raise snapshot.SnapshotError('INVALID_PATH','Current Program path is not a directory')
    manifest_path=program/'manifest.json'
    try:manifest_path.lstat()
    except FileNotFoundError:return None
    manifest=snapshot._read_json(manifest_path)
    snapshot._validate_header(manifest)
    files=manifest.get('files')
    backup._require(isinstance(files,list) and len(files)<=10000,'Invalid current Program manifest')
    entries={}
    for entry in files:
        backup._require(isinstance(entry,dict) and set(entry)=={'path','size','sha256','kind'},
                        'Invalid current Program manifest entry')
        relative=snapshot._validate_relative(entry['path'])
        backup._require(type(entry['size']) is int and entry['size']>=0 and
                        isinstance(entry['sha256'],str) and snapshot._HEX.fullmatch(entry['sha256']) and
                        entry['kind'] in ('result','chat_bundle') and relative not in entries,
                        'Invalid current Program manifest entry')
        entries[relative]=entry
    snapshot._check_collisions(list(entries))
    if verify_files:
        ready=snapshot._read_json(program/'READY.json')
        backup._require(ready=={'schema':snapshot.SCHEMA,'snapshot_id':manifest['snapshot_id'],
                                'manifest_sha256':snapshot._digest(manifest)},
                        'Current Program readiness marker differs')

    template_names=tuple(staging.DATABASES)
    template_entries={name:entries.get('Schema/'+name) for name in template_names}
    decoder_entry=entries.get('Dependencies/libzstd.dll')

    def matches(relative,entry):
        if entry is None:return False
        path=program.joinpath(*relative.split('/'))
        snapshot._safe_path(path,allow_missing=True)
        try:info=path.lstat()
        except FileNotFoundError:return False
        if not stat.S_ISREG(info.st_mode):
            raise snapshot.SnapshotError('INVALID_FILE','Program resource is not a regular file')
        digest,size=snapshot._hash_file(path)
        if digest!=entry['sha256'] or size!=entry['size']:
            raise snapshot.SnapshotError('HASH_MISMATCH','Program resource differs from its verified manifest')
        return True

    template_available=(all(template_entries.values()) and
                        (not verify_files or all(matches('Schema/'+name,template_entries[name]) for name in template_names)))
    decoder_available=(decoder_entry is not None and
                       (not verify_files or matches('Dependencies/libzstd.dll',decoder_entry)))
    return {'schema_template':str(program/'Schema') if template_available else None,
            'template_pins':({name:template_entries[name]['sha256'] for name in template_names}
                             if template_available else None),
            'decoder':({'path':str(program/'Dependencies/libzstd.dll'),
                        'sha256':decoder_entry['sha256']} if decoder_available else None)}


def _validate_existing_settings(settings):
    backup._require(isinstance(settings,dict) and settings.get('format')=='rehome-two-action-settings/1',
                    'Invalid existing ReHome settings')
    # Restore-only pins can be absent or invalid while a previous transaction
    # still needs recovery. Its immutable receipt supplies its own trust pins.
    if 'finished_results' in settings:
        backup._require(isinstance(settings['finished_results'],list),'Invalid existing finished results')


def _replace_settings(path,settings):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        snapshot._write_json(temporary,settings)
        snapshot._safe_path(path,allow_missing=True)
        os.replace(temporary,path)
    finally:
        try:temporary.unlink()
        except FileNotFoundError:pass


def configure(program,data_root):
    """Per-PC local paths; no active Codex settings or original files written."""
    program=Path(program);data_root=Path(data_root)
    identity=hashlib.sha256(str(Path.home()).casefold().encode()).hexdigest()[:12]
    path=data_root/('settings-'+socket.gethostname()+'-'+identity+'.json')
    try:path.lstat();existing=True
    except FileNotFoundError:existing=False
    if existing:
        settings=snapshot._read_json(path)
        _validate_existing_settings(settings)
        try:resources=_package_resources(program)
        except snapshot.SnapshotError as error:
            # These package-only resources are consumed by backup/restore
            # actions. A damaged or incomplete arrival must not hide the
            # existing recovery UI; action code rechecks their pins strictly.
            if error.code in {'MISSING_FILE','INVALID_FILE','SIZE_MISMATCH','HASH_MISMATCH'}:
                resources=None
            else:raise
        updated=dict(settings)
        if (resources is not None and resources['schema_template'] is not None and
                resources['decoder'] is not None):
            updated['schema_template']=resources['schema_template']
            updated['template_pins']=resources['template_pins']
            updated['decoder']=dict(resources['decoder'])
        if updated==settings:return path
        _replace_settings(path,updated)
        return path
    # First launch also needs the profile-bound recovery UI when action-only
    # files are missing. Declare their pinned paths; actions verify the bytes.
    resources=_package_resources(program,verify_files=False)
    home=Path.home()
    base=home/'AppData/Local/OpenAI/Codex/bin'
    matches=[]
    if base.is_dir():
        for index,folder in enumerate(base.iterdir()):
            if index>=128:break
            exe=folder/'codex.exe'
            if exe.is_file() and snapshot._hash_file(exe)[0]==NATIVE_SHA:matches.append(exe)
    executable=sorted(matches)[-1] if matches else base/'reviewed-runtime-unavailable/codex.exe'
    backup._require(resources is not None and resources['schema_template'] is not None and
                    resources['decoder'] is not None,'Program lacks verified package resources')
    synology=[home/'Documents/SynologyDrive/Codex/ReHome',home/'Documents/ReHome']
    synced=next((p for p in synology if p.is_dir()),synology[0])
    settings={'format':'rehome-two-action-settings/1','source_home':str(home),'source_profile':str(home/'.codex'),
        # Keep nested environment materialization usable on Windows 10 without
        # enabling a global long-path policy. Existing user choices stay intact.
        'local_root':str(home/'ReHome'),
        'backup_root':str(synced/'Backups'),'projects_root':str(home/'Documents/ReHome/Projects'),
        'runtime_executable':str(executable),'runtime_sha256':NATIVE_SHA,'runtime_version':backup.SUPPORTED_RUNTIME,
        'schema_template':resources['schema_template'],
        'template_pins':resources['template_pins'],
        'decoder':resources['decoder'],'finished_results':[],'backup_root_selected':False}
    snapshot._write_json(path,settings)
    return path


def entry(arguments):
    """Verified launcher entry; also cleans up a task if configuration fails."""
    import re
    import subprocess
    args=list(arguments)
    if len(args)!=6:raise ValueError('Invalid launcher arguments')
    data_root,action,input_file,pin,output,task_name=args
    if task_name and not re.fullmatch(r'ReHome-Session-[a-f0-9]{32}',task_name):
        raise ValueError('Invalid session worker task')
    try:
        program=Path(__file__).resolve().parent.parent
        os.environ['TCL_LIBRARY']=str(program/'Runtime/tcl/tcl8.6')
        os.environ['TK_LIBRARY']=str(program/'Runtime/tcl/tk8.6')
        settings=configure(program,Path(data_root))
        if action=='ui':
            import rehome_backup_app
            rehome_backup_app.main(['--settings',str(settings)])
        else:
            import rehome_sessions
            cli=[action,'--settings',str(settings),'--output',output,
                 '--launcher',str(Path(data_root)/'Start-ReHome.ps1')]
            if input_file:cli+=['--input',input_file]
            if pin:cli+=['--pin',pin]
            rehome_sessions.main(cli)
    except Exception:
        if action!='ui' and output:
            target=snapshot._safe_path(Path(output),allow_missing=True)
            if not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True)
                snapshot._write_json(target,{'status':'failed','error_type':'launcher_or_configuration_failed',
                                            'profile_success_not_asserted':True})
        raise
    finally:
        if task_name:
            subprocess.run(['schtasks.exe','/Delete','/TN',task_name,'/F'],capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW,timeout=20,check=False)


POWERSHELL=r'''[CmdletBinding()]
param([switch]$Check,
 [ValidateSet('ui','inventory','prepare','finish','start','install')][string]$Action='ui',
 [string]$InputFile='', [string]$RequestPin='', [string]$OutputFile='')
$ErrorActionPreference='Stop'
$program=Join-Path $PSScriptRoot 'Program'
$pin='__PROGRAM_PIN__'
function Assert-Plain([string]$Path) {
 $current=[IO.Path]::GetFullPath($Path)
 while($current) {
  if(Test-Path -LiteralPath $current) {
   $item=Get-Item -LiteralPath $current -Force
   if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Linked program path'}
  }
  $parent=[IO.Directory]::GetParent($current);if($null-eq $parent){break};$current=$parent.FullName
 }
}
try {
 Assert-Plain $program
 $manifestPath=Join-Path $program 'manifest.json'
 if((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()-cne $pin){throw 'Program manifest pin mismatch'}
 $manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8|ConvertFrom-Json
 if($manifest.schema-cne 'agent-workspace-snapshot/1' -or $null-eq $manifest.files -or $manifest.files.Count-gt 10000){throw 'Program contract mismatch'}
 $expected=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
 foreach($name in @('manifest.json','snapshot.json','READY.json')){[void]$expected.Add($name)}
 foreach($entry in $manifest.files) {
  $name=$entry.path
  if($name-cnotmatch '^(App|Runtime|Schema|Dependencies|Assets)/[A-Za-z0-9_.+/-]+$' -or $name.Split('/')-contains '..' -or $name.Split('/')-contains '.' -or -not $expected.Add($name)){throw 'Invalid program component path'}
  $path=Join-Path $program ($name.Replace('/','\'))
  # Missing template/decoder resources never disable recovery. Their action
  # wrappers check external pins before backup/restore; code/runtime are vital.
  if($name.StartsWith('Schema/') -or $name.StartsWith('Dependencies/')){continue}
  Assert-Plain $path
  $file=Get-Item -LiteralPath $path -Force
  if($file.Length-ne $entry.size -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()-cne $entry.sha256){throw 'Program component changed'}
 }
 $pending=New-Object 'Collections.Generic.Stack[string]';$pending.Push($program);$count=0
 while($pending.Count-gt 0) {
  $directory=$pending.Pop();Assert-Plain $directory
  foreach($file in Get-ChildItem -LiteralPath $directory -Force) {
   $count++;if($count-gt 20000){throw 'Program entry limit'}
   Assert-Plain $file.FullName
   if($file.PSIsContainer){$pending.Push($file.FullName)}else{
    $relative=$file.FullName.Substring($program.Length+1).Replace('\','/')
    if(-not $expected.Contains($relative)){throw 'Unexpected program component'}
   }
  }
 }
 if($Check){Write-Output '{"status":"local_program_verified","profile_applied":false}';exit 0}
 $runtime=Join-Path $program 'Runtime'
 $app=Join-Path $program 'App'
 $taskName=if($Action-in @('finish','start')){'ReHome-Session-'+[guid]::NewGuid().ToString('N')}else{''}
 $code='import sys;sys.path.insert(0,sys.argv[1]);import rehome_app_package;rehome_app_package.entry(sys.argv[2:])'
 function Quote([string]$Value){return '"'+$Value.Replace('"','\"')+'"'}
 $info=New-Object Diagnostics.ProcessStartInfo
 $info.FileName=Join-Path $runtime 'pythonw.exe'
 if($Action-ne 'ui' -and [string]::IsNullOrWhiteSpace($OutputFile)){throw 'Session action requires OutputFile'}
 $info.Arguments='-I -B -c '+(Quote $code)+' '+(Quote $app)+' '+(Quote $PSScriptRoot)+' '+(Quote $Action)+' '+(Quote $InputFile)+' '+(Quote $RequestPin)+' '+(Quote $OutputFile)+' '+(Quote $taskName)
 $info.WorkingDirectory=$runtime;$info.UseShellExecute=$false;$info.CreateNoWindow=$true
 foreach($key in @('PYTHONPATH','PYTHONHOME','PYTHONUSERBASE','TCL_LIBRARY','TK_LIBRARY','TCLLIBPATH')){[void]$info.EnvironmentVariables.Remove($key)}
 $info.EnvironmentVariables['TCL_LIBRARY']=Join-Path $runtime 'tcl/tcl8.6'
 $info.EnvironmentVariables['TK_LIBRARY']=Join-Path $runtime 'tcl/tk8.6'
 if($taskName) {
  # An on-demand task has no schedule/password and runs as this interactive
  # user, without elevation. Its worker survives the Codex parent's job exit.
  $taskAction=New-ScheduledTaskAction -Execute $info.FileName -Argument $info.Arguments -WorkingDirectory $runtime
  $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
  $taskSettings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $taskName -Action $taskAction -Principal $principal -Settings $taskSettings | Out-Null
  try {
   # An elevated SSH launcher otherwise gives the limited worker read-only
   # access to its own task. Preserve existing ACEs and grant this same user
   # control of this one newly created, limited, unscheduled task for cleanup.
   $scheduler=New-Object -ComObject Schedule.Service;$scheduler.Connect()
   $registered=$scheduler.GetFolder('\').GetTask($taskName)
   $security=New-Object Security.AccessControl.RawSecurityDescriptor ($registered.GetSecurityDescriptor(4))
   $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
   $ace=New-Object Security.AccessControl.CommonAce ([Security.AccessControl.AceFlags]::None,[Security.AccessControl.AceQualifier]::AccessAllowed,0x1f01ff,$sid,$false,$null)
   $security.DiscretionaryAcl.InsertAce(0,$ace)
   $registered.SetSecurityDescriptor($security.GetSddlForm([Security.AccessControl.AccessControlSections]::Access),0)
   Start-ScheduledTask -TaskName $taskName
  } catch {Unregister-ScheduledTask -TaskName $taskName -Confirm:$false;throw}
  Write-Output ('ReHome worker requested: '+$taskName);exit 0
 }
 $process=[Diagnostics.Process]::Start($info)
 $process.WaitForExit()
 if($process.ExitCode-ne 0){throw 'ReHome window could not start'}
} catch {
 if($Check){Write-Output '{"status":"local_program_verification_failed","profile_applied":false}';exit 1}
 Add-Type -AssemblyName System.Windows.Forms
 [void][Windows.Forms.MessageBox]::Show('ReHome остановлен. Проверьте комплект и снова откройте окно. Для незавершённого восстановления доступен возврат прежних данных.','ReHome')
 exit 1
}
'''


# Run the packaged CLI from PowerShell. There is no nested Node test supervisor,
# recursive deletion, real Codex configuration or secret input. Observations remain.
$ErrorActionPreference = 'Stop'
$taskSource = Split-Path -Parent $PSScriptRoot
$taskVersion = (Get-Content -LiteralPath (Join-Path $taskSource 'package.json') -Raw | ConvertFrom-Json).version
$taskRelease = Join-Path $taskSource ("output/agent-workspace-$taskVersion-win-x64")
$taskNode = Join-Path $taskRelease 'runtime/node.exe'
$taskApp = Join-Path $taskRelease 'app.mjs'
$taskRun = Join-Path $taskSource ('work/release-observations/run-' + [guid]::NewGuid().ToString())
$taskLocal = Join-Path $taskRun 'local-a'
$taskShared = Join-Path $taskRun 'shared-a'
$taskEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = $taskEncoding
New-Item -ItemType Directory -Path $taskRun | Out-Null
function Invoke-Workspace {
    param([string[]]$Arguments, [string]$Local = $taskLocal)
    $taskText = & $taskNode $taskApp @Arguments --local $Local
    if ($LASTEXITCODE -ne 0) { throw ('Packaged CLI failed: ' + $Arguments[0]) }
    return (($taskText -join "`n") | ConvertFrom-Json)
}
$taskHelp = Invoke-Workspace -Arguments @('help')
if ($taskHelp.version -ne $taskVersion) { throw 'Unexpected package version' }
$null = Invoke-Workspace -Arguments @('init', '--root', $taskShared)
$taskProject = Invoke-Workspace -Arguments @('project', '--name', 'Packaged runtime observation')
$taskFinished = Join-Path $taskRun 'finished.txt'
[IO.File]::WriteAllText($taskFinished, 'Final observation from portable runtime', $taskEncoding)
$taskResult = Invoke-Workspace -Arguments @('result', '--file', $taskFinished, '--project', $taskProject.record_id)
$taskInput = Join-Path $taskRun 'task.json'
$taskData = @{
    project_id = $taskProject.record_id
    title = 'Portable handoff'
    goal = 'Continue from a verified compact handoff'
    task_status = 'in_progress'
    session_status = 'closed'
    current_stage = 'integration'
    plan = @(@{id='integration';title='Integration';done_criterion='Real shared folder checked';status='in_progress';verification='Synthetic replica only'})
    next_steps = @('Connect the real synchronized folder')
    result_records = @($taskResult.record_id)
}
[IO.File]::WriteAllText($taskInput, ($taskData | ConvertTo-Json -Depth 10), $taskEncoding)
$taskSaved = Invoke-Workspace -Arguments @('save-task', '--input', $taskInput)
$taskRepeated = Invoke-Workspace -Arguments @('save-task', '--input', $taskInput, '--record', $taskSaved.record_id, '--expected-revision', $taskSaved.revision_id)
if ($taskRepeated.status -ne 'unchanged' -or $taskRepeated.revision_id -ne $taskSaved.revision_id) { throw 'Unchanged CLI handoff created a revision' }
$taskStartup = Invoke-Workspace -Arguments @('startup')
if ($taskStartup.status -ne 'ready') { throw 'First replica is not ready' }
$taskRestore = Join-Path $taskRun 'restored'
$null = Invoke-Workspace -Arguments @('restore', '--record', $taskResult.record_id, '--destination', $taskRestore)
if ((Get-FileHash -LiteralPath $taskFinished).Hash -ne (Get-FileHash -LiteralPath (Join-Path $taskRestore 'finished.txt')).Hash) { throw 'Finished result was changed' }
$taskOtherRoot = Join-Path $taskRun 'other-provider'
Copy-Item -LiteralPath $taskShared -Destination $taskOtherRoot -Recurse
$taskOtherLocal = Join-Path $taskRun 'local-b'
$null = Invoke-Workspace -Arguments @('attach', '--root', $taskOtherRoot) -Local $taskOtherLocal
if ((Invoke-Workspace -Arguments @('startup') -Local $taskOtherLocal).status -ne 'ready') { throw 'Second replica is not ready' }

# Read-only MCP calls follow initialization. State was prepared by completed CLI calls.
$taskRequests = @(
    @{jsonrpc='2.0';id=1;method='initialize';params=@{protocolVersion='2025-03-26';capabilities=@{};clientInfo=@{name='packaged-stdio-observation';version=$taskVersion}}},
    @{jsonrpc='2.0';method='notifications/initialized'},
    @{jsonrpc='2.0';id=2;method='tools/list';params=@{}},
    @{jsonrpc='2.0';id=3;method='tools/call';params=@{name='workspace_get';arguments=@{record_id=$taskSaved.record_id}}}
) | ForEach-Object { $_ | ConvertTo-Json -Depth 10 -Compress }
[IO.File]::WriteAllText((Join-Path $taskRun 'stdio-input.jsonl'), ($taskRequests -join "`n") + "`n", $taskEncoding)
$taskStart = [Diagnostics.ProcessStartInfo]::new()
$taskStart.FileName = $taskNode
$taskStart.WorkingDirectory = $taskRelease
$taskStart.UseShellExecute = $false
$taskStart.CreateNoWindow = $true
$taskStart.RedirectStandardInput = $true
$taskStart.RedirectStandardOutput = $true
$taskStart.RedirectStandardError = $true
foreach ($taskArgument in @($taskApp, 'serve', '--local', $taskOtherLocal)) { $taskStart.ArgumentList.Add($taskArgument) }
$taskProcess = [Diagnostics.Process]::new()
$taskProcess.StartInfo = $taskStart
$taskResponses = [Collections.Generic.List[string]]::new()
try {
    if (!$taskProcess.Start()) { throw 'Could not start packaged stdio' }
    foreach ($taskRequest in $taskRequests) {
        $taskProcess.StandardInput.WriteLine($taskRequest)
        $taskProcess.StandardInput.Flush()
        if (($taskRequest | ConvertFrom-Json).id) {
            $taskLine = $taskProcess.StandardOutput.ReadLineAsync()
            if (!$taskLine.Wait(10000) -or $null -eq $taskLine.Result) { throw 'Stdio response timed out' }
            $taskResponses.Add($taskLine.Result)
        }
    }
    $taskProcess.StandardInput.Close()
    if (!$taskProcess.WaitForExit(10000)) { throw 'Stdio did not close after its input closed' }
    if ($taskProcess.ExitCode -ne 0) { throw ('Stdio failed: ' + $taskProcess.StandardError.ReadToEnd()) }
} finally {
    if ($taskProcess.Id -and !$taskProcess.HasExited) { $taskProcess.StandardInput.Close() }
    $taskProcess.Dispose()
}
[IO.File]::WriteAllText((Join-Path $taskRun 'stdio-output.jsonl'), ($taskResponses -join "`n") + "`n", $taskEncoding)
$taskMessages = @($taskResponses | ForEach-Object { $_ | ConvertFrom-Json })
$taskInitialization = $taskMessages | Where-Object { $_.id -eq 1 }
$taskTools = $taskMessages | Where-Object { $_.id -eq 2 }
$taskGet = $taskMessages | Where-Object { $_.id -eq 3 }
if ($taskInitialization.result.serverInfo.version -ne $taskVersion) { throw 'Stdio initialization failed' }
if ($taskTools.result.tools.Count -ne 12) { throw 'Unexpected MCP tool count' }
if ($taskGet.error -or $taskGet.result.isError -or !$taskGet.result.content) { throw 'Stdio handoff read failed' }
$taskHandoff = $taskGet.result.content[0].text | ConvertFrom-Json
if ($taskHandoff.state.data.goal -ne $taskData.goal -or $taskHandoff.state.data.current_stage -ne 'integration') { throw 'Handoff contents differ' }
$taskReport = @{
    version=$taskVersion; verified_at=[DateTime]::UtcNow.ToString('o'); observations=$taskRun
    checks=@{standalone_cli=$true;unchanged_handoff_skipped=$true;finished_result_hash=$true;second_replica=$true;provider_folder_change=$true;stdio_initialize=$true;mcp_tools=12;stdio_handoff=$true}
    real_synology_drive_test=$false;real_user_codex_settings_changed=$false
}
$taskReportPath = Join-Path (Split-Path -Parent $taskRelease) ("agent-workspace-$taskVersion-execution-validation.json")
[IO.File]::WriteAllText($taskReportPath, ($taskReport | ConvertTo-Json -Depth 10) + "`n", $taskEncoding)
$taskReport | ConvertTo-Json -Depth 10

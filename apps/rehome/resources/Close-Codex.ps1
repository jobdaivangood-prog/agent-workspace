[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Ordinary desktop Quit only. Never Stop-Process/taskkill or a global keystroke.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$session = [Diagnostics.Process]::GetCurrentProcess().SessionId
$russianFile = -join ([char[]](0x424,0x430,0x439,0x43b))
$apps = @(Get-Process ChatGPT,Codex -ErrorAction SilentlyContinue | Where-Object {
    $_.SessionId -eq $session -and $_.MainWindowHandle -ne 0 -and
    $_.Path -match '\\WindowsApps\\OpenAI\.(Codex|ChatGPT)_'
})
$requested = 0
foreach ($app in $apps) {
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($app.MainWindowHandle)
        $all = $root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
        $quit = @($all | Where-Object {
            $_.Current.ControlType -eq [Windows.Automation.ControlType]::MenuItem -and
            -not $_.Current.IsOffscreen -and $_.Current.Name -match '(Ctrl\+Q|^Quit\b|^Exit\b)'
        })
        if ($quit.Count -eq 0) {
            $file = @($all | Where-Object {
                $_.Current.ControlType -eq [Windows.Automation.ControlType]::MenuItem -and
                -not $_.Current.IsOffscreen -and $_.Current.Name -in @('File',$russianFile)
            })
            if ($file.Count -eq 1) {
                $pattern = $null
                if ($file[0].TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) {$pattern.Invoke()}
                elseif ($file[0].TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern,[ref]$pattern)) {$pattern.Expand()}
                Start-Sleep -Milliseconds 300
                $all = $root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
                $quit = @($all | Where-Object {
                    $_.Current.ControlType -eq [Windows.Automation.ControlType]::MenuItem -and
                    -not $_.Current.IsOffscreen -and $_.Current.Name -match '(Ctrl\+Q|^Quit\b|^Exit\b)'
                })
            }
        }
        $pattern = $null
        if ($quit.Count -eq 1 -and $quit[0].TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) {
            $pattern.Invoke(); $requested++
        } elseif ($app.CloseMainWindow()) { $requested++ }
    } catch {
        # The app may already have exited. The separate writer guard decides
        # whether it is safe to proceed; this helper never asserts quiescence.
    }
}
@{status='quit_requested';window_count=$apps.Count;requests=$requested} | ConvertTo-Json -Compress

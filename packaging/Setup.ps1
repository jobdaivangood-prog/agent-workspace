[CmdletBinding()]
param(
    [string]$Root,
    [string]$Local,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$taskSetupCode = 0
try {
    $taskNode = Join-Path $PSScriptRoot 'runtime/node.exe'
    $taskApp = Join-Path $PSScriptRoot 'app.mjs'
    if (!(Test-Path -LiteralPath $taskNode -PathType Leaf) -or !(Test-Path -LiteralPath $taskApp -PathType Leaf)) {
        throw 'Не найдена программа. Сначала распакуйте ZIP целиком, затем запустите Setup.cmd из распакованной папки.'
    }
    $taskArguments = @($taskApp, 'setup')
    if ($Root) { $taskArguments += @('--root', $Root) }
    if ($Local) { $taskArguments += @('--local', $Local) }
    & $taskNode @taskArguments
    if ($LASTEXITCODE -ne 0) { throw "Настройка завершилась с ошибкой (код $LASTEXITCODE). Сообщение приведено выше." }
    Write-Host 'Результат настройки приведён выше. Инструкция — START_HERE.md.' -ForegroundColor Green
} catch {
    $taskSetupCode = 1
    Write-Host ('Ошибка: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Скопируйте сообщение об ошибке. Для запуска двойным щелчком используйте Setup.cmd.'
} finally {
    if (!$NoPause) { [void](Read-Host 'Нажмите Enter, чтобы закрыть окно') }
}
exit $taskSetupCode

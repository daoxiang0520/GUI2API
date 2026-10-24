$ErrorActionPreference = 'Stop'
$winappExecutable = Join-Path $PSScriptRoot 'tools\winapp-cli\0.7.1\winapp.exe'
if (-not (Test-Path -LiteralPath $winappExecutable -PathType Leaf)) {
    throw "WinApp CLI not found: $winappExecutable"
}

$previousTelemetryOptOut = $env:WINAPP_CLI_TELEMETRY_OPTOUT
$previousLockDirectory = $env:WINAPP_UI_LOCK_DIRECTORY
try {
    $env:WINAPP_CLI_TELEMETRY_OPTOUT = '1'
    $env:WINAPP_UI_LOCK_DIRECTORY = Join-Path $PSScriptRoot '.winapp-ui'
    & $winappExecutable @args
    $winappExitCode = $LASTEXITCODE
}
finally {
    $env:WINAPP_CLI_TELEMETRY_OPTOUT = $previousTelemetryOptOut
    $env:WINAPP_UI_LOCK_DIRECTORY = $previousLockDirectory
}
exit $winappExitCode

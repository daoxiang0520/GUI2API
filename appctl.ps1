$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'appctl.py') @args
exit $LASTEXITCODE

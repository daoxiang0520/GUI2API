param([ValidateSet('uia','uia3','msaa','win32')][string]$Backend = 'uia')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$frameworkDirectory = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
$interop = Join-Path $PSScriptRoot 'Interop.UIAutomationClient.dll'
if (-not [IO.File]::Exists($interop)) { & (Join-Path $PSScriptRoot 'Build-UiaInterop.ps1') }
[void][Reflection.Assembly]::LoadFrom($interop)
$assemblies = @(
    $interop,
    (Join-Path $frameworkDirectory 'WPF\UIAutomationClient.dll'),
    (Join-Path $frameworkDirectory 'WPF\UIAutomationTypes.dll'),
    (Join-Path $frameworkDirectory 'WPF\WindowsBase.dll'),
    (Join-Path $frameworkDirectory 'System.Web.Extensions.dll'),
    (Join-Path $frameworkDirectory 'System.Core.dll')
    (Join-Path $frameworkDirectory 'Accessibility.dll')
)
$sources = @('AppCtl.cs','Win32.cs','Msaa.cs','Events.cs','Uia3.cs','Security.cs','InputTarget.cs','Recovery.cs') | ForEach-Object { Join-Path $PSScriptRoot $_ }
Add-Type -LiteralPath $sources -ReferencedAssemblies $assemblies
[AppCtl.Worker]::Run($Backend)

param(
    [string]$Assembly,
    [string]$Source,
    [Parameter(Mandatory=$true)][string]$FactoryType,
    [string]$FactoryMethod = 'CreateRoots'
)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$agentSource = Join-Path $PSScriptRoot 'DotNetObjects.cs'
if ($Source) {
    Add-Type -Path @($agentSource, (Resolve-Path -LiteralPath $Source).Path) -ReferencedAssemblies @('System.Web.Extensions','System.Core')
} else {
    Add-Type -Path $agentSource -ReferencedAssemblies @('System.Web.Extensions','System.Core')
}
if ($Assembly) { [void][Reflection.Assembly]::LoadFrom((Resolve-Path -LiteralPath $Assembly).Path) }
$type = $FactoryType -as [type]
if (-not $type) { throw 'Registered factory type was not found' }
$method = $type.GetMethod($FactoryMethod, [Reflection.BindingFlags]'Public,Static')
if (-not $method) { throw 'Registered factory method was not found' }
$protocolOutput = [Console]::Out
try {
    [Console]::SetOut([Console]::Error)
    $registeredRoots = $method.Invoke($null, @())
} finally { [Console]::SetOut($protocolOutput) }
$provider = New-Object AppCtlObjects.Provider -ArgumentList (,$registeredRoots)
$provider.Serve()

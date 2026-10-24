$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$framework = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
$source = Join-Path $PSScriptRoot 'ReliabilityFixture.cs'
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $digest = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($source))).Replace('-','').Substring(0,12) } finally { $sha.Dispose() }
$target = Join-Path $PSScriptRoot ('ReliabilityFixture-' + $digest + '.exe')
if (-not [IO.File]::Exists($target)) { Add-Type -LiteralPath $source -ReferencedAssemblies @('System.Windows.Forms','System.Drawing',(Join-Path $framework 'System.Web.Extensions.dll')) -OutputAssembly $target -OutputType WindowsApplication }
$target

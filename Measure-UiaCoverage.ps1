$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'winapp.ps1'
$executable = Join-Path $PSScriptRoot 'tools\winapp-cli\0.7.1\winapp.exe'
$windowJson = & $launcher ui list-windows --json
if ($LASTEXITCODE -ne 0) { throw 'Unable to enumerate desktop windows.' }
$windows = @((($windowJson -join "`n") | ConvertFrom-Json))

function Get-UiaNodes {
    param([object[]]$Elements)
    foreach ($element in $Elements) {
        $element
        if ($element.children) { Get-UiaNodes -Elements @($element.children) }
    }
}

function Read-UiaTree {
    param([long]$WindowHandle)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new($executable)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment['WINAPP_CLI_TELEMETRY_OPTOUT'] = '1'
    $startInfo.Environment['WINAPP_UI_LOCK_DIRECTORY'] = Join-Path $PSScriptRoot '.winapp-ui'
    foreach ($argument in @('ui', 'inspect', '-w', [string]$WindowHandle, '--depth', '8', '--json')) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill($true)
            [void]$process.WaitForExit(3000)
            return [pscustomobject]@{ status = 'timeout'; nodes = @(); errorCode = 'inspection_timeout' }
        }
        $outputText = $outputTask.GetAwaiter().GetResult()
        $errorText = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $errorCode = 'cli_error'
            foreach ($candidateText in @($outputText, $errorText)) {
                try {
                    $candidate = $candidateText | ConvertFrom-Json
                    if ($candidate.error.code) { $errorCode = $candidate.error.code }
                } catch { }
            }
            return [pscustomobject]@{ status = 'error'; nodes = @(); errorCode = $errorCode }
        }
        $tree = $outputText | ConvertFrom-Json
        $nodes = @($tree.windows | ForEach-Object { Get-UiaNodes -Elements @($_.elements) })
        return [pscustomobject]@{ status = 'ok'; nodes = $nodes; errorCode = $null }
    }
    finally { $process.Dispose() }
}

$samples = @(
    @{ app = 'Explorer'; choose = { $_.processName -eq 'explorer' -and $_.title -eq 'Bot - 文件资源管理器' } },
    @{ app = 'Calculator'; choose = { $_.title -eq '计算器' } },
    @{ app = 'QQ'; choose = { $_.processName -eq 'QQ' -and $_.title -eq 'QQ' } },
    @{ app = 'Weixin'; choose = { $_.processName -eq 'Weixin' -and $_.title -eq '微信' } },
    @{ app = 'WPS'; choose = { $_.processName -eq 'wps' -and $_.title } },
    @{ app = 'DeepSeek Harness'; choose = { $_.processName -eq 'DeepSeek Harness' -and $_.title } }
)
$results = @()
foreach ($sample in $samples) {
    $matches = @($windows | Where-Object -FilterScript $sample.choose)
    if ($matches.Count -ne 1) {
        $item = [pscustomobject][ordered]@{
            app = $sample.app; status = 'not_sampled'; matchingWindows = $matches.Count
        }
    }
    else {
        $tree = Read-UiaTree -WindowHandle $matches[0].hwnd
        $nodes = @($tree.nodes)
        $types = @($nodes | Group-Object type | Sort-Object Count -Descending | ForEach-Object {
            [pscustomobject]@{ type = $_.Name; count = $_.Count }
        })
        $item = [pscustomobject][ordered]@{
            app = $sample.app
            status = $tree.status
            hwnd = $matches[0].hwnd
            nodeCount = $nodes.Count
            namedNodeCount = @($nodes | Where-Object { -not [string]::IsNullOrWhiteSpace($_.name) }).Count
            actionableNodeCount = @($nodes | Where-Object isInvokable -eq $true).Count
            editRoleCount = @($nodes | Where-Object type -eq 'Edit').Count
            nodesWithReportedValues = @($nodes | Where-Object { $null -ne $_.PSObject.Properties['value'] }).Count
            typeCounts = $types
            errorCode = $tree.errorCode
        }
    }
    $results += $item
    $item | Select-Object app,status,nodeCount,actionableNodeCount,editRoleCount,errorCode | ConvertTo-Json -Compress
}

$report = [ordered]@{
    checkedAt = [DateTimeOffset]::UtcNow.ToString('o')
    winappVersion = '0.7.1'
    treeDepthLimit = 8
    inspectionTimeoutMs = 15000
    readOnly = $true
    screenshotCommandsUsed = $false
    actionCommandsUsed = $false
    limitations = @(
        'Counts describe the observed windows at this time and depth, not complete app compatibility.',
        'Actionable flags are reported by inspection; no action or write was executed.',
        'No control names, values, conversation text or document text are saved in this report.'
    )
    samples = $results
}
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'uia-coverage.json') -Encoding utf8

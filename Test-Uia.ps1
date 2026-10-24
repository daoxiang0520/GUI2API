$ErrorActionPreference = 'Stop'
$winappLauncher = Join-Path $PSScriptRoot 'winapp.ps1'

function Invoke-WinAppJson {
    param([string[]]$CliArguments)
    $raw = & $winappLauncher @CliArguments
    $commandExitCode = $LASTEXITCODE
    $rawText = $raw -join "`n"
    if ($commandExitCode -ne 0) {
        throw "WinApp command failed ($commandExitCode): $rawText"
    }
    $rawText | ConvertFrom-Json
}

$windows = @(Invoke-WinAppJson -CliArguments @('ui', 'list-windows', '-a', 'explorer', '--json'))
$targetWindows = @($windows | Where-Object {
    $_.className -eq 'CabinetWClass' -and $_.title -eq 'Bot - 文件资源管理器'
})
if ($targetWindows.Count -ne 1) {
    throw "Expected one Bot Explorer window; found $($targetWindows.Count)."
}
$windowHandle = [string]$targetWindows[0].hwnd
$inspection = Invoke-WinAppJson -CliArguments @('ui', 'inspect', '-w', $windowHandle, '--interactive', '--json')
function Get-DescendantElements {
    param([object[]]$Elements)
    foreach ($element in $Elements) {
        $element
        if ($element.children) {
            Get-DescendantElements -Elements @($element.children)
        }
    }
}
$elements = @(Get-DescendantElements -Elements @($inspection.windows[0].elements))
$searchRoots = @($elements | Where-Object automationId -eq 'FileExplorerSearchBox')
if ($searchRoots.Count -ne 1) {
    throw "Expected one search box container; found $($searchRoots.Count)."
}
$searchFields = @(Get-DescendantElements -Elements @($searchRoots[0].children) | Where-Object {
    $_.type -eq 'Edit' -and $_.automationId -eq 'TextBox'
})
if ($searchFields.Count -ne 1 -or -not $searchFields[0].selector) {
    throw 'Expected one search text field with a current semantic selector.'
}
$searchSelector = [string]$searchFields[0].selector
$searchArguments = @($searchSelector, '-w', $windowHandle)
$original = Invoke-WinAppJson -CliArguments (@('ui', 'get-value') + $searchArguments + @('--json'))
if ($null -eq $original.PSObject.Properties['text']) {
    throw 'Search box did not return a readable text value.'
}
$originalText = [string]$original.text
if ($originalText.Length -ne 0) {
    throw 'Search box already contains text; leaving it unchanged.'
}

$refresh = Invoke-WinAppJson -CliArguments @(
    'ui', 'invoke', 'refreshButton', '-w', $windowHandle, '--action', 'invoke', '--json'
)
$probeText = 'winapp-uia-probe'
$restored = $null
try {
    $setValue = Invoke-WinAppJson -CliArguments (
        @('ui', 'set-value', $searchSelector, $probeText, '-w', $windowHandle, '--json')
    )
    $readBack = Invoke-WinAppJson -CliArguments (@('ui', 'get-value') + $searchArguments + @('--json'))
    if ([string]$readBack.text -cne $probeText) {
        throw 'UIA value read-back did not match the text written.'
    }
}
finally {
    $restore = Invoke-WinAppJson -CliArguments (
        @('ui', 'set-value', $searchSelector, $originalText, '-w', $windowHandle, '--json')
    )
    $restored = Invoke-WinAppJson -CliArguments (@('ui', 'get-value') + $searchArguments + @('--json'))
    if ([string]$restored.text -cne $originalText) {
        throw 'Search box restoration could not be verified.'
    }
}

$report = [ordered]@{
    checkedAt = [DateTimeOffset]::UtcNow.ToString('o')
    version = '0.7.1'
    targetWindow = $targetWindows[0].title
    targetHwnd = $targetWindows[0].hwnd
    inspectedElementCount = $inspection.windows[0].elementCount
    screenshotCommandsUsed = $false
    coordinateInputCommandsUsed = $false
    invokeResult = $refresh
    setValueResult = $setValue
    readBack = $readBack
    originalTextRestored = ([string]$restored.text -ceq $originalText)
}
$reportPath = Join-Path $PSScriptRoot 'uia-validation.json'
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $reportPath -Encoding utf8
$report | ConvertTo-Json -Depth 12

param()
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    $operation = $managerType::RequestAsync()
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $asTask.MakeGenericMethod($managerType).Invoke($null, @($operation))
    if (-not $task.Wait(5000)) { throw 'Media session discovery exceeded 5 seconds' }
    $manager = $task.Result
    $rows = @()
    $allSessions = @($manager.GetSessions())
    foreach ($session in $allSessions) {
        $appId = [string]$session.SourceAppUserModelId
        if ($appId -notmatch '(?i)cloudmusic|netease|orpheus|weixin|wechat|qq\.exe|wps') { continue }
        $playback = $session.GetPlaybackInfo()
        $controls = $playback.Controls
        $capabilities = @{}
        foreach ($property in $controls.GetType().GetProperties()) {
            if ($property.PropertyType -eq [bool] -and $property.GetIndexParameters().Count -eq 0) {
                $capabilities[$property.Name] = $property.GetValue($controls, $null)
            }
        }
        $rows += @{
            app_id = $appId
            playback_status = [string]$playback.PlaybackStatus
            capabilities = $capabilities
            object_type = $session.GetType().FullName
            adapter = 'Windows.Media.Control / GSMTC'
        }
    }
    @{ok=$true;data=@{sessions=$rows;total_sessions=$allSessions.Count;playback_actions=0;media_titles_saved=$false}} | ConvertTo-Json -Depth 8 -Compress
} catch {
    @{ok=$false;error=@{code='media_api_unavailable';message=$_.Exception.Message;hresult=$_.Exception.HResult}} | ConvertTo-Json -Depth 5 -Compress
}

Set-StrictMode -Version Latest

function Get-SelectedSuperResolutionModel {
    if ((Get-SelectedTaskMode) -ne 'super_resolution') { return 'upscayl-lite-4x' }
    return [string]$SuperResolutionModelBox.SelectedItem.Tag
}

function Get-SuperResolutionModelLabel([string]$Model) {
    if ($Model -eq 'nomosuni-otf-medium') { return 'NomosUni OTF Medium（可选）' }
    return 'Upscayl Lite'
}

function Get-SuperResolutionProgressText($Work) {
    $progress = Get-JsonProperty $Work 'super_resolution' $null
    if ($null -eq $progress -or (Get-JsonProperty $progress 'phase' '') -eq 'completed') { return '' }
    $frames = Get-JsonProperty $progress 'processed_frames' 0
    return "超分已处理 $frames 帧"
}

function Start-SuperResolutionPreview {
    if ($script:IsRunning -or $null -ne $script:AccessBridge) { return }
    if (-not $script:QueueEnabled) { $InputErrorText.Text='请从启动翻译流入口使用短片对照。'; return }
    $seconds = 0.0
    if (-not [double]::TryParse($SuperResolutionPreviewStartBox.Text, [ref]$seconds) -or
        [double]::IsNaN($seconds) -or [double]::IsInfinity($seconds) -or $seconds -lt 0) {
        $InputErrorText.Text='请输入非负的对照起点秒数。'; return
    }
    $originalMode = Get-SelectedTaskMode
    $previewModel = Get-SelectedSuperResolutionModel
    $originalModel = [string]$SuperResolutionModelBox.SelectedItem.Tag
    $seconds = [Math]::Round($seconds, 3)
    $originalPreview = $script:SRPreviewStart
    $originalFiles = $script:SelectedFiles
    $script:CollectedQueueItems.Clear()
    try {
        Select-ComboBoxItemByTag $TaskModeBox 'super_resolution'
        Select-ComboBoxItemByTag $SuperResolutionModelBox $previewModel
        $script:SRPreviewStart = $seconds
        $script:SelectedFiles = @($InputVideoBox.Text)
        $errorText = Test-RunInputs
        if ($errorText) { throw $errorText }
        $script:CollectQueueRequests = $true
        Start-ConfirmedRun
        $payload = @{action='enqueue'; authorized=$true; items=@($script:CollectedQueueItems)}
        $script:QueueDraftActive = $true
        $script:QueueSubmitting = $true
        $script:CurrentQueueId = ''
        $script:QueueRunStamp = ''
        [void](Start-QueueCall $payload)
        $RunStateText.Text='正在建立短片对照'
        $StageText.Text='左侧原画、右侧超分。短片对照使用独立任务，不翻译字幕。'
        Set-UiPage 'run'
    } catch {
        $InputErrorText.Text=$_.Exception.Message
        $script:QueueSubmitting=$false
    } finally {
        $script:CollectQueueRequests=$false
        $script:CollectedQueueItems.Clear()
        $script:SRPreviewStart=$originalPreview
        $script:SelectedFiles=$originalFiles
        Select-ComboBoxItemByTag $TaskModeBox $originalMode
        Select-ComboBoxItemByTag $SuperResolutionModelBox $originalModel
    }
}

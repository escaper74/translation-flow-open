Set-StrictMode -Version Latest

function Start-DesktopAccess([ValidateSet('login','check')][string]$Action) {
    if ($script:IsRunning -or $null -ne $script:AccessBridge) { return }
    $script:AccessAction = $Action
    $PreparationText.Visibility = 'Visible'
    $script:AccessLines.Clear()
    $InputErrorText.Text = ''
    try {
        $script:AccessBridge = New-Object TranslationFlow.ProcessBridge
        $python = Resolve-TranslationPython
        if ($Action -eq 'login') {
            $PreparationText.Text = '正在打开官方登录页面，请在浏览器完成。无需向本程序提供账号密码。'
            $script:AccessBridge.StartCodexLogin($python, $ProjectRoot)
        } else {
            $PreparationText.Text = '正在检查本地工具和服务响应，尚未开始处理文件…'
            $mode = Get-SelectedTaskMode
            $checkMode = $(if ($mode -eq 'media_repackage') { 'repackage' } elseif ($mode -eq 'picture_only') { 'picture' } elseif ($mode -eq 'translation_picture_final') { 'full' } elseif ($mode -eq 'srt_translation') { 'srt' } else { 'video' })
            if ($checkMode -eq 'srt' -and @($script:SelectedFiles | Where-Object { [IO.Path]::GetExtension($_) -ine '.srt' }).Count) { $checkMode='video' }
            $settings = $(if ($checkMode -in @('repackage','picture')) { [pscustomobject]@{Id='codex';Model='cli-default'} } else { Get-SelectedProviderSettings })
            $endpoint = ''; $cap = ''
            if ($settings.Id -notin @('codex','deepseek')) { $endpoint=[string]$settings.EndpointProfile; $cap=[string]$settings.MaxOutputTokens }
            $script:AccessBridge.StartReadiness($python, $ProjectRoot, $checkMode, $settings.Id,
                $settings.Model, $endpoint, $cap, $ToolRootBox.Text.Trim(), $ApiKeyBox.Password)
        }
        Set-InputEnabled $false
    } catch {
        if ($null -ne $script:AccessBridge) { $script:AccessBridge.Dispose() }
        $script:AccessBridge=$null
        $InputErrorText.Text="无法开始检查：$($_.Exception.Message)"
        $PreparationText.Text='尚未开始处理文件，请修正提示后重试。'
        Set-InputEnabled $true
        return
    }
    $script:AccessTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:AccessTimer.Interval=[TimeSpan]::FromMilliseconds(200)
    $script:AccessTimer.Add_Tick({
        if ($null -eq $script:AccessBridge) { return }
        $line=''
        while ($script:AccessBridge.TryGetOutput([ref]$line)) { [void]$script:AccessLines.Add($line) }
        while ($script:AccessBridge.TryGetError([ref]$line)) { }
        if (-not $script:AccessBridge.HasExited) { return }
        $startTask=$false; $refresh=$false
        try {
            $script:AccessBridge.FinishReading()
            while ($script:AccessBridge.TryGetOutput([ref]$line)) { [void]$script:AccessLines.Add($line) }
            $result=($script:AccessLines -join "`n") | ConvertFrom-Json
            if ($script:AccessBridge.ExitCode -ne 0) { throw '检查进程未正常结束，请重新检测。' }
            $PreparationText.Text=[string]$result.message
            if ($script:AccessAction -eq 'login') { $refresh=$true }
            elseif ($result.status -eq 'ready') { $startTask=$true }
            else { $InputErrorText.Text=[string]$result.message }
        } catch {
            $InputErrorText.Text='无法完成开始前检查，尚未启动任务。请重新检测或检查本地工具设置。'
        } finally {
            $script:AccessTimer.Stop()
            $script:AccessBridge.Dispose()
            $script:AccessBridge=$null
            $script:AccessLines.Clear()
            Set-InputEnabled (-not $script:IsRunning)
        }
        if ($refresh) { Start-CodexStatusCheck }
        if ($startTask) {
            try { Start-AuthorizedBatch }
            catch { $InputErrorText.Text="无法建立任务：$($_.Exception.Message)" }
        }
    })
    $script:AccessTimer.Start()
}

function Start-CheckedBatch {
    # Recovery is owned by the existing backend: a completed result may need
    # neither a model installation nor a new billable connection request.
    if ($script:AccessResumeIntent -or $script:RestoringTask -or $script:PendingQueueResumeId -or $script:PendingSrtRequestPath) {
        $script:AccessResumeIntent=$false
        Start-AuthorizedBatch
    } else {
        Start-DesktopAccess 'check'
    }
}

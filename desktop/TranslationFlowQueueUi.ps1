# WPF adapter for 08's queue contract. Requests/events/results remain authoritative.
Set-StrictMode -Version Latest

function Initialize-QueueUi {
    $script:QueueEnabled = [IO.File]::Exists((Join-Path $ProjectRoot 'translation_flow/queue_cli.py'))
    $script:QueueCall = $null
    $script:QueueSnapshot = $null
    $script:CurrentQueueId = ''
    $script:QueueRunStamp = ''
    $script:QueueRenderedStatus = ''
    $script:PendingQueueResumeId = ''
    $script:CollectQueueRequests = $false
    $script:CollectedQueueItems = New-Object System.Collections.ArrayList
    $script:SelectedFiles = @()
    $script:QueuePollAt = [DateTime]::MinValue
    $script:QueueSubmitting = $false
    $script:PendingQueuePayload = $null
    foreach ($name in @('QueuePanel','QueueGrid','QueueStatusText','QueueSubmissionText','QueueSummaryText','ViewQueueItemButton','ResumeQueueItemButton','PauseQueueButton','StopQueueButton')) {
        Set-Variable -Name $name -Value (Get-Control $name) -Scope Script
    }
    if (-not $script:QueueEnabled) { return }
    if ([string]::IsNullOrWhiteSpace($QueueStateDirectory)) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($ProjectRoot).ToLowerInvariant()))).Replace('-','').Substring(0,12).ToLowerInvariant() }
        finally { $sha.Dispose() }
        $QueueStateDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) "TranslationFlow\$hash"
    }
    $script:QueueStatePath = [IO.Path]::GetFullPath($QueueStateDirectory)
    $QueuePanel.Visibility = 'Visible'
    $script:QueueTimer = New-Object Windows.Threading.DispatcherTimer
    $script:QueueTimer.Interval = [TimeSpan]::FromMilliseconds(200)
    $script:QueueTimer.Add_Tick({ Receive-QueueCall })
    $script:QueueTimer.Start()
}

function Set-SelectedFiles([string[]]$Paths) {
    $files = @($Paths | Select-Object -Unique)
    if (-not $files.Count) { return }
    foreach ($path in $files) { if (-not [IO.File]::Exists($path)) { $InputErrorText.Text = '有个文件找不到了，请重新选择。'; return } }
    if ((Get-SelectedTaskMode) -eq 'media_repackage' -and @($files | Where-Object { [IO.Path]::GetExtension($_) -ieq '.srt' }).Count) {
        $InputErrorText.Text = '换格式需要视频文件；如需翻译字幕，请选择“精修中文字幕”。'
        return
    }
    if (-not $script:QueueEnabled -and $files.Count -gt 1) { $InputErrorText.Text = '当前环境未接入后台队列，请先选择一个文件。'; return }
    Set-SelectedVideo $files[0]
    $script:SelectedFiles = @($files | ForEach-Object { [IO.Path]::GetFullPath($_) })
    $SelectedFilesText.Visibility = $(if ($files.Count -gt 1) { 'Visible' } else { 'Collapsed' })
    $SelectedFilesText.Text = "已选择 $($files.Count) 个文件，将按顺序处理"
    $SelectedFilesText.ToolTip = $script:SelectedFiles -join "`n"
    if ($files.Count -gt 1 -and (Get-SelectedTaskMode) -in @('translation','srt_translation')) {
        $SelectedFunctionText.Text = '已选择多个文件：视频自动识别对白，SRT 直接翻译，按顺序生成中文字幕。'
    }
    if ($files.Count -gt 1) { $DropTitle.Text = "$($files.Count) 个文件已准备好" }
    $script:PendingQueueResumeId = ''
    Start-SubtitleImportCheck
}

function Start-QueueCall([System.Collections.IDictionary]$Payload) {
    if ($null -ne $script:QueueCall) {
        if ($Payload.action -eq 'status') { return $false }
        if ($null -ne $script:PendingQueuePayload) { throw '上一项操作正在提交，请稍等。' }
        $script:PendingQueuePayload = $Payload
        return $true
    }
    $shell = [PowerShell]::Create()
    [void]$shell.AddScript({
        param($bridgePath,$python,$root,$statePath,$payload,$watchId)
        $ErrorActionPreference = 'Stop'
        . $bridgePath
        $queue = Invoke-TranslationFlowQueue -PythonPath $python -WorkingDirectory $root -StateDirectory $statePath -Payload $payload
        if ($payload.action -eq 'enqueue' -and $queue.paused) {
            # Start applies only to this newly submitted batch. Old paused tasks
            # still need their own resume confirmation, using the existing RPC.
            $added = @($queue.submission.added)
            if ($added.Count) {
                $queue = Invoke-TranslationFlowQueue -PythonPath $python -WorkingDirectory $root -StateDirectory $statePath -Payload @{action='resume'; ids=$added; authorized=$payload.authorized}
            }
        }
        $selected = @($queue.items | Where-Object { $_.id -eq $watchId }) | Select-Object -First 1
        if ($null -eq $selected) { $selected = @($queue.items | Where-Object { $_.status -in @('starting','running','stopping') }) | Select-Object -First 1 }
        if ($null -eq $selected) { $selected = @($queue.items | Where-Object { $_.status -eq 'queued' }) | Select-Object -First 1 }
        if ($null -eq $selected) { $selected = @($queue.items) | Select-Object -Last 1 }
        $events = @()
        if ($null -ne $selected) {
            $eventPath = Join-Path ([IO.Path]::GetDirectoryName($selected.request_path)) 'events.jsonl'
            if ([IO.File]::Exists($eventPath)) {
                # Read small public progress records off the WPF dispatcher.
                $events = @(Get-Content -LiteralPath $eventPath -Encoding UTF8 -Tail 1200 | ForEach-Object {
                    try { $item = $_ | ConvertFrom-Json; if ($item.schema_version -eq 1 -and $item.job_id -eq $selected.job_id) { $item } } catch { }
                })
            }
        }
        [pscustomobject]@{ Queue=$queue; Watched=$selected; Events=$events }
    }).AddArgument((Join-Path $PSScriptRoot 'TranslationFlowQueueBridge.ps1')).AddArgument((Resolve-TranslationPython)).AddArgument($ProjectRoot).AddArgument($script:QueueStatePath).AddArgument($Payload).AddArgument($script:CurrentQueueId)
    try {
        $async = $shell.BeginInvoke()
        $script:QueueCall = @{ Shell=$shell; Async=$async; Action=[string]$Payload.action }
        return $true
    } catch { $shell.Dispose(); throw }
}

function Receive-QueueCall {
    if ($null -eq $script:QueueCall) {
        if ([DateTime]::UtcNow -ge $script:QueuePollAt) {
            $script:QueuePollAt = [DateTime]::UtcNow.AddSeconds(2)
            try { [void](Start-QueueCall @{ action='status' }) } catch { $QueueStatusText.Text = '暂时连不上后台，正在重试。' }
        }
        return
    }
    if (-not $script:QueueCall.Async.IsCompleted) { return }
    $call = $script:QueueCall
    $script:QueueCall = $null
    try {
        $values = @($call.Shell.EndInvoke($call.Async))
        if ($call.Shell.HadErrors -or -not $values.Count) { throw '后台暂未确认操作。请刷新任务，确认进度后再试。' }
        Apply-QueueSnapshot $values[-1]
        $script:QueueLastError = ''
        $QueueStatusText.ToolTip = $null
        if ($call.Action -eq 'enqueue') { Show-QueueSubmission $values[-1].Queue }
    } catch {
        $QueueStatusText.Text = '暂时连不上后台，正在重试。已有任务可能仍在运行。'
        $failureMessage = $_.Exception.Message
        if ($call.Shell.Streams.Error.Count) { $failureMessage = $call.Shell.Streams.Error[0].Exception.Message }
        $script:QueueLastError = "$($_.Exception.Message) at $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line)"
        if ($call.Shell.Streams.Error.Count) { $script:QueueLastError = ($call.Shell.Streams.Error | ForEach-Object { "$_ / $($_.InvocationInfo.PositionMessage) / $($_.ScriptStackTrace)" }) -join "`n" }
        $QueueStatusText.ToolTip = $script:QueueLastError
        if ($call.Action -ne 'status') {
            $InputErrorText.Text = "后台未确认开始：$failureMessage 请在任务页核对是否已接收；已保存的请求可以继续。"
            Set-UiPage 'setup'
        }
        Add-EventLog "后台状态暂时不可用：$failureMessage"
    } finally {
        $call.Shell.Dispose()
        if ($call.Action -in @('enqueue','resume')) { $script:QueueSubmitting = $false }
        $StartButton.IsEnabled = -not $script:IsRunning -and -not $script:QueueSubmitting -and [IO.File]::Exists($InputVideoBox.Text)
        $script:QueuePollAt = [DateTime]::UtcNow.AddSeconds(1)
        if ($null -ne $script:PendingQueuePayload) {
            $next = $script:PendingQueuePayload
            $script:PendingQueuePayload = $null
            [void](Start-QueueCall $next)
        }
    }
}

function Test-QueueItemPaused($Item) {
    return $Item.status -eq 'paused' -or ($Item.status -eq 'queued' -and $null -ne $script:QueueSnapshot -and (Get-JsonProperty $script:QueueSnapshot 'paused' $false))
}

function Apply-QueueSnapshot($Reply) {
    $queue = $Reply.Queue
    if ([IO.Path]::GetFullPath([string]$queue.project_root).TrimEnd('\') -ne [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')) { throw '后台与当前软件目录不一致。' }
    $script:QueueSnapshot = $queue
    $labels = @{ queued='等待中'; starting='正在准备'; running='进行中'; stopping='正在停止'; paused='已暂停'; stopped='已停止'; failed='未完成'; interrupted='需要继续'; completed='已完成' }
    $priorId = $(if ($null -ne $QueueGrid.SelectedItem) { $QueueGrid.SelectedItem.id } else { '' })
    $rows = @($queue.items | ForEach-Object {
        [pscustomobject]@{ id=$_.id; SourceName=[IO.Path]::GetFileName($_.source); StatusLabel=$(if (Test-QueueItemPaused $_) { '已暂停' } else { $labels[$_.status] }); ProgressLabel="$([Math]::Round($_.progress))%"; Item=$_ }
    })
    $QueueGrid.ItemsSource = $rows
    $QueueGrid.SelectedItem = @($rows | Where-Object { $_.id -eq $priorId }) | Select-Object -First 1
    $active = @($queue.items | Where-Object { $_.status -in @('starting','running','stopping') })
    $waiting = @($queue.items | Where-Object { $_.status -eq 'queued' -and -not (Test-QueueItemPaused $_) })
    $paused = @($queue.items | Where-Object { (Test-QueueItemPaused $_) -or $_.status -in @('stopped','failed','interrupted') })
    $QueueStatusText.Text = $(if ($queue.paused) { '后续任务已暂停；已有进度会保留。' } elseif ($active.Count) { "正在处理 1 个文件，另有 $($waiting.Count) 个等待中。" } else { "共 $($rows.Count) 个任务；$($paused.Count) 个可继续。" })
    $queueError=[string](Get-JsonProperty $queue 'error' '')
    if ($queueError) { $QueueStatusText.Text=$queueError }
    Update-DesignTaskCards
    $QueueSummaryText.Visibility = $(if ($rows.Count -gt 1) { 'Visible' } else { 'Collapsed' })
    $QueueSummaryText.Text = $QueueStatusText.Text
    $ShowTasksButton.Content = $(if ($active.Count -or $waiting.Count) { "任务 · $($active.Count + $waiting.Count)" } else { '任务' })
    $watched = $Reply.Watched
    if ($null -eq $watched) { return }
    if ($script:UiPage -eq 'setup' -and -not $script:QueueSubmitting -and $watched.status -notin @('starting','running','stopping')) { return }
    $stamp = "$($watched.id)/$(Get-JsonProperty $watched 'started_at' 0)"
    if ($script:QueueRunStamp -ne $stamp) {
        $script:QueueRunStamp = $stamp
        $script:CurrentQueueId = $watched.id
        $script:CurrentRequestPath = $watched.request_path
        $script:CurrentWorkflowMode = $watched.mode
        $script:CurrentProviderId = $watched.provider
        $script:CurrentJobId = $watched.job_id
        $script:LastSeq = 0
        $script:CompletedEvent = $null; $script:FailedEvent = $null; $script:CancelledEvent = $null
        $script:Finalized = $false; $script:ProtocolFailure = ''; $script:QueueRenderedStatus = ''
        $oldPage = $script:UiPage
        if ($oldPage -eq 'setup' -and -not $script:SelectedFiles.Count -and $watched.status -in @('starting','running','stopping')) { $oldPage='run' }
        $InputVideoBox.Text = $watched.source
        Reset-RunSummary
        Reset-RunPresentation
        Set-UiPage $oldPage
        $started = Get-JsonProperty $watched 'started_at' $null
        if ($null -ne $started) { $script:JobStartedAt = [DateTimeOffset]::FromUnixTimeSeconds([long]$started) }
    }
    $script:IsRunning = $watched.status -in @('starting','running','stopping')
    foreach ($event in $Reply.Events) {
        if ([int]$event.seq -le $script:LastSeq) { continue }
        # Resume appends new events; skip the preceding attempt's terminal records.
        $eventTime = ConvertTo-UiEventTime $event
        $started = Get-JsonProperty $watched 'started_at' $null
        if ($null -ne $started -and $null -ne $eventTime -and $eventTime.ToUnixTimeMilliseconds() -lt ([double]$started * 1000 - 1000)) { continue }
        Handle-BackendEvent $event
    }
    Set-ProgressValue ([double]$watched.progress)
    $BackgroundButton.Visibility = $(if ($script:IsRunning -or $waiting.Count) { 'Visible' } else { 'Collapsed' })
    $StopRunButton.Visibility = $(if ($script:IsRunning) { 'Visible' } else { 'Collapsed' })
    $StopRunButton.IsEnabled = $watched.status -in @('starting','running')
    $StopRunButton.Content = $(if ($watched.status -eq 'stopping') { '正在保存进度…' } else { '停止并保留进度' })
    if ($watched.status -eq 'stopping') { $StageText.Text = '正在保存当前进度，完成后会停止。' }
    $displayStatus = $(if (Test-QueueItemPaused $watched) { 'paused' } else { $watched.status })
    if ($script:QueueRenderedStatus -ne $displayStatus) {
        $script:QueueRenderedStatus = $displayStatus
        if ($watched.status -eq 'completed') {
            if ($null -ne $script:CompletedEvent) {
                # Reuse the existing file/report gate after the worker verifies termination.
                Finalize-Result
            } else { Complete-Run $false '没有找到本次完成记录，请查看任务详情。' }
        } elseif ($watched.status -in @('failed','interrupted')) {
            $errorText = $(if ($null -ne $script:FailedEvent) { Get-EventMessage $script:FailedEvent } else { [string](Get-JsonProperty $watched 'detail' $watched.message) })
            Complete-Run $false $errorText
            $hint=[string](Get-JsonProperty $watched 'recovery_hint' '')
            if ($hint) { $RecoverySummaryText.Text=$hint }
        } elseif ($watched.status -eq 'stopped') { Complete-Run $false '进度已保存' $true }
        elseif ($watched.status -in @('queued','paused')) {
            $RunStateText.Text = $(if ($displayStatus -eq 'paused') { '任务已暂停' } else { '等待前面的任务完成' })
            $StageText.Text = $(if ($displayStatus -eq 'paused') { '文件和设置已保存，点击继续处理。' } else { '文件和设置已经保存。' })
            $RetryRunButton.Visibility = $(if ($displayStatus -eq 'paused') { 'Visible' } else { 'Collapsed' })
        }
    }
    if (-not $script:QueueSubmitting) { Set-InputEnabled (-not $script:IsRunning) }
}

function Test-QueueSessionKeyAvailable {
    if (-not $script:QueueEnabled -or -not $script:PendingQueueResumeId -or $null -eq $script:QueueSnapshot) { return $false }
    $row=@($script:QueueSnapshot.items | Where-Object { $_.id -eq $script:PendingQueueResumeId })
    return $row.Count -gt 0 -and (Get-JsonProperty $row[0] 'has_session_key' $false) -eq $true
}

function Show-QueueSubmission($Queue) {
    $submission=Get-JsonProperty $Queue 'submission' $null
    $skipped=@(Get-JsonProperty $submission 'skipped' @())
    $QueueSubmissionText.Visibility=$(if ($skipped.Count) { 'Visible' } else { 'Collapsed' })
    if (-not $skipped.Count) { return }
    $QueueSubmissionText.Text="$($skipped.Count) 个文件未加入，请检查后重新选择。其他文件会继续处理。"
    $QueueSubmissionText.ToolTip=($skipped | ForEach-Object { "$($_.request_path): $($_.message)" }) -join "`n"
    Add-Warning $QueueSubmissionText.Text
}

function Start-AuthorizedBatch {
    if (-not $script:QueueEnabled) { Start-ConfirmedRun; return }
    if ($script:PendingQueueResumeId) {
        $resumeError = Test-QueueResumeSettings
        if ($resumeError) { throw $resumeError }
        $keys = @{}
        if ($ApiKeyBox.Password) { $keys[$script:PendingQueueResumeId] = $ApiKeyBox.Password }
        $payload = @{ action='resume'; ids=@($script:PendingQueueResumeId); authorized=$true; credentials=$keys }
        $ApiKeyBox.Clear()
        $script:PendingQueueResumeId = ''
    } else {
        $paths = $(if ($script:SelectedFiles.Count) { $script:SelectedFiles } else { @($InputVideoBox.Text) })
        $credential = $ApiKeyBox.Password
        $originalMode = Get-SelectedTaskMode
        $script:CollectedQueueItems.Clear()
        $script:CollectQueueRequests = $true
        try {
            foreach ($path in $paths) {
                $effectiveMode = Get-ImportedTaskMode $path $originalMode
                if ($effectiveMode -eq 'none') { continue }
                $InputVideoBox.Text = $path
                if ($effectiveMode -in @('translation','srt_translation')) { $effectiveMode = $(if ([IO.Path]::GetExtension($path) -ieq '.srt') { 'srt_translation' } else { 'translation' }) }
                Select-ComboBoxItemByTag $TaskModeBox $effectiveMode
                $ApiKeyBox.Password = $credential
                $script:RestoringTask = [bool]$script:PendingSrtRequestPath
                try { $errorText = Test-RunInputs }
                finally { $script:RestoringTask = $false }
                if ($errorText) { throw $errorText }
                Start-ConfirmedRun
            }
            $payload = @{ action='enqueue'; authorized=$true; items=@($script:CollectedQueueItems) }
        } finally { $script:CollectQueueRequests=$false; $ApiKeyBox.Clear(); $credential=$null; Select-ComboBoxItemByTag $TaskModeBox $originalMode }
    }
    $script:QueueSubmitting = $true
    $StartButton.IsEnabled = $false
    $script:SelectedFiles = @()
    $script:CurrentQueueId = ''
    $script:QueueRunStamp = ''
    try {
        [void](Start-QueueCall $payload)
        $RunStateText.Text = '正在交给后台'
        $StageText.Text = '文件准备好后会按顺序处理。'
        Set-UiPage 'run'
    } catch {
        $script:QueueSubmitting = $false
        $StartButton.IsEnabled = -not $script:IsRunning -and [IO.File]::Exists($InputVideoBox.Text)
        throw
    } finally { $script:CollectedQueueItems.Clear(); $payload=$null }
}

function Test-QueueResumeSettings {
    if (-not $script:QueueEnabled -or -not $script:PendingQueueResumeId) { return '' }
    $row=@($script:QueueSnapshot.items | Where-Object { $_.id -eq $script:PendingQueueResumeId })
    if (-not $row.Count) { return '没有找到原任务，请从任务列表重新选择。' }
    $original=Get-TaskCenterResumeRequest $row[0].request_path
    $changed=(Get-SelectedTaskMode) -ne $original.TaskMode -or
        [IO.Path]::GetFullPath($InputVideoBox.Text) -ne $original.SourcePath -or
        [IO.Path]::GetFullPath($OutputDirectoryBox.Text) -ne $original.OutputDirectory -or
        (Get-SelectedOutputTarget) -ne $original.OutputTarget
    if ($original.TaskMode -in @('translation','translation_picture_final')) {
        $changed=$changed -or $script:CurrentWhisperAModel -ne $original.AsrAModel
    }
    if ($original.TaskMode -notin @('media_repackage','picture_only')) {
        $settings=Get-SelectedProviderSettings
        $changed=$changed -or $settings.Id -ne $original.ProviderId -or $settings.Host -ne $original.ProviderHost -or
            $settings.Model -ne $original.ProviderModel -or [string]$ProfileBox.SelectedItem.Tag -ne $original.ProfileId
        if ($settings.Id -notin @('codex','deepseek')) {
            $changed=$changed -or $settings.EndpointProfile -ne $original.EndpointProfile -or $settings.MaxOutputTokens -ne $original.MaxOutputTokens
        }
    }
    if ($changed) { return '继续任务需要沿用原设置。请从任务列表重新选择；更换设置请新建任务。' }
    return ''
}

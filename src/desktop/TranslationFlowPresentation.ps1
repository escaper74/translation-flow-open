# Presentation only: consume public events, never synthesize completion or task identity.
Set-StrictMode -Version Latest

function Set-UiPage([string]$Page) {
    $Window.FindName('HomePanel').Visibility = $(if ($Page -eq 'home') { 'Visible' } else { 'Collapsed' })
    $Window.FindName('LibraryPageHost').Visibility = $(if ($Page -eq 'library') { 'Visible' } else { 'Collapsed' })
    $SetupPanel.Visibility = $(if ($Page -eq 'setup') { 'Visible' } else { 'Collapsed' })
    $RunPanel.Visibility = $(if ($Page -eq 'run') { 'Visible' } else { 'Collapsed' })
    $TaskCenterPanel.Visibility = $(if ($Page -eq 'tasks') { 'Visible' } else { 'Collapsed' })
    $script:UiPage = $Page
    Set-DesignNavigation $Page
    $MainScroll.ScrollToTop()
}

function Update-ProgressPresentation {
    $unknownAsrProgress = $script:IsRunning -and $script:LastActiveStage -in @('asr_a','whisper_a','asr_c','whisper_c')
    (Get-Control 'ProgressCaption').Text = $(if ($unknownAsrProgress) { '阶段状态' } else { '总进度' })
    $ProgressText.Text = $(if ($unknownAsrProgress) { '识别中' } else { '{0}%' -f [Math]::Round($script:CurrentProgress) })
    # ASR currently emits only stage boundaries. Do not present the starting
    # stage weight as a live percentage of the audio already recognized.
    $ProgressBar.Visibility = $(if ($unknownAsrProgress) { 'Hidden' } else { 'Visible' })
}

function Reset-RunPresentation {
    $script:CurrentProgress = 0.0
    $script:EtaObservation = $null
    $script:EtaRange = ''
    $script:RunEndedAt = $null
    $script:RunPresentationState = 'running'
    $ProgressBar.BeginAnimation([Windows.Controls.Primitives.RangeBase]::ValueProperty, $null)
    $ProgressBar.Value = 0
    $ProgressBar.Visibility = 'Visible'
    (Get-Control 'ProgressCaption').Text = '总进度'
    $ProgressText.Text = '0%'
    $ProgressBar.Foreground = Convert-ToBrush '#00A5A2'
    $ProgressText.Foreground = Convert-ToBrush '#00A5A2'
    $RunStateText.Foreground = Convert-ToBrush '#142333'
    $RunSourceText.Text = [IO.Path]::GetFileName($InputVideoBox.Text)
    $RunSourceText.ToolTip = $InputVideoBox.Text
    $ElapsedText.Text = '00:00'
    $EtaText.Text = '正在估算'
    (Get-Control 'EtaLabel').Text = '当前阶段预计还需'
    $RecoverySummaryText.Text = ''
    $RunDetails.IsExpanded = $false
    $ResultFilesPanel.Visibility = 'Collapsed'
    $RetryRunButton.Visibility = 'Collapsed'
    $OpenOutputButton.Visibility = 'Collapsed'
    $OpenResultButton.Visibility = 'Collapsed'
    $RecognizeStep.Visibility = $(if ($script:CurrentWorkflowMode -eq 'srt_translation' -or $script:CurrentWorkflowMode -in @('media_repackage','picture_only')) { 'Collapsed' } else { 'Visible' })
    $TranslateStep.Visibility = $(if ($script:CurrentWorkflowMode -in @('media_repackage','picture_only')) { 'Collapsed' } else { 'Visible' })
    $ExportStep.Visibility = $(if ($script:CurrentWorkflowMode -eq 'srt_translation') { 'Collapsed' } else { 'Visible' })
    if ($script:CurrentWorkflowMode -eq 'picture_only') { $RecognizeStep.Visibility='Visible' }
    (Get-Control 'RecognizeLabel').Text = $(if ($script:CurrentWorkflowMode -eq 'picture_only') { '去马' } else { '识别' })
    (Get-Control 'TranslateLabel').Text = $(if ($script:CurrentWorkflowMode -eq 'translation_picture_final') { '字幕与去马' } else { '翻译' })
    Set-StageIndicators ''
    Set-UiPage 'run'
}

function Set-StageIndicators([string]$Stage, [bool]$Completed = $false) {
    $index = 0
    if ($Stage -match '^(source_review|asr_review|context|translation|translate_initial|polish|kana_repair|subtitle_validation)$') { $index = 1 }
    if ($Stage -match '^(packaging|mkv_packaging|technical_validation|media_probe|compatibility_preflight|media_repackage|mp4_delivery|final_mux)$') { $index = 2 }
    $dots = @($RecognizeDot, $TranslateDot, $ExportDot)
    $marks = @($RecognizeMark, $TranslateMark, $ExportMark)
    for ($i = 0; $i -lt 3; $i++) {
        $active = $Completed -or $i -le $index
        $dots[$i].Background = Convert-ToBrush $(if ($active) { '#00A5A2' } else { '#DDF2F1' })
        $marks[$i].Foreground = Convert-ToBrush $(if ($active) { '#FFFFFF' } else { '#778599' })
        $marks[$i].Text = $(if ($Completed -or $i -lt $index) { [char]0x2713 } else { [string]($i + 1) })
        if (-not $Completed -and $i -eq $index -and $script:RunPresentationState -in @('failed','stopped')) {
            $dots[$i].Background = Convert-ToBrush '#BA7A14'
        }
    }
}

function Update-EtaObservation($Event) {
    $stage = [string](Get-JsonProperty $Event 'stage' '')
    # Overall percentages are stage weights, not elapsed-time predictions.
    # Estimate only a measurable current phase from fresh completed-work observations.
    if ($stage -notin @('context','translate_initial','polish')) { $script:EtaObservation = $null; $script:EtaRange = ''; return }
    $detail = [string](Get-JsonProperty $Event 'detail' '')
    if ($detail -notmatch '(\d+)\s*/\s*(\d+)' -or (Get-JsonProperty $Event 'resumed' $false) -eq $true) { return }
    $current = [int]$Matches[1]; $total = [int]$Matches[2]
    if ($total -le 0 -or $current -gt $total -or $detail -match '_started') { return }
    $at = ConvertTo-UiEventTime $Event
    if ($null -eq $at) { return }
    $previous = $script:EtaObservation
    if ($null -eq $previous -or $previous.Stage -ne $stage -or $previous.Total -ne $total -or $current -lt $previous.Current) {
        $script:EtaObservation = @{ Stage=$stage; Total=$total; Current=$current; At=$at; LastAt=$at }
        $script:EtaRange = ''
        return
    }
    if ($current -le $previous.Current) { return }
    $seconds = ($at - $previous.At).TotalSeconds
    $previous.LastAt = $at
    if ($seconds -lt 10) { return }
    $remaining = $seconds / ($current - $previous.Current) * ($total - $current)
    if ($current -eq $total) { $script:EtaRange = '即将完成'; return }
    $low = [Math]::Max(1, [Math]::Floor($remaining * 0.75 / 60))
    $high = [Math]::Max($low + 1, [Math]::Ceiling($remaining * 1.35 / 60))
    $script:EtaRange = $(if ($remaining -lt 45) { '不到 1 分钟' } else { "${low}–${high} 分钟" })
}

function Update-LivePresentation($Event) {
    $eventName = [string](Get-JsonProperty $Event 'event' '')
    $stage = [string](Get-JsonProperty $Event 'stage' '')
    if ($eventName -in @('stage_started','stage_progress','stage_completed')) {
        if ($stage) {
            if ($script:LastActiveStage -ne $stage) { $script:EtaObservation = $null; $script:EtaRange = '' }
            $script:LastActiveStage = $stage
        }
        if ($eventName -ne 'stage_completed') {
            $RunStateText.Text = Get-StageLabel $stage
            $detail = [string](Get-JsonProperty $Event 'detail' '')
            $StageText.Text = ''
            if ($stage -in @('asr_a','whisper_a','asr_c','whisper_c')) {
                $StageText.Text = "$(Get-StageLabel $stage)；完成后自动更新进度。"
            }
            if ($detail -match '(\d+)\s*/\s*(\d+)') { $StageText.Text = "已完成 $($Matches[1]) / $($Matches[2])" }
            if ((Get-JsonProperty $Event 'resumed' $false) -eq $true) { $StageText.Text = '正在接着上次的进度继续' }
            Update-EtaObservation $Event
        }
        Set-StageIndicators $stage
    }
    if ($eventName -eq 'job_started') { $RunStateText.Text = '正在准备'; $StageText.Text = '已接收任务' }
    Update-ClockPresentation
}

function Update-ClockPresentation {
    if ($null -ne $script:JobStartedAt) {
        $end = $(if ($null -ne $script:RunEndedAt) { $script:RunEndedAt } else { [DateTimeOffset]::UtcNow })
        $seconds = [Math]::Max(0, ($end - $script:JobStartedAt).TotalSeconds)
        $span = [TimeSpan]::FromSeconds($seconds)
        $ElapsedText.Text = $(if ($span.TotalHours -ge 1) { '{0}:{1:00}:{2:00}' -f [int][Math]::Floor($span.TotalHours),$span.Minutes,$span.Seconds } else { '{0:00}:{1:00}' -f [int][Math]::Floor($span.TotalMinutes),$span.Seconds })
    }
    if (-not $script:IsRunning) { return }
    Update-DurationSummary
    Update-ProgressPresentation
    $EtaText.Text = $(if ($script:EtaRange) { $script:EtaRange } elseif ($script:LastActiveStage -in @('asr_a','whisper_a','asr_c','whisper_c')) { '暂无可靠估算' } else { '正在估算' })
    if ($null -ne $script:EtaObservation -and ([DateTimeOffset]::UtcNow - $script:EtaObservation.LastAt).TotalSeconds -gt 120) { $EtaText.Text = '等待新的进度' }
}

function Complete-RunPresentation([bool]$Succeeded, [bool]$Cancelled) {
    $StopRunButton.Visibility = 'Collapsed'
    $terminal = $(if ($Succeeded) { $script:CompletedEvent } elseif ($Cancelled) { $script:CancelledEvent } else { $script:FailedEvent })
    $script:RunEndedAt = ConvertTo-UiEventTime $terminal
    if ($null -eq $script:RunEndedAt) { $script:RunEndedAt = [DateTimeOffset]::UtcNow }
    $script:RunPresentationState = $(if ($Succeeded) { 'completed' } elseif ($Cancelled) { 'stopped' } else { 'failed' })
    Update-ProgressPresentation
    $EtaText.Text = $(if ($Succeeded) { '已完成' } elseif ($Cancelled) { '已停止' } else { '等待继续' })
    (Get-Control 'EtaLabel').Text = '状态'
    $OpenOutputButton.Visibility = $(if ($Succeeded) { 'Visible' } else { 'Collapsed' })
    $OpenResultButton.Visibility = $OpenOutputButton.Visibility
    $ResultFilesPanel.Visibility = $OpenOutputButton.Visibility
    $RetryRunButton.Visibility = $(if (-not $Succeeded -and [IO.File]::Exists($script:CurrentRequestPath)) { 'Visible' } else { 'Collapsed' })
    $BackgroundButton.Visibility = 'Collapsed'
    if ($Succeeded) {
        $RunStateText.Text = $(if ($script:CurrentWorkflowMode -in @('media_repackage','picture_only','translation_picture_final')) { '视频已导出' } else { '翻译好了' })
        $RunStateText.Foreground = Convert-ToBrush '#142333'
        $StageText.Text = $(if ($script:CurrentWorkflowMode -eq 'srt_translation') { '打开字幕，看看翻译效果。' } else { '打开视频，看看字幕效果。' })
        $OpenResultButton.Content = $(if ($script:CurrentWorkflowMode -eq 'srt_translation') { '打开字幕' } else { '打开视频' })
        $RecoverySummaryText.Foreground = Convert-ToBrush '#778599'
        $outputFiles = Get-JsonProperty $script:CompletedEvent 'outputs' $null
        if ($null -ne $outputFiles) {
            $subtitle = [string](Get-JsonProperty $outputFiles 'chinese_srt' '')
            $SubtitleResultText.Text = $(if ($subtitle) { '字幕 · ' + [IO.Path]::GetFileName($subtitle) } else { '' })
            $SubtitleResultText.ToolTip = $subtitle
            $videoFiles = @('final_mkv','final_mp4','final_media' | ForEach-Object { $path=[string](Get-JsonProperty $outputFiles $_ ''); if ($path) { $path } })
            $MkvResultText.Text = ($videoFiles | ForEach-Object { '视频 · ' + [IO.Path]::GetFileName($_) }) -join "`n"
            $MkvResultText.ToolTip = $videoFiles -join "`n"
        }
        Set-StageIndicators $script:LastActiveStage $true
    } else {
        $ProgressBar.Foreground = Convert-ToBrush '#BA7A14'
        $ProgressText.Foreground = Convert-ToBrush '#96600D'
        $RunStateText.Foreground = Convert-ToBrush '#142333'
        $RecoverySummaryText.Foreground = Convert-ToBrush '#778599'
        if ($Cancelled) { $RunStateText.Text = '已停止，进度已保留'; $StageText.Text = '准备好后，可以从这里继续。'; $RecoverySummaryText.Text = '已完成的部分会直接复用。' }
        Set-StageIndicators $script:LastActiveStage
    }
    Update-ClockPresentation
}

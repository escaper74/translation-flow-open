# Read-only import inspection, independent of the queue and recovery checkpoints.
Set-StrictMode -Version Latest

function Initialize-SubtitleImport {
    $script:ImportReports = @{}
    $script:ImportPaths = @()
    $script:ImportBridge = $null
    $script:ImportCurrentPath = ''
    $script:ImportCurrentIdentity = ''
    $script:ImportLines = New-Object Collections.ArrayList
    $script:ImportAutoDeselected = $false
    foreach ($name in @('SubtitleImportPanel','SubtitleImportText','ForceSubtitleTranslationBox')) {
        Set-Variable -Scope Script -Name $name -Value (Get-Control $name)
    }
    $script:ImportTimer = New-Object Windows.Threading.DispatcherTimer
    $script:ImportTimer.Interval = [TimeSpan]::FromMilliseconds(200)
    $script:ImportTimer.Add_Tick({ Receive-SubtitleImport })
    $ForceSubtitleTranslationBox.Add_Click({
        if ($ForceSubtitleTranslationBox.IsChecked) {
            $script:ImportAutoDeselected = $false
            $SubtitleFeatureButton.IsChecked = $true
            Sync-FeatureMode
        }
        Update-SubtitleImportSummary
    })
}

function Get-ImportIdentity([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return '' }
    $file = Get-Item -LiteralPath $Path
    $parts = @("$($file.FullName)/$($file.Length)/$($file.LastWriteTimeUtc.Ticks)")
    # Sidecars can be added/edited without changing the video itself.
    foreach ($sidecar in @(Get-ChildItem -LiteralPath $file.DirectoryName -File | Where-Object {
        $_.Extension -in @('.srt','.ass','.ssa','.vtt') -and ($_.BaseName -eq $file.BaseName -or $_.BaseName.StartsWith($file.BaseName+'.',[StringComparison]::OrdinalIgnoreCase))
    } | Sort-Object Name)) {
        $parts += "$($sidecar.Name)/$($sidecar.Length)/$($sidecar.LastWriteTimeUtc.Ticks)"
    }
    return ($parts -join '|') + '|' + $ToolRootBox.Text.Trim()
}

function Get-SubtitleImportReport([string]$Path) {
    if (-not $script:ImportReports.ContainsKey($Path)) { return $null }
    $entry = $script:ImportReports[$Path]
    if ($entry.Identity -ne (Get-ImportIdentity $Path)) { return $null }
    return $entry.Report
}

function Start-SubtitleImportCheck {
    if ($script:ImportAutoDeselected) {
        $SubtitleFeatureButton.IsChecked = $true
        $script:ImportAutoDeselected = $false
        Sync-FeatureMode
    }
    $ForceSubtitleTranslationBox.IsChecked = $false
    $script:ImportPaths = @($script:SelectedFiles)
    if ((Get-SelectedTaskMode) -eq 'media_repackage') { $script:ImportPaths=@() }
    $script:ImportTimer.Start()
    Receive-SubtitleImport
}

function Update-SubtitleImportSummary {
    $SubtitleImportPanel.Visibility = $(if ($script:ImportPaths.Count) { 'Visible' } else { 'Collapsed' })
    if (-not $script:ImportPaths.Count) { return }
    $found = 0; $pending = 0; $unknown = 0; $details = @()
    foreach ($path in $script:ImportPaths) {
        $report = Get-SubtitleImportReport $path
        if ($null -eq $report) { $pending++; continue }
        if ($report.status -eq 'detected') { $found++ }
        if ($report.status -eq 'unknown') { $unknown++ }
        $detail = "$([IO.Path]::GetFileName($path))：$($report.message)"
        foreach ($evidence in @($report.evidence)) {
            $sample = [string](Get-JsonProperty $evidence 'text' '')
            if ($sample) { $detail += "`n  $sample" }
        }
        if ($report.warnings.Count) { $detail += "`n  " + ($report.warnings -join '；') }
        $details += $detail
    }
    $ForceSubtitleTranslationBox.Visibility = $(if ($found) { 'Visible' } else { 'Collapsed' })
    $SubtitleImportText.ToolTip = $details -join "`n"
    if ($pending) { $SubtitleImportText.Text = "正在本地检查字幕…剩余 $pending 个文件。"; return }
    if ($script:ImportAutoDeselected -and $found -lt $script:ImportPaths.Count) {
        $script:ImportAutoDeselected=$false
        $SubtitleFeatureButton.IsChecked=$true
        Sync-FeatureMode
    }
    if ($found) {
        $SubtitleImportText.Text = $(if ($ForceSubtitleTranslationBox.IsChecked) { "$found 个文件已有中文字幕；本次仍重新翻译。" } else { "$found 个文件已有中文字幕，将跳过翻译。去马按你的勾选执行。" })
        if ($found -eq $script:ImportPaths.Count -and -not $ForceSubtitleTranslationBox.IsChecked -and $SubtitleFeatureButton.IsChecked) {
            $SubtitleFeatureButton.IsChecked = $false
            $script:ImportAutoDeselected = $true
            Sync-FeatureMode
        }
    } elseif ($unknown -eq $script:ImportPaths.Count) { $SubtitleImportText.Text = '字幕检查未完成，按所选功能处理。' }
    else { $SubtitleImportText.Text = '抽样未检测到中文字幕，按所选功能处理。' }
    if ($unknown) { $SubtitleImportText.Text += " $unknown 个文件检查未完成，保留原选择；详情见提示。" }
}

function Receive-SubtitleImport {
    if ($null -ne $script:ImportBridge) {
        $line = ''
        while ($script:ImportBridge.TryGetOutput([ref]$line)) { [void]$script:ImportLines.Add($line) }
        while ($script:ImportBridge.TryGetError([ref]$line)) { }
        if (-not $script:ImportBridge.HasExited) { return }
        try {
            $script:ImportBridge.FinishReading()
            while ($script:ImportBridge.TryGetOutput([ref]$line)) { [void]$script:ImportLines.Add($line) }
            $report = ($script:ImportLines -join "`n") | ConvertFrom-Json
            if ($script:ImportBridge.ExitCode -ne 0 -or $report.status -notin @('detected','not_detected','not_applicable','unknown')) { throw '检查未完成' }
        } catch { $report = [pscustomobject]@{status='unknown';message='字幕检查未完成，保留原选择。';evidence=@();warnings=@()} }
        finally { $script:ImportBridge.Dispose(); $script:ImportBridge=$null; $script:ImportLines.Clear() }
        $script:ImportReports[$script:ImportCurrentPath] = @{Identity=$script:ImportCurrentIdentity;Report=$report}
    }
    foreach ($path in $script:ImportPaths) {
        if ($null -ne (Get-SubtitleImportReport $path)) { continue }
        $script:ImportCurrentPath = $path
        $script:ImportCurrentIdentity = Get-ImportIdentity $path
        try {
            $script:ImportBridge = New-Object TranslationFlow.ProcessBridge
            $script:ImportBridge.StartSubtitleInspection((Resolve-TranslationPython), $ProjectRoot, $path, $ToolRootBox.Text.Trim())
        } catch {
            if ($null -ne $script:ImportBridge) { $script:ImportBridge.Dispose(); $script:ImportBridge=$null }
            $script:ImportReports[$path] = @{Identity=$script:ImportCurrentIdentity;Report=[pscustomobject]@{status='unknown';message='字幕检查未完成，请检查本地工具设置。';evidence=@();warnings=@()}}
        }
        Update-SubtitleImportSummary
        return
    }
    $script:ImportTimer.Stop()
    if (-not $script:IsRunning -and -not $script:QueueSubmitting -and -not $script:PendingQueueResumeId -and
        -not $script:PendingSrtRequestPath -and $InputVideoBox.Text -in $script:ImportPaths) { Update-SubtitleImportSummary }
}

function Get-ImportedTaskMode([string]$Path, [string]$Mode) {
    if ($ForceSubtitleTranslationBox.IsChecked) { return $Mode }
    $report = Get-SubtitleImportReport $Path
    if ($null -eq $report -or $report.status -ne 'detected') { return $Mode }
    if ($Mode -eq 'translation_picture_final') { return 'picture_only' }
    if ($Mode -in @('translation','srt_translation')) { return 'none' }
    return $Mode
}

function Test-SubtitleImportReady {
    if ($script:RestoringTask -or $script:PendingQueueResumeId -or $script:PendingSrtRequestPath -or (Get-SelectedTaskMode) -eq 'media_repackage') { return $true }
    if ((Get-SelectedTaskMode) -eq 'picture_only' -and -not $script:ImportAutoDeselected) { return $true }
    foreach ($path in $script:ImportPaths) {
        if ($null -eq (Get-SubtitleImportReport $path)) {
            $script:ImportTimer.Start()
            Receive-SubtitleImport
            $InputErrorText.Text = '正在检查已有字幕，完成后再开始。'
            return $false
        }
    }
    if ((Get-SelectedTaskMode) -eq 'none' -and $script:ImportAutoDeselected) {
        $InputErrorText.Text = '已有中文字幕，当前无需处理。需要去马时勾选去马即可。'
        return $false
    }
    return $true
}

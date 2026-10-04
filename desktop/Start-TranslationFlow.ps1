[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [string]$PythonPath = "",
    [string]$ToolRoot = "",
    [string]$ProjectRoot = "",
    [string]$QueueStateDirectory = "",
    [ValidateSet("large-v3", "large-v3-turbo")][string]$WhisperAModel = "large-v3-turbo"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:CurrentWhisperAModel = $WhisperAModel
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

# Machine-specific paths stay in the already ignored local data file.
$localRuntimePath = Join-Path $ProjectRoot "config/local.psd1"
if ([IO.File]::Exists($localRuntimePath)) {
    $runtimeTokens = $null
    $runtimeErrors = $null
    $runtimeText = [IO.File]::ReadAllText($localRuntimePath, [Text.Encoding]::UTF8)
    $runtimeAst = [Management.Automation.Language.Parser]::ParseInput($runtimeText, [ref]$runtimeTokens, [ref]$runtimeErrors)
    if ($runtimeErrors.Count -or $runtimeAst.EndBlock.Statements.Count -ne 1) { throw "本地工具配置必须是单个静态 Hashtable。" }
    $localRuntime = $runtimeAst.EndBlock.Statements[0].PipelineElements[0].Expression.SafeGetValue()
    if ($localRuntime -isnot [hashtable]) { throw "本地工具配置必须是静态 Hashtable。" }
    if (-not $PythonPath -and $localRuntime.ContainsKey("PythonPath")) { $PythonPath = [string]$localRuntime.PythonPath }
    if (-not $ToolRoot -and $localRuntime.ContainsKey("ToolRoot")) { $ToolRoot = [string]$localRuntime.ToolRoot }
    foreach ($runtimeSetting in @{
        JasnaPath="TRANSLATION_FLOW_JASNA";
        AcceptanceGateReport="TRANSLATION_FLOW_ACCEPTANCE_REPORT"
    }.GetEnumerator()) {
        if ($localRuntime.ContainsKey($runtimeSetting.Key) -and
            -not [Environment]::GetEnvironmentVariable($runtimeSetting.Value, "Process")) {
            [Environment]::SetEnvironmentVariable($runtimeSetting.Value, [string]$localRuntime[$runtimeSetting.Key], "Process")
        }
    }
}

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Windows.Forms
. (Join-Path $PSScriptRoot "TranslationFlowProcessBridge.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowUiSummary.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowTaskCenter.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowProvider.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowDesign.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowPresentation.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowQueueUi.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowAccessUi.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowSubtitleImport.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowLibrary.ps1")
. (Join-Path $PSScriptRoot "TranslationFlowBeauty.ps1")

[xml]$Xaml = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "TranslationFlowView.xaml"), [Text.Encoding]::UTF8)
$xamlContext = New-Object Windows.Markup.ParserContext
$xamlContext.BaseUri = [Uri]($PSScriptRoot + [IO.Path]::DirectorySeparatorChar)
$reader = [Xml.XmlReader]::Create([IO.StringReader]::new($Xaml.OuterXml), [Xml.XmlReaderSettings]::new(), $xamlContext.BaseUri.AbsoluteUri)
$Window = [Windows.Markup.XamlReader]::Load($reader)

function Get-Control([string]$Name) {
    $control = $Window.FindName($Name)
    if ($null -eq $control) {
        throw "界面控件缺失：$Name"
    }
    return $control
}

$DropZone = Get-Control "DropZone"
$DropZoneInner = Get-Control "DropZoneInner"
$InputVideoBox = Get-Control "InputVideoBox"
$OutputDirectoryBox = Get-Control "OutputDirectoryBox"
$BrowseVideoButton = Get-Control "BrowseVideoButton"
$BrowseOutputButton = Get-Control "BrowseOutputButton"
$TaskModeBox = Get-Control "TaskModeBox"
$SubtitleFeatureButton = Get-Control "SubtitleFeatureButton"
$PictureFeatureButton = Get-Control "PictureFeatureButton"
$PictureSelectedText = Get-Control "PictureSelectedText"
$SubtitleSelectedText = Get-Control "SubtitleSelectedText"
$SelectedFunctionText = Get-Control "SelectedFunctionText"
$RepackageToolButton = Get-Control "RepackageToolButton"
$ProviderBox = Get-Control "ProviderBox"
$ModelPanel = Get-Control "ModelPanel"
$ModelBox = Get-Control "ModelBox"
$EndpointPanel = Get-Control "EndpointPanel"
$EndpointBox = Get-Control "EndpointBox"
$TokenCapBox = Get-Control "TokenCapBox"
$ApiKeyLabel = Get-Control "ApiKeyLabel"
$PythonPathBox = Get-Control "PythonPathBox"
$ToolRootBox = Get-Control "ToolRootBox"
$PythonPathBox.Text = $PythonPath
$ToolRootBox.Text = $ToolRoot
$ApiKeyPanel = Get-Control "ApiKeyPanel"
$CodexStatusPanel = Get-Control "CodexStatusPanel"
$CodexStatusText = Get-Control "CodexStatusText"
$RefreshCodexStatusButton = Get-Control "RefreshCodexStatusButton"
$LoginCodexButton = Get-Control "LoginCodexButton"
$InstallCodexButton = Get-Control "InstallCodexButton"
$PreparationText = Get-Control "PreparationText"
$StopRunButton = Get-Control "StopRunButton"
$OutputFormatBox = Get-Control "OutputFormatBox"
$OutputFormatPanel = Get-Control "OutputFormatPanel"
$OutputFormatHintText = Get-Control "OutputFormatHintText"
$ProfileBox = Get-Control "ProfileBox"
$WhisperModelPanel = Get-Control "WhisperModelPanel"
$WhisperModelBox = Get-Control "WhisperModelBox"
$WhisperModelBox.SelectedIndex = $(if ($WhisperAModel -eq "large-v3-turbo") { 0 } else { 1 })
$ApiKeyBox = Get-Control "ApiKeyBox"
$TaskModeHintText = Get-Control "TaskModeHintText"
$StartButton = Get-Control "StartButton"
$InputErrorText = Get-Control "InputErrorText"
$TaskCenterScopeText = Get-Control "TaskCenterScopeText"
$RefreshTaskCenterButton = Get-Control "RefreshTaskCenterButton"
$TaskHistoryGrid = Get-Control "TaskHistoryGrid"
$TaskCenterDetailText = Get-Control "TaskCenterDetailText"
$ResumeTaskButton = Get-Control "ResumeTaskButton"
$RunStateText = Get-Control "RunStateText"
$StageText = Get-Control "StageText"
$ProgressText = Get-Control "ProgressText"
$ProgressBar = Get-Control "ProgressBar"
$DurationSummaryText = Get-Control "DurationSummaryText"
$ReuseSummaryText = Get-Control "ReuseSummaryText"
$RecoverySummaryText = Get-Control "RecoverySummaryText"
$WarningPanel = Get-Control "WarningPanel"
$WarningList = Get-Control "WarningList"
$EventLog = Get-Control "EventLog"
$SubtitleResultText = Get-Control "SubtitleResultText"
$MkvResultText = Get-Control "MkvResultText"
$ReportResultText = Get-Control "ReportResultText"
$OpenOutputButton = Get-Control "OpenOutputButton"
$ConsentOverlay = Get-Control "ConsentOverlay"
$CancelConsentButton = Get-Control "CancelConsentButton"
$ConfirmConsentButton = Get-Control "ConfirmConsentButton"
$ConsentDetailText = Get-Control "ConsentDetailText"
$SetupPanel = Get-Control "SetupPanel"
$SetupTitle = Get-Control "SetupTitle"
$ProviderPanel = Get-Control "ProviderPanel"
$ProfilePanel = Get-Control "ProfilePanel"
$RunPanel = Get-Control "RunPanel"
$TaskCenterPanel = Get-Control "TaskCenterPanel"
$MainScroll = Get-Control "MainScroll"
$MoreSettings = Get-Control "MoreSettings"
$DropTitle = Get-Control "DropTitle"
$SelectedFilesText = Get-Control "SelectedFilesText"
$RunSourceText = Get-Control "RunSourceText"
$ElapsedText = Get-Control "ElapsedText"
$EtaText = Get-Control "EtaText"
$ResultFilesPanel = Get-Control "ResultFilesPanel"
$RunDetails = Get-Control "RunDetails"
$RecognizeStep = Get-Control "RecognizeStep"
$TranslateStep = Get-Control "TranslateStep"
$ExportStep = Get-Control "ExportStep"
$RecognizeDot = Get-Control "RecognizeDot"
$TranslateDot = Get-Control "TranslateDot"
$ExportDot = Get-Control "ExportDot"
$RecognizeMark = Get-Control "RecognizeMark"
$TranslateMark = Get-Control "TranslateMark"
$ExportMark = Get-Control "ExportMark"
$NewTaskButton = Get-Control "NewTaskButton"
$ShowTasksButton = Get-Control "ShowTasksButton"
$ShowLibraryButton = Get-Control "ShowLibraryButton"
$BackFromTasksButton = Get-Control "BackFromTasksButton"
$RetryRunButton = Get-Control "RetryRunButton"
$BackgroundButton = Get-Control "BackgroundButton"
$OpenResultButton = Get-Control "OpenResultButton"
$script:ProviderId = "codex"
$script:ProviderHost = "chatgpt.com"
$script:ProviderModel = "cli-default"
$script:DeepSeekModel = "deepseek-v4-pro"

if ($ValidateOnly) {
    [pscustomobject]@{
        status = "valid"
        entrypoint = $PSCommandPath
        bridge = (Join-Path $PSScriptRoot "TranslationFlowProcessBridge.ps1")
        protocol_schema = 1
        provider_model = $script:ProviderModel
        default_provider = $script:ProviderId
        default_asr_a_model = $script:CurrentWhisperAModel
        asr_a_models = @("large-v3-turbo", "large-v3")
        codex_original_picture_only = $false
        backends = @("translation_flow.cli", "full_workflow.cli", "full_workflow.picture_cli", "media_planning.cli", "translation_flow.srt_desktop")
        subtitle_providers = @("codex","deepseek","qwen","kimi","openai","anthropic","gemini")
        ui_summary_contract = 1
        task_center_contract = 1
        mp4_delivery_contract = 1
    } | ConvertTo-Json -Compress
    return
}

$script:Bridge = $null
$script:PollTimer = $null
$script:IsRunning = $false
$script:TaskCenterCompletingExitedRun = $false
$script:DrainingBackendQueues = $false
$script:Finalized = $false
$script:LastSeq = 0
$script:CurrentJobId = ""
$script:CompletedEvent = $null
$script:FailedEvent = $null
$script:CancelledEvent = $null
$script:CurrentRequestPath = ""
$script:CurrentProviderId = ""
$script:CodexStatusBridge = $null
$script:CodexStatusTimer = $null
$script:CodexStatus = "checking"
$script:CodexStatusMessage = '正在检查本机 Codex…'
$script:AccessBridge = $null
$script:AccessTimer = $null
$script:AccessAction = ''
$script:AccessResumeIntent = $false
$script:AccessLines = New-Object System.Collections.ArrayList
$script:RestoringTask = $false
$script:PendingSrtRequestPath = ""
$script:DisplayedProvider = ""
$script:ProviderDrafts = @{}
$script:CodexStatusLines = New-Object System.Collections.ArrayList
$script:ProtocolFailure = ""
$script:FinalMediaPath = ""
$script:CurrentWorkflowMode = ""
$script:SelectedOutputDirectory = ""
$script:JobStartedAt = $null
$script:StageStartedAt = @{}
$script:StageDurations = @{}
$script:StageDurationOrder = New-Object System.Collections.ArrayList
$script:ObservedResumes = @{}
$script:LastActiveStage = ""
$script:UiPage = 'setup'
$script:CurrentProgress = 0.0
$script:EtaObservation = $null
$script:EtaRange = ''
$script:RunEndedAt = $null
$script:RunPresentationState = 'idle'
$OutputDirectoryBox.Text = Join-Path $ProjectRoot 'deliverables'

function Convert-ToBrush([string]$Color) {
    return (New-Object System.Windows.Media.BrushConverter).ConvertFromString($Color)
}

function Get-JsonProperty($Value, [string]$Name, $Default = $null) {
    if ($null -eq $Value) { return $Default }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Add-EventLog([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($Message)) { return }
    $stamp = [DateTime]::Now.ToString("HH:mm:ss")
    $EventLog.AppendText("[$stamp] $Message`r`n")
    $EventLog.ScrollToEnd()
}

function Add-Warning([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($Message)) { return }
    $WarningList.Items.Add($Message) | Out-Null
    $WarningPanel.Visibility = [Windows.Visibility]::Visible
    Add-EventLog "警告：$Message"
}

function Set-ProgressValue([double]$Value) {
    $bounded = [Math]::Max(0, [Math]::Min(100, $Value))
    $bounded = [Math]::Max($script:CurrentProgress, $bounded)
    $script:CurrentProgress = $bounded
    $animation = New-Object Windows.Media.Animation.DoubleAnimation
    $animation.To = $bounded
    $animation.Duration = [Windows.Duration]::new([TimeSpan]::FromMilliseconds(240))
    $animation.EasingFunction = New-Object Windows.Media.Animation.QuadraticEase
    $ProgressBar.BeginAnimation([Windows.Controls.Primitives.RangeBase]::ValueProperty, $animation)
    Update-ProgressPresentation
}

function Reset-RunSummary {
    $script:JobStartedAt = $null
    $script:StageStartedAt = @{}
    $script:StageDurations = @{}
    $script:StageDurationOrder = New-Object System.Collections.ArrayList
    $script:ObservedResumes = @{}
    $script:LastActiveStage = ""
    $DurationSummaryText.Text = "总耗时与阶段用时会在运行过程中自动汇总。"
    $ReuseSummaryText.Text = "检查点复用情况会在后端报告后显示。"
    $RecoverySummaryText.Text = "若任务失败，这里会说明失败阶段、已保留成果和恢复方式。"
    $RecoverySummaryText.Foreground = Convert-ToBrush "#778599"
}

function Register-StageStart($Event, [string]$Stage) {
    if ([string]::IsNullOrWhiteSpace($Stage)) { return }
    $startedAt = ConvertTo-UiEventTime $Event
    if ($null -eq $startedAt) { $startedAt = [DateTimeOffset]::UtcNow }
    $script:StageStartedAt[$Stage] = $startedAt
    $script:LastActiveStage = $Stage
}

function Register-StageEnd($Event, [string]$Stage) {
    if ([string]::IsNullOrWhiteSpace($Stage)) { return }
    $endedAt = ConvertTo-UiEventTime $Event
    if ($null -eq $endedAt) { $endedAt = [DateTimeOffset]::UtcNow }
    if ($script:StageStartedAt.ContainsKey($Stage)) {
        $seconds = ($endedAt - $script:StageStartedAt[$Stage]).TotalSeconds
        if ($seconds -ge 0) {
            $script:StageDurations[$Stage] = $seconds
            if (-not $script:StageDurationOrder.Contains($Stage)) {
                [void]$script:StageDurationOrder.Add($Stage)
            }
        }
        [void]$script:StageStartedAt.Remove($Stage)
    }
    $resumed = Get-JsonProperty $Event "resumed" $false
    if ($resumed -eq $true) { $script:ObservedResumes[$Stage] = $true }
    $reused = Get-JsonProperty $Event "reused" $false
    if ($reused -eq $true) { $script:ObservedResumes[$Stage] = $true }
    $script:LastActiveStage = $Stage
}

function Update-DurationSummary($EndEvent = $null) {
    $endTime = $(if ($null -eq $EndEvent) { [DateTimeOffset]::UtcNow } else { ConvertTo-UiEventTime $EndEvent })
    if ($null -eq $endTime) { $endTime = [DateTimeOffset]::UtcNow }
    $totalText = "总耗时：等待后端时间证据"
    if ($null -ne $script:JobStartedAt) {
        $totalSeconds = ($endTime - $script:JobStartedAt).TotalSeconds
        if ($totalSeconds -ge 0) { $totalText = "总耗时：$(Format-UiDuration $totalSeconds)" }
    }
    $stageParts = @()
    foreach ($stageName in $script:StageDurationOrder) {
        $stageParts += "$(Get-StageLabel $stageName) $(Format-UiDuration ([double]$script:StageDurations[$stageName]))"
    }
    if ($stageParts.Count -gt 0) {
        $DurationSummaryText.Text = "$totalText`n已完成阶段：$($stageParts -join '；')"
    }
    else {
        $DurationSummaryText.Text = "$totalText；阶段完成后会逐项列出。"
    }
    if ($script:IsRunning -and $script:StageStartedAt.ContainsKey($script:LastActiveStage)) {
        $activeSeconds = [Math]::Max(0, ($endTime - $script:StageStartedAt[$script:LastActiveStage]).TotalSeconds)
        $DurationSummaryText.Text += "`n当前：$(Get-StageLabel $script:LastActiveStage)，已用 $(Format-UiDuration $activeSeconds)"
    }
}

function Update-ReuseSummary($CompletedEvent = $null) {
    $items = @(Get-UiReuseItems $CompletedEvent $script:ObservedResumes)
    if ($items.Count -eq 0) {
        $ReuseSummaryText.Text = "检查点复用：本次尚未报告复用；新任务会按正常阶段执行。"
        return
    }
    $parts = @()
    foreach ($item in $items) {
        $state = $(if ($item.reused -eq $true) { "已复用" } else { "本次新运行" })
        $displayStage = switch ([string]$item.stage) {
            "translation" { "translation_flow" }
            "picture" { "picture_workflow" }
            default { [string]$item.stage }
        }
        $parts += "$(Get-StageLabel $displayStage)：$state"
    }
    $ReuseSummaryText.Text = "检查点复用：$($parts -join '；')"
}

function Set-FailureSummary([string]$RawMessage) {
    $stageLabel = Get-StageLabel $script:LastActiveStage
    $guidance = Get-UiFailureGuidance $RawMessage $script:LastActiveStage $stageLabel
    $RunStateText.Text = $guidance.title
    $StageText.Text = $guidance.summary
    $RecoverySummaryText.Text = $guidance.next_step
    $RecoverySummaryText.Foreground = Convert-ToBrush "#778599"
}

function Get-SelectedTaskMode {
    if ($null -eq $TaskModeBox.SelectedItem) { return "" }
    return [string]$TaskModeBox.SelectedItem.Tag
}

function Get-SelectedOutputTarget {
    if ($null -eq $OutputFormatBox.SelectedItem) { return "" }
    return [string]$OutputFormatBox.SelectedItem.Tag
}

function Select-ComboBoxItemByTag($ComboBox, [string]$Tag) {
    foreach ($item in $ComboBox.Items) {
        if ([string]$item.Tag -eq $Tag) {
            $ComboBox.SelectedItem = $item
            return
        }
    }
    throw "界面不支持原任务设置：$Tag"
}

function Update-ProviderFields {
    $id = [string]$ProviderBox.SelectedItem.Tag
    if ($id -eq $script:DisplayedProvider) { return }
    if ($script:DisplayedProvider) {
        $script:ProviderDrafts[$script:DisplayedProvider] = @{
            Model=$ModelBox.Text; Cap=$TokenCapBox.Text;
            Endpoint=$(if ($null -ne $EndpointBox.SelectedItem) { [string]$EndpointBox.SelectedItem.Tag } else { "" })
        }
    }
    $script:DisplayedProvider = $id
    $draft = $(if ($script:ProviderDrafts.ContainsKey($id)) { $script:ProviderDrafts[$id] }
        else { @{Model=$(if ($id -eq "deepseek") { $script:DeepSeekModel } elseif ($id -eq "codex") { "cli-default" } else { "" }); Cap="8192"; Endpoint=""} })
    $ModelBox.Text = $draft.Model
    $TokenCapBox.Text = $draft.Cap
    $EndpointBox.ItemsSource = @(Get-TranslationEndpoints $id)
    if ($EndpointBox.Items.Count -gt 0) {
        $EndpointBox.SelectedIndex = 0
        if ($draft.Endpoint) { Select-ComboBoxItemByTag $EndpointBox $draft.Endpoint }
    }
}

function Get-SelectedProviderSettings {
    $id = [string]$ProviderBox.SelectedItem.Tag
    $endpoint = $(if ($null -ne $EndpointBox.SelectedItem) { [string]$EndpointBox.SelectedItem.Tag } else { "" })
    $cap = 8192
    if ($id -notin @("codex", "deepseek") -and
        (-not [int]::TryParse($TokenCapBox.Text, [ref]$cap) -or $cap -le 0)) {
        throw "输出 token 上限必须为正整数。"
    }
    return Get-TranslationProviderSettings $id $ModelBox.Text $endpoint $cap
}

function Update-TaskModeControls {
    $selectedMode = Get-SelectedTaskMode
    $mediaMode = $selectedMode -eq "media_repackage"
    $fullWorkflow = $selectedMode -eq "translation_picture_final"
    $srtMode = $selectedMode -eq "srt_translation"
    $localOnly = $mediaMode -or $selectedMode -in @("picture_only", "none")
    Update-ProviderFields
    $script:ProviderId = [string]$ProviderBox.SelectedItem.Tag
    $codexMode = -not $localOnly -and $script:ProviderId -eq "codex"
    $ProviderBox.IsEnabled = -not $localOnly
    $OutputFormatBox.IsEnabled = -not $srtMode
    $OutputFormatPanel.Visibility = $(if ($srtMode) { "Collapsed" } else { "Visible" })
    $ProfileBox.IsEnabled = -not $localOnly
    $WhisperModelPanel.Visibility = $(if ($selectedMode -in @('translation', 'translation_picture_final')) { 'Visible' } else { 'Collapsed' })
    $ApiKeyBox.IsEnabled = -not $localOnly -and -not $codexMode
    $ApiKeyPanel.Visibility = $(if (-not $localOnly -and -not $codexMode) { "Visible" } else { "Collapsed" })
    $ModelPanel.Visibility = $ApiKeyPanel.Visibility
    $EndpointPanel.Visibility = $(if ($script:ProviderId -eq "deepseek") { "Collapsed" } else { "Visible" })
    $ApiKeyLabel.Text = "API Key"
    $ProviderPanel.Visibility = $(if ($localOnly) { 'Collapsed' } else { 'Visible' })
    $ProfilePanel.Visibility = $ProviderPanel.Visibility
    $Window.FindName('ProviderSettingsButton').Visibility = $ProviderPanel.Visibility
    $SetupTitle.Text = $(if ($mediaMode) { '视频换个格式' } else { '准备好，就开始' })
    $SubtitleFeatureButton.IsChecked = $selectedMode -in @('translation','srt_translation','translation_picture_final')
    $PictureFeatureButton.IsChecked = $selectedMode -in @('picture_only','translation_picture_final')
    foreach ($card in @($SubtitleFeatureButton, $PictureFeatureButton)) {
        $card.Background = Convert-ToBrush $(if ($card.IsChecked) { '#ECFAF9' } else { '#FFFFFF' })
        $card.BorderBrush = Convert-ToBrush $(if ($card.IsChecked) { '#00A5A2' } else { '#E3E9EF' })
    }
    $SubtitleSelectedText.Text = $(if ($SubtitleFeatureButton.IsChecked) { '✓ 已选择' } else { '未选择' })
    $PictureSelectedText.Text = $(if ($PictureFeatureButton.IsChecked) { '✓ 已选择' } else { '未选择' })
    $SubtitleSelectedText.Foreground = Convert-ToBrush $(if ($SubtitleFeatureButton.IsChecked) { '#00A5A2' } else { '#778599' })
    $PictureSelectedText.Foreground = Convert-ToBrush $(if ($PictureFeatureButton.IsChecked) { '#00A5A2' } else { '#778599' })
    $SelectedFunctionText.Text = $(switch ($selectedMode) {
        'media_repackage' { '当前工具：已有字幕视频换格式。' }
        'picture_only' { '去马后保存视频，保留原声音和已有字幕。' }
        'translation_picture_final' { '识别对白、精修中文字幕并去马，自动保存成品。' }
        'srt_translation' { '已识别字幕文件：直接翻译并润色，另存中文字幕。' }
        'none' { '请至少选择一个功能，也可以同时选择两项。' }
        default { if ([IO.File]::Exists($InputVideoBox.Text)) { '已识别视频文件：自动识别对白、翻译并润色。' } else { '放入视频或 SRT，自动识别对白或读取字幕，翻译并润色。' } }
    })
    if ([string]::IsNullOrWhiteSpace($InputVideoBox.Text)) {
        $DropTitle.Text = $(if ($mediaMode) { '把已有字幕的视频拖到这里' } else { '把视频或字幕文件拖到这里' })
    }
    $StartButton.Content = '开始处理'
    $OutputFormatBox.Items[1].Content = 'MP4'
    $BrowseVideoButton.Content = "选择视频"
    $CodexStatusPanel.Visibility = $(if ($codexMode) { "Visible" } else { "Collapsed" })
    $RefreshCodexStatusButton.IsEnabled = $codexMode -and $null -eq $script:CodexStatusBridge
    if ($srtMode) {
        $OutputFormatHintText.Text = "另存中文字幕和检查表，保留字幕时间。"
        $TaskModeHintText.Text = "直接翻译并润色已有字幕，无需重新识别对白。"
    }
    elseif ($selectedMode -eq "picture_only") {
        $OutputFormatHintText.Text = "去马成品保留原声音和已有字幕。"
        $TaskModeHintText.Text = "按所选格式保存去马后的视频。"
    }
    elseif ($mediaMode) {
        $StartButton.Content = "开始换格式"
        $OutputFormatHintText.Text = "视频和声音保持原样，只更换文件格式。"
        $TaskModeHintText.Text = "需要视频内已有可开关的中文字幕；不重新翻译。"
    }
    else {
        if ($fullWorkflow) {
            $OutputFormatHintText.Text = "按所选格式保存字幕与去马成品，原声音保留。"
            $TaskModeHintText.Text = "自动精修中文字幕并去马，保留原声音。"
        }
        else {
            $OutputFormatHintText.Text = "按所选格式保存视频，中文字幕可在播放器中开关。"
            $TaskModeHintText.Text = "保留原视频和声音，另存带中文字幕的视频。"
        }
    }
}

function Get-TaskCenterOutputDirectory {
    if (-not [string]::IsNullOrWhiteSpace($OutputDirectoryBox.Text) -and
        [IO.Directory]::Exists($OutputDirectoryBox.Text)) {
        return [IO.Path]::GetFullPath($OutputDirectoryBox.Text)
    }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot "deliverables"))
}

function Complete-ExitedRunForTaskCenter {
    if ($script:TaskCenterCompletingExitedRun -or $script:DrainingBackendQueues -or -not $script:IsRunning -or
        $script:Finalized -or $null -eq $script:Bridge) {
        return $false
    }
    $script:TaskCenterCompletingExitedRun = $true
    try {
        if (-not $script:Bridge.HasExited) { return $false }
        $script:Bridge.FinishReading()
        Drain-BackendQueues
        Finalize-ExitedProcess
        return $true
    }
    catch {
        Add-EventLog "任务中心检查后端退出状态失败：$($_.Exception.Message)"
        return $false
    }
    finally {
        $script:TaskCenterCompletingExitedRun = $false
    }
}

function Refresh-TaskCenter {
    if (Complete-ExitedRunForTaskCenter) { return }
    $previousRequestPath = $(if ($null -ne $TaskHistoryGrid.SelectedItem) {
        [string]$TaskHistoryGrid.SelectedItem.RequestPath
    } else { "" })
    $scope = Get-TaskCenterOutputDirectory
    $TaskCenterScopeText.Text = "任务记录目录：$scope"
    $entries = @(Get-TaskCenterEntries $scope)
    if ($script:IsRunning -and -not [string]::IsNullOrWhiteSpace($script:CurrentJobId)) {
        foreach ($entry in $entries) {
            if ($entry.JobId -eq $script:CurrentJobId) {
                $entry.StatusLabel = "运行中"
                $entry.CanResume = $false
            }
        }
    }
    $TaskHistoryGrid.ItemsSource = $entries
    Update-DesignTaskCards
    Update-DesignRecent $entries
    $selection = Get-TaskCenterPreferredEntry $entries $previousRequestPath
    $TaskHistoryGrid.SelectedItem = $selection
    if ($null -eq $selection) {
        $TaskCenterDetailText.Text = "当前输出目录没有可显示的正式任务。"
        $ResumeTaskButton.IsEnabled = $false
    }
    else {
        Show-SelectedTaskDetail
    }
}

function Show-SelectedTaskDetail {
    if (Complete-ExitedRunForTaskCenter) { return }
    $item = $TaskHistoryGrid.SelectedItem
    if ($null -eq $item) {
        $TaskCenterDetailText.Text = "选择任务后可查看阶段、失败原因和产物。"
        $ResumeTaskButton.IsEnabled = $false
        return
    }
    $parts = @("任务：$($item.JobId)", "状态：$($item.StatusLabel)")
    if (-not [string]::IsNullOrWhiteSpace([string]$item.ProviderLabel)) {
        $parts += "翻译方式：$($item.ProviderLabel)"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$item.StageLabel)) {
        $parts += "最近阶段：$($item.StageLabel)"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$item.FailureText)) {
        $parts += "原因：$($item.FailureText)"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$item.OutputsText)) {
        $parts += "产物：$($item.OutputsText)"
    }
    $TaskCenterDetailText.Text = $parts -join "`n"
    $ResumeTaskButton.IsEnabled = (-not $script:IsRunning) -and ($item.CanResume -eq $true)
}

function Set-InputEnabled([bool]$Enabled) {
    if ($null -ne $script:AccessBridge) { $Enabled=$false }
    $LoginCodexButton.IsEnabled=$Enabled -and $script:CodexStatus -ne 'checking'
    $InstallCodexButton.IsEnabled=$Enabled
    $ModelBox.IsEnabled = $Enabled
    $EndpointBox.IsEnabled = $Enabled
    $TokenCapBox.IsEnabled = $Enabled
    $PythonPathBox.IsEnabled = $Enabled
    $ToolRootBox.IsEnabled = $Enabled
    $BrowseVideoButton.IsEnabled = $Enabled
    $BrowseOutputButton.IsEnabled = $Enabled
    $TaskModeBox.IsEnabled = $Enabled
    $SubtitleFeatureButton.IsEnabled = $Enabled
    $PictureFeatureButton.IsEnabled = $Enabled
    $SubtitleFeatureButton.IsEnabled = $Enabled
    $RepackageToolButton.IsEnabled = $Enabled
    $ProviderBox.IsEnabled = $Enabled
    $WhisperModelBox.IsEnabled = $Enabled
    $StartButton.IsEnabled = $Enabled -and [IO.File]::Exists($InputVideoBox.Text) -and -not $script:QueueSubmitting
    $RefreshTaskCenterButton.IsEnabled = $true
    $ResumeTaskButton.IsEnabled = $Enabled -and $null -ne $TaskHistoryGrid.SelectedItem -and
        $TaskHistoryGrid.SelectedItem.CanResume -eq $true
    $DropZone.AllowDrop = $Enabled
    if ($Enabled) {
        Update-TaskModeControls
    }
    else {
        $OutputFormatBox.IsEnabled = $false
        $ProfileBox.IsEnabled = $false
        $ApiKeyBox.IsEnabled = $false
        $RefreshCodexStatusButton.IsEnabled = $false
    }
}

function Set-SelectedVideo([string]$Path) {
    if (-not [IO.File]::Exists($Path)) {
        $InputErrorText.Text = "选择的文件不存在。"
        return
    }
    $mediaMode = (Get-SelectedTaskMode) -eq 'media_repackage'
    if ($mediaMode -and [IO.Path]::GetExtension($Path) -ieq '.srt') {
        $InputErrorText.Text = '换格式需要视频文件；如需翻译字幕，请选择“精修中文字幕”。'
        return
    }
    $InputVideoBox.Text = [IO.Path]::GetFullPath($Path)
    if (-not $mediaMode) { Sync-FeatureMode }
    $InputErrorText.Text = ""
}

function Test-RunInputs {
    $resumeError = Test-QueueResumeSettings
    if ($resumeError) { return $resumeError }
    $hasSessionKey = Test-QueueSessionKeyAvailable
    if (-not [IO.File]::Exists($InputVideoBox.Text)) {
        return "请先选择存在的源文件。"
    }
    if ([string]::IsNullOrWhiteSpace($OutputDirectoryBox.Text)) {
        return "请选择输出目录。"
    }
    $selectedMode = Get-SelectedTaskMode
    if ($selectedMode -eq 'none') { return '请至少选择一个功能。' }
    if ($selectedMode -in @('picture_only','translation_picture_final') -and
        @($script:SelectedFiles + @($InputVideoBox.Text) | Where-Object { [IO.Path]::GetExtension($_) -ieq '.srt' }).Count) {
        return 'SRT 只有字幕，不能去马。请取消去马，或从本批移出 SRT 后处理视频。'
    }
    $runMediaRepackage = $selectedMode -in @("media_repackage", "picture_only")
    if ($runMediaRepackage) {
        $target = $OutputFormatBox.SelectedItem
        if ($null -eq $target -or @("mkv", "mp4") -notcontains [string]$target.Tag) {
            return "请选择有效的输出容器。"
        }
        return ""
    }
    $runFullWorkflow = (Get-SelectedTaskMode) -eq "translation_picture_final"
    if ((Get-SelectedTaskMode) -eq "srt_translation" -and [IO.Path]::GetExtension($InputVideoBox.Text) -ine ".srt") {
        return "字幕模式需要 SRT 文件。"
    }
    try {
        $settings = Get-SelectedProviderSettings
        $script:ProviderId = $settings.Id
        $script:ProviderHost = $settings.Host
        $script:ProviderModel = $settings.Model
    }
    catch { return $_.Exception.Message }
    if ($script:QueueEnabled -and $script:PendingQueueResumeId -and $settings.Id -ne 'codex' -and -not $hasSessionKey -and [string]::IsNullOrWhiteSpace($ApiKeyBox.Password)) {
        return '继续这个任务前，请填写本次 API Key。'
    }
    Update-TaskModeControls
    if ($script:ProviderId -eq "codex" -and $script:CodexStatus -ne "available" -and -not $script:RestoringTask) {
        return $script:CodexStatusMessage
    }
    if ($script:ProviderId -eq "deepseek" -and [string]::IsNullOrWhiteSpace($ApiKeyBox.Password) -and -not $hasSessionKey -and -not $runFullWorkflow) {
        if (-not $script:RestoringTask -or (Get-SelectedTaskMode) -ne "srt_translation") { return "请输入本次使用的 DeepSeek API Key。" }
    }
    if ($script:ProviderId -notin @("codex", "deepseek") -and
        [string]::IsNullOrWhiteSpace($ApiKeyBox.Password) -and -not $hasSessionKey -and -not $script:RestoringTask) {
        return "请输入本次所选服务的 API Key。"
    }
    $profile = $ProfileBox.SelectedItem
    if ($null -eq $profile -or @("normal", "adult_direct") -notcontains [string]$profile.Tag) {
        return "请选择有效的翻译模式。"
    }
    return ""
}

function Resolve-TranslationPython {
    $PythonPath = $PythonPathBox.Text.Trim()
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        if ([IO.File]::Exists($PythonPath)) { return [IO.Path]::GetFullPath($PythonPath) }
        $explicitCommand = Get-Command $PythonPath -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $explicitCommand) { return $explicitCommand.Source }
        throw "指定的 Python 不存在：$PythonPath"
    }
    $projectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ([IO.File]::Exists($projectPython)) { return $projectPython }
    $bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if ([IO.File]::Exists($bundledPython)) { return $bundledPython }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pythonCommand) { return $pythonCommand.Source }
    throw "未找到 Python。请使用 -PythonPath 指定本地 python.exe。"
}

function Start-CodexStatusCheck {
    if ($script:IsRunning -or $null -ne $script:CodexStatusBridge -or $null -ne $script:AccessBridge) { return }
    $script:CodexStatus = "checking"
    $CodexStatusText.Text = "正在检查本机 Codex 登录状态…"
    $RefreshCodexStatusButton.IsEnabled = $false
    $script:CodexStatusLines.Clear()
    try {
        $script:CodexStatusBridge = New-Object TranslationFlow.ProcessBridge
        $script:CodexStatusBridge.StartCodexStatus((Resolve-TranslationPython), $ProjectRoot)
    }
    catch {
        if ($null -ne $script:CodexStatusBridge) { $script:CodexStatusBridge.Dispose() }
        $script:CodexStatusBridge = $null
        $script:CodexStatus = "unavailable"
        $CodexStatusText.Text = "无法检查 Codex：$($_.Exception.Message)"
        $RefreshCodexStatusButton.IsEnabled = $true
        return
    }
    $script:CodexStatusTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:CodexStatusTimer.Interval = [TimeSpan]::FromMilliseconds(200)
    $script:CodexStatusTimer.Add_Tick({
        if ($null -eq $script:CodexStatusBridge) { return }
        $line = ""
        while ($script:CodexStatusBridge.TryGetOutput([ref]$line)) {
            [void]$script:CodexStatusLines.Add($line)
        }
        while ($script:CodexStatusBridge.TryGetError([ref]$line)) { }
        if (-not $script:CodexStatusBridge.HasExited) { return }
        $script:CodexStatusBridge.FinishReading()
        while ($script:CodexStatusBridge.TryGetOutput([ref]$line)) {
            [void]$script:CodexStatusLines.Add($line)
        }
        try {
            $status = ($script:CodexStatusLines -join "`n") | ConvertFrom-Json
            $state = [string](Get-JsonProperty $status "status" "unavailable")
            if ($state -notin @("available", "login_required", "unavailable", "api_login_not_allowed") -or
                $script:CodexStatusBridge.ExitCode -ne 0) { throw "状态响应无效" }
            $script:CodexStatus = $state
            $message = [string](Get-JsonProperty $status "message" "")
            if ($state -eq "available") {
                $CodexStatusText.Text = "已登录 ChatGPT · 无需 Key。开始前会测试服务响应。"
                $CodexStatusText.ToolTip = $message
            }
            else {
                $CodexStatusText.Text = $message
            }
            $script:CodexStatusMessage=$message
            $reason=[string](Get-JsonProperty $status 'reason' '')
            $LoginCodexButton.Visibility=$(if ($state -eq 'available' -or $reason -eq 'cli_not_found') { 'Collapsed' } else { 'Visible' })
            $InstallCodexButton.Visibility=$(if ($reason -eq 'cli_not_found') { 'Visible' } else { 'Collapsed' })
        }
        catch {
            $script:CodexStatus = "unavailable"
            $CodexStatusText.Text = "检测未完成，不能据此判断未登录。请点击重新检测。"
            $script:CodexStatusMessage=$CodexStatusText.Text
        }
        finally {
            $script:CodexStatusTimer.Stop()
            $script:CodexStatusBridge.Dispose()
            $script:CodexStatusBridge = $null
            $RefreshCodexStatusButton.IsEnabled = -not $script:IsRunning
        }
    })
    $script:CodexStatusTimer.Start()
}

function Get-EventMessage($Event) {
    foreach ($name in @("message", "detail", "warning", "error")) {
        $value = Get-JsonProperty $Event $name ""
        if (-not [string]::IsNullOrWhiteSpace([string]$value)) { return [string]$value }
    }
    return ""
}

function Get-StageLabel([string]$Stage) {
    $labels = @{
        "connection_preflight" = "翻译服务检查"
        "preflight" = "任务预检"
        "media_input" = "媒体输入检查"
        "codex_auth" = "Codex 登录检查"
        "asr_review" = "识别疑点复核"
        "source_review" = "识别疑点复核"
        "whisper_a" = "正在识别对白（Whisper A）"
        "asr_a" = "正在识别对白（Whisper A）"
        "gpu_release_after_a" = "正在准备下一步"
        "whisper_c" = "正在核对识别结果（Whisper C）"
        "asr_c" = "正在核对识别结果（Whisper C）"
        "gpu_release_after_c" = "正在整理识别结果"
        "asr_fusion" = "正在整理字幕"
        "context" = "正在理解内容"
        "translation" = "正在翻译"
        "translate_initial" = "正在翻译"
        "polish" = "正在润色"
        "kana_repair" = "正在检查漏译"
        "subtitle_validation" = "正在检查字幕"
        "packaging" = "正在导出视频"
        "mkv_packaging" = "正在导出视频"
        "technical_validation" = "正在检查输出文件"
        "translation_flow" = "翻译主线"
        "picture_workflow" = "整片画面处理"
        "parallel_branches" = "翻译与画面并行分支"
        "final_mux" = "最终薄汇合"
        "mp4_delivery" = "正在导出 MP4"
        "media_probe" = "正在读取视频信息"
        "compatibility_preflight" = "正在检查格式兼容性"
        "media_repackage" = "正在导出视频"
    }
    if ($labels.ContainsKey($Stage)) { return $labels[$Stage] }
    if ([string]::IsNullOrWhiteSpace($Stage)) { return "正在处理" }
    return $Stage
}

function Get-StableJobId(
    [string]$InputVideo,
    [string]$OutputDirectory,
    [string]$ProfileId,
    [string]$ProviderId,
    [string]$ProviderHost,
    [string]$ProviderModel,
    [string]$ProviderVariant = "",
    [string]$AsrAModel = "large-v3"
) {
    $sourceInfo = [IO.FileInfo]::new([IO.Path]::GetFullPath($InputVideo))
    $identity = @(
        $sourceInfo.FullName.ToUpperInvariant(),
        $sourceInfo.Length,
        $sourceInfo.LastWriteTimeUtc.Ticks,
        ([IO.Path]::GetFullPath($OutputDirectory)).ToUpperInvariant(),
        $ProfileId,
        $ProviderId,
        $ProviderHost,
        $ProviderModel
    ) -join "`n"
    if ($ProviderVariant) { $identity += "`n$ProviderVariant" }
    if ($AsrAModel -ne "large-v3") { $identity += "`nasr-a=$AsrAModel" }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))
    }
    finally { $sha.Dispose() }
    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return "job-$($hex.Substring(0, 32))"
}

function Get-StableMediaJobId(
    [string]$InputMedia,
    [string]$OutputDirectory,
    [string]$OutputTarget
) {
    $sourceInfo = [IO.FileInfo]::new([IO.Path]::GetFullPath($InputMedia))
    $identity = @(
        $sourceInfo.FullName.ToUpperInvariant(),
        $sourceInfo.Length,
        $sourceInfo.LastWriteTimeUtc.Ticks,
        ([IO.Path]::GetFullPath($OutputDirectory)).ToUpperInvariant(),
        "media_repackage",
        $OutputTarget
    ) -join "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))
    }
    finally { $sha.Dispose() }
    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return "media-$($hex.Substring(0, 32))"
}

function Handle-BackendEvent($Event) {
    $schemaVersion = [int](Get-JsonProperty $Event "schema_version" 0)
    $jobId = [string](Get-JsonProperty $Event "job_id" "")
    $seq = [int](Get-JsonProperty $Event "seq" 0)
    if ($schemaVersion -ne 1 -or $jobId -ne $script:CurrentJobId -or $seq -le $script:LastSeq) {
        throw "后端事件协议无效（schema/job_id/seq）。"
    }
    $script:LastSeq = $seq
    $eventName = [string](Get-JsonProperty $Event "event" "")
    $message = Get-EventMessage $Event
    $stage = [string](Get-JsonProperty $Event "stage" "")
    $stageLabel = Get-StageLabel $stage

    switch ($eventName) {
        "job_started" {
            $StopRunButton.IsEnabled = $script:CurrentWorkflowMode -in @("translation", "srt_translation")
            $script:JobStartedAt = ConvertTo-UiEventTime $Event
            if ($null -eq $script:JobStartedAt) { $script:JobStartedAt = [DateTimeOffset]::UtcNow }
            $RunStateText.Text = "任务已启动"
            $StageText.Text = $(if ($message) { $message } else { "后端已接收正式任务。" })
            Update-DurationSummary $Event
        }
        "stage_started" {
            Register-StageStart $Event $stage
            $RunStateText.Text = $stageLabel
            $StageText.Text = $(if ($message) { $message } else { "阶段开始。" })
            Add-EventLog "开始：$stageLabel"
            $percent = Get-JsonProperty $Event "overall_percent" $null
            if ($null -ne $percent) { Set-ProgressValue ([double]$percent) }
        }
        "stage_progress" {
            $RunStateText.Text = $stageLabel
            if ($message) { $StageText.Text = $message }
            $percent = Get-JsonProperty $Event "overall_percent" $null
            if ($null -eq $percent) { $percent = Get-JsonProperty $Event "progress_percent" $null }
            if ($null -eq $percent) { $percent = Get-JsonProperty $Event "percent" $null }
            if ($null -ne $percent) { Set-ProgressValue ([double]$percent) }
        }
        "stage_completed" {
            Register-StageEnd $Event $stage
            Add-EventLog "完成：$stageLabel"
            if ($message) { $StageText.Text = $message }
            $percent = Get-JsonProperty $Event "overall_percent" $null
            if ($null -ne $percent) { Set-ProgressValue ([double]$percent) }
            Update-DurationSummary $Event
            Update-ReuseSummary
        }
        "stage_failed" {
            Register-StageEnd $Event $stage
            $RunStateText.Text = "$stageLabel 失败"
            if (-not $message) { $message = "$stageLabel 未通过。" }
            Add-EventLog $message
            Set-FailureSummary $message
            Update-DurationSummary $Event
            Update-ReuseSummary
        }
        "resource_status" {
            $afterStage = [string](Get-JsonProperty $Event "after_stage" "")
            Add-EventLog "本地 GPU 阶段已退出：$(Get-StageLabel $afterStage)"
        }
        "warning" {
            if (-not $message) { $message = "后端报告了未说明内容的警告。" }
            Add-Warning $message
        }
        "job_completed" {
            $script:CompletedEvent = $Event
            Update-DurationSummary $Event
            Update-ReuseSummary $Event
            Add-EventLog "后端报告所有阶段已完成，正在核对退出码和产物。"
        }
        "job_failed" {
            $script:FailedEvent = $Event
            if (-not $message) { $message = "后端报告任务失败。" }
            Update-DurationSummary $Event
            Update-ReuseSummary
            Set-FailureSummary $message
            Add-EventLog $message
        }
        "job_cancelled" {
            $script:CancelledEvent = $Event
            Update-DurationSummary $Event
            Update-ReuseSummary
            $RunStateText.Text = "已停止 / 可恢复"
            $RecoverySummaryText.Text = "成功批次和检查点已保留；按原请求继续时只补未完成部分。"
            Add-EventLog $(if ($message) { $message } else { "任务已停止，进度已保留。" })
        }
        default {
            if ($message) { Add-EventLog $message }
        }
    }
    Update-LivePresentation $Event
    if ($eventName -in @(
        "job_started",
        "stage_started",
        "stage_completed",
        "stage_failed",
        "job_completed",
        "job_failed",
        "job_cancelled"
    )) {
        Refresh-TaskCenter
    }
}

function Drain-BackendQueues {
    if ($null -eq $script:Bridge -or $script:DrainingBackendQueues) { return }
    $script:DrainingBackendQueues = $true
    try {
    $line = $null
    while ($script:Bridge.TryGetOutput([ref]$line)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $eventObject = $line | ConvertFrom-Json
            Handle-BackendEvent $eventObject
        }
        catch {
            if (-not $script:ProtocolFailure) {
                $script:ProtocolFailure = "无法验证后端 JSONL 事件：$($_.Exception.Message)"
            }
        }
    }
    $errorLine = $null
    while ($script:Bridge.TryGetError([ref]$errorLine)) {
        if (-not [string]::IsNullOrWhiteSpace($errorLine)) {
            Add-EventLog "后端诊断：$errorLine"
        }
    }
    }
    finally { $script:DrainingBackendQueues = $false }
}

function Complete-Run([bool]$Succeeded, [string]$Message, [bool]$Cancelled = $false) {
    $script:IsRunning = $false
    $script:Finalized = $true
    if ($null -ne $script:PollTimer) { $script:PollTimer.Stop() }
    Set-InputEnabled $true
    $StopRunButton.IsEnabled = $false
    $StopRunButton.Visibility = "Collapsed"
    if ($Succeeded) {
        Set-ProgressValue 100
        $RunStateText.Text = "任务完成，技术检查已通过"
        $RunStateText.Foreground = Convert-ToBrush "#317C64"
        $StageText.Text = $Message
        Update-DurationSummary $script:CompletedEvent
        Update-ReuseSummary $script:CompletedEvent
        $RecoverySummaryText.Text = "后续：机器技术检查已通过；仍请实际播放并判断翻译、同步、画面与字幕开关。"
        if ($script:CurrentWorkflowMode -eq "srt_translation") {
            $RecoverySummaryText.Text = "字幕结构检查已通过；请查看译文自然度和报告中的疑点。"
        }
        $RecoverySummaryText.Foreground = Convert-ToBrush "#317C64"
        $OpenOutputButton.IsEnabled = $true
    }
    elseif ($Cancelled) {
        $RunStateText.Text = "已停止 / 可恢复"
        $RunStateText.Foreground = Convert-ToBrush "#96600D"
        $StageText.Text = $Message
        $RecoverySummaryText.Text = "成功批次和检查点已保留；按原请求继续时只补未完成部分。"
        Update-DurationSummary $script:CancelledEvent
        Update-ReuseSummary
        $OpenOutputButton.IsEnabled = $false
    }
    else {
        $RunStateText.Text = "任务失败"
        $RunStateText.Foreground = Convert-ToBrush "#B04426"
        Set-FailureSummary $Message
        Update-DurationSummary $script:FailedEvent
        Update-ReuseSummary
        $OpenOutputButton.IsEnabled = $false
        Add-EventLog "失败：$Message"
    }
    if ($null -ne $script:Bridge) {
        $script:Bridge.Dispose()
        $script:Bridge = $null
    }
    Complete-RunPresentation $Succeeded $Cancelled
    Refresh-TaskCenter
}

function Finalize-ExitedProcess {
    if ($script:Finalized) { return }
    $exitCode = $script:Bridge.ExitCode
    if ($script:ProtocolFailure) {
        Complete-Run $false $script:ProtocolFailure
        return
    }
    if ($null -ne $script:CancelledEvent -and $exitCode -eq 2) {
        Complete-Run $false "本次任务已停止，成功批次和检查点已保留。" $true
        return
    }
    if ($null -ne $script:FailedEvent) {
        $failureMessage = Get-EventMessage $script:FailedEvent
        if (-not $failureMessage) { $failureMessage = "后端报告任务失败（退出码 $exitCode）。" }
        Complete-Run $false $failureMessage
        return
    }
    if ($exitCode -ne 0) {
        Complete-Run $false "后端进程退出码为 $exitCode，未交付成功结果。"
        return
    }
    if ($null -eq $script:CompletedEvent) {
        Complete-Run $false "后端退出码为 0，但没有 job_completed 事件，禁止显示假成功。"
        return
    }

    Finalize-Result
}

function Finalize-Result {
    $outputs = Get-JsonProperty $script:CompletedEvent "outputs" $null
    $technical = Get-JsonProperty $script:CompletedEvent "technical_validation" $null
    $technicalStatus = [string](Get-JsonProperty $technical "status" "")
    $technicalReportPath = [string](Get-JsonProperty $technical "report_path" "")
    if ($script:CurrentWorkflowMode -eq "srt_translation") {
        $subtitlePath = [string](Get-JsonProperty $outputs "chinese_srt" "")
        if ($technicalStatus -ne "passed" -or -not [IO.File]::Exists($subtitlePath) -or
            -not [IO.File]::Exists($technicalReportPath)) {
            Complete-Run $false "SRT 完成事件缺少通过的结构检查或实际产物。"
            return
        }
        $script:FinalMediaPath = $subtitlePath
        $SubtitleResultText.Text = "中文字幕：$subtitlePath"
        $MkvResultText.Text = "独立字幕任务"
        $ReportResultText.Text = "检查报告：$technicalReportPath"
        Complete-Run $true "中文字幕与报告已生成。请检查译文和疑点。"
        return
    }
    if ($script:CurrentWorkflowMode -in @("media_repackage", "picture_only")) {
        $mediaPath = [string](Get-JsonProperty $outputs "final_media" "")
        $reportPath = [string](Get-JsonProperty $outputs "technical_report" "")
        if ($technicalStatus -ne "passed") {
            Complete-Run $false "后端结束，但媒体技术检查未标记为 passed。"
            return
        }
        foreach ($item in @($mediaPath, $reportPath, $technicalReportPath)) {
            if ([string]::IsNullOrWhiteSpace($item) -or -not [IO.File]::Exists($item)) {
                Complete-Run $false "后端结束，但完成事件中的媒体输出或技术报告不存在。"
                return
            }
        }
        $script:FinalMediaPath = $mediaPath
        $SubtitleResultText.Text = $(if ($script:CurrentWorkflowMode -eq "picture_only") { "已保留原声音和已有字幕" } else { "软字幕：唯一默认 zho（播放时可开关）" })
        $MkvResultText.Text = "媒体输出：$mediaPath"
        $ReportResultText.Text = "技术报告：$reportPath"
        Complete-Run $true "媒体输出和技术报告已存在。请实际播放、拖动并检查同步与字幕开关。"
        return
    }
    $subtitlePath = [string](Get-JsonProperty $outputs "chinese_srt" "")
    $mkvPath = [string](Get-JsonProperty $outputs "final_mkv" "")
    $reportPath = [string](Get-JsonProperty $outputs "technical_report" "")
    $mp4Path = [string](Get-JsonProperty $outputs "final_mp4" "")
    $mp4ReportPath = [string](Get-JsonProperty $outputs "mp4_technical_report" "")

    if ($technicalStatus -ne "passed") {
        Complete-Run $false "后端结束，但最低技术检查未标记为 passed。"
        return
    }
    foreach ($item in @($subtitlePath, $mkvPath, $reportPath, $technicalReportPath)) {
        if ([string]::IsNullOrWhiteSpace($item) -or -not [IO.File]::Exists($item)) {
            Complete-Run $false "后端结束，但完成事件中的字幕、MKV 或技术报告不存在。"
            return
        }
    }

    $script:FinalMediaPath = $mkvPath
    $SubtitleResultText.Text = "中文字幕：$subtitlePath"
    $MkvResultText.Text = "MKV：$mkvPath"
    $ReportResultText.Text = "技术报告：$reportPath"
    if (-not [string]::IsNullOrWhiteSpace($mp4Path)) {
        foreach ($item in @($mp4Path, $mp4ReportPath)) {
            if (-not [IO.File]::Exists($item)) {
                Complete-Run $false "完成事件声明了追加 MP4，但媒体或技术报告不存在。"
                return
            }
        }
        $script:FinalMediaPath = $mp4Path
        $MkvResultText.Text = "MKV：$mkvPath`nMP4：$mp4Path"
        $ReportResultText.Text = "MKV 技术报告：$reportPath`nMP4 技术报告：$mp4ReportPath"
        Complete-Run $true "正式 MKV 与追加 MP4 均已通过技术检查。请实际播放并判断翻译、同步、画面和字幕开关。"
        return
    }
    $stages = Get-JsonProperty $script:CompletedEvent "stages" $null
    $mp4Stage = Get-JsonProperty $stages "mp4_delivery" $null
    if ([string](Get-JsonProperty $mp4Stage "status" "") -eq "failed") {
        Complete-Run $true "正式 MKV 已通过技术检查；追加 MP4 未交付，可按原任务继续重试。"
        return
    }
    Complete-Run $true "中文字幕、MKV 和技术报告均已存在。请实际播放并判断翻译、同步和观看体验。"
}

function Start-ConfirmedRun {
    $runPicture = (Get-SelectedTaskMode) -eq "picture_only"
    $runSrt = (Get-SelectedTaskMode) -eq "srt_translation"
    $runMediaRepackage = (Get-SelectedTaskMode) -eq "media_repackage"
    $runFullWorkflow = (Get-SelectedTaskMode) -eq "translation_picture_final"
    $outputDir = [IO.Path]::GetFullPath($OutputDirectoryBox.Text)
    [IO.Directory]::CreateDirectory($outputDir) | Out-Null
    if (-not $runMediaRepackage -and -not $runPicture) { $settings = Get-SelectedProviderSettings }
    $variant = $(if (-not $runMediaRepackage -and -not $runPicture -and $settings.Id -notin @("codex", "deepseek")) {
        "$($settings.EndpointProfile)`n$($settings.MaxOutputTokens)"
    } else { "" })
    if ($runPicture) {
        $outputTarget = Get-SelectedOutputTarget
        $jobId = 'picture-' + (Get-StableMediaJobId $InputVideoBox.Text $outputDir $outputTarget)
        $recordRoot = '.picture-workflow'
        $request = [ordered]@{
            schema_version=1; workflow_mode='picture_only'; job_id=$jobId;
            input_video=[IO.Path]::GetFullPath($InputVideoBox.Text); output_dir=$outputDir;
            output_target=$outputTarget; gates=[ordered]@{picture_and_mux_30m_human_accepted=$true}
        }
        $backendModule = 'full_workflow.picture_cli'
        $script:CurrentWorkflowMode = 'picture_only'
        $startingMessage = '正在建立去马任务。'
    }
    elseif ($runSrt) {
        $profileId = [string]$ProfileBox.SelectedItem.Tag
        $jobId = "srt-" + (Get-StableJobId $InputVideoBox.Text $outputDir $profileId `
            $script:ProviderId $script:ProviderHost $script:ProviderModel $variant).Substring(4)
        if ($script:PendingSrtRequestPath) {
            $resumeDirectory = [IO.Path]::GetDirectoryName($script:PendingSrtRequestPath)
            $resumeRoot = [IO.Path]::GetFullPath((Join-Path $outputDir ".srt-translation"))
            if ([IO.Path]::GetDirectoryName($resumeDirectory) -ne $resumeRoot) { throw "原 SRT 任务记录不在当前输出目录。" }
            $jobId = [IO.Path]::GetFileName($resumeDirectory)
        }
        $recordRoot = ".srt-translation"
        $request = [ordered]@{
            schema_version=1; workflow_mode="srt_translation"; job_id=$jobId;
            input_srt=[IO.Path]::GetFullPath($InputVideoBox.Text); output_dir=$outputDir;
            profile_id=$profileId;
            provider=[ordered]@{id=$settings.Id; host=$settings.Host; model=$settings.Model};
            consent=[ordered]@{current_run=$true; subtitle_text_only=$true; audio_video_local=$true; provider=$settings.Id}
        }
        $backendModule = "translation_flow.srt_desktop"
        $script:CurrentWorkflowMode = "srt_translation"
        $startingMessage = "已记录本次字幕授权，正在开始 SRT 翻译。"
    }
    elseif (-not $runMediaRepackage) {
        $profileId = [string]$ProfileBox.SelectedItem.Tag
        $jobId = Get-StableJobId $InputVideoBox.Text $outputDir $profileId `
            $script:ProviderId $script:ProviderHost $script:ProviderModel $variant $script:CurrentWhisperAModel
        $recordRoot = $(if ($runFullWorkflow) { ".full-workflow" } else { ".translation-flow" })
        $request = [ordered]@{
            schema_version = 1
            job_id = $jobId
            input_video = [IO.Path]::GetFullPath($InputVideoBox.Text)
            output_dir = $outputDir
            profile_id = $profileId
            provider = [ordered]@{
                id = $script:ProviderId
                host = $script:ProviderHost
                model = $script:ProviderModel
            }
            consent = [ordered]@{
                current_run = $true
                subtitle_text_only = $true
                audio_video_local = $true
            }
            delivery = [ordered]@{
                additional_mp4 = (Get-SelectedOutputTarget) -eq "mp4"
            }
        }
        if ($script:CurrentWhisperAModel -ne "large-v3") { $request["asr_a_model"] = $script:CurrentWhisperAModel }
        if ($runFullWorkflow) {
            $request["workflow_mode"] = "translation_picture_final"
            $request["gates"] = [ordered]@{
                picture_and_mux_30m_human_accepted = $true
            }
            $request["delivery"] = [ordered]@{
                additional_mp4 = (Get-SelectedOutputTarget) -eq "mp4"
            }
        }
        $backendModule = $(if ($runFullWorkflow) { "full_workflow.cli" } else { "translation_flow.cli" })
        $script:CurrentWorkflowMode = $(if ($runFullWorkflow) { "translation_picture_final" } else { "translation" })
        $startingMessage = "已记录本次授权，正在建立正式任务。"
    }
    else {
        $outputTarget = [string]$OutputFormatBox.SelectedItem.Tag
        $jobId = Get-StableMediaJobId $InputVideoBox.Text $outputDir $outputTarget
        $recordRoot = ".media-repackage"
        $request = [ordered]@{
            schema_version = 1
            workflow_mode = "media_repackage"
            job_id = $jobId
            source_media = [IO.Path]::GetFullPath($InputVideoBox.Text)
            output_dir = $outputDir
            output_target = $outputTarget
        }
        $backendModule = "media_planning.cli"
        $script:CurrentWorkflowMode = "media_repackage"
        $startingMessage = "正在建立本地媒体重新封装任务。"
    }
    if (-not $runMediaRepackage -and -not $runPicture -and $settings.Id -notin @("codex", "deepseek")) {
        $request.provider["endpoint_profile"] = $settings.EndpointProfile
        $request.provider["max_output_tokens"] = $settings.MaxOutputTokens
        $request.consent["provider"] = $settings.Id
    }
    $recordDir = Join-Path $outputDir (Join-Path $recordRoot $jobId)
    $script:PendingSrtRequestPath = ""
    [IO.Directory]::CreateDirectory($recordDir) | Out-Null
    $requestPath = Join-Path $recordDir "request.json"
    $json = $request | ConvertTo-Json -Depth 5
    $credential = $(if ($runMediaRepackage -or $runPicture -or $script:ProviderId -eq "codex") { "" } else { $ApiKeyBox.Password })
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    if ($script:QueueEnabled -and [IO.File]::Exists($requestPath)) {
        $priorJson = [IO.File]::ReadAllText($requestPath, [Text.Encoding]::UTF8)
        $priorQueueItem = @(if ($null -ne $script:QueueSnapshot) { $script:QueueSnapshot.items | Where-Object { $_.request_path -eq $requestPath } })
        if ($priorQueueItem.Count -and $priorJson -ne $json) {
            throw "原任务已在队列中，不能改写它的设置。请继续原任务，或选择另一个保存目录。"
        }
    }
    [IO.File]::WriteAllText($requestPath, $json, $utf8NoBom)
    if ($script:CollectQueueRequests) {
        [void]$script:CollectedQueueItems.Add(@{ request_path=$requestPath; tool_root=$ToolRootBox.Text.Trim(); api_key=$credential })
        $credential = $null
        return
    }

    $script:CurrentJobId = $jobId
    $script:LastSeq = 0
    $script:CompletedEvent = $null
    $script:FailedEvent = $null
    $script:CancelledEvent = $null
    $script:CurrentRequestPath = $requestPath
    $script:CurrentProviderId = $(if ($runMediaRepackage) { "" } else { $script:ProviderId })
    $script:ProtocolFailure = ""
    $script:FinalMediaPath = ""
    $script:SelectedOutputDirectory = $outputDir
    $script:Finalized = $false
    $script:IsRunning = $true
    Reset-RunSummary
    Reset-RunPresentation
    $WarningList.Items.Clear()
    $WarningPanel.Visibility = [Windows.Visibility]::Collapsed
    $EventLog.Clear()
    $SubtitleResultText.Text = ""
    $MkvResultText.Text = ""
    $ReportResultText.Text = ""
    $OpenOutputButton.IsEnabled = $false
    $RunStateText.Foreground = Convert-ToBrush "#142333"
    $RunStateText.Text = "正在启动后端"
    $StageText.Text = $startingMessage
    Set-ProgressValue 0
    Set-InputEnabled $false
    $StopRunButton.Content = "停止并保留进度"
    $StopRunButton.Visibility = $(if ($script:CurrentWorkflowMode -in @("translation", "srt_translation")) { "Visible" } else { "Collapsed" })
    $StopRunButton.IsEnabled = $false

    $ApiKeyBox.Clear()
    try {
        $pythonExecutable = Resolve-TranslationPython
        $script:Bridge = Start-TranslationFlowBackend -PythonPath $pythonExecutable `
            -WorkingDirectory $ProjectRoot -RequestPath $requestPath -ApiKey $credential `
            -BackendModule $backendModule -ToolRoot $ToolRootBox.Text.Trim()
        Add-EventLog "后端进程已启动（PID $($script:Bridge.ProcessId)）。"
    }
    catch {
        Complete-Run $false "无法启动后端：$($_.Exception.Message)"
        return
    }
    finally {
        $credential = $null
    }

    $script:PollTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:PollTimer.Interval = [TimeSpan]::FromMilliseconds(180)
    $script:PollTimer.Add_Tick({
        if ($null -eq $script:Bridge -or $script:Finalized) { return }
        Drain-BackendQueues
        if ($script:Bridge.HasExited) {
            $script:Bridge.FinishReading()
            Drain-BackendQueues
            Finalize-ExitedProcess
        }
    })
    $script:PollTimer.Start()
}

function Begin-ConfiguredRun {
    if (-not (Test-SubtitleImportReady)) { return }
    $script:AccessResumeIntent = [bool]$script:RestoringTask -or [bool]$script:PendingQueueResumeId -or [bool]$script:PendingSrtRequestPath
    $errorMessage = Test-RunInputs
    if ($errorMessage) {
        if ($errorMessage -match '模型|model|token|Python|工具') { $MoreSettings.IsExpanded = $true }
        Set-UiPage 'setup'
        $InputErrorText.Text = $errorMessage
        if ($errorMessage -match 'API Key|模型|model|token|Python|工具') {
            $Window.FindName('SettingsOverlay').Visibility='Visible'
            if($errorMessage -match 'API Key'){[void]$ApiKeyBox.Focus()}
        }
        return
    }
    $InputErrorText.Text = ""
    if ((Get-SelectedTaskMode) -in @("media_repackage", "picture_only")) {
        Start-CheckedBatch
        return
    }
    $profileLabel = [string]$ProfileBox.SelectedItem.Content
    $fullDetail = $(if ((Get-SelectedTaskMode) -eq "translation_picture_final") {
        " A+C 正式检查点完成后，本次会按已通过人工验收的 30 分钟路线处理整片画面，并与翻译 API 并行；只有 Whisper A/C 已结束才启动 GPU，最后与原音轨和正式中文字幕汇合。"
    } else { "" })
    $mp4Detail = $(if ((Get-SelectedOutputTarget) -eq "mp4" -and (Get-SelectedTaskMode) -ne "srt_translation") {
        " 正式 MKV 技术通过后会再追加 MP4；追加失败不会推翻或删除 MKV。"
    } else { "" })
    if ($script:ProviderId -eq "codex" -and (Get-SelectedTaskMode) -eq "srt_translation") {
        $ConsentDetailText.Text = "本次通过官方 Codex CLI 的本机 ChatGPT 登录，以 CLI 默认模型翻译已有 SRT。只发送日文字幕、中文草稿和字幕上下文，用于语境、初译、润色和必要的假名修复。使用 ChatGPT/Codex 账户额度，无需 API Key；音频、视频和生成的成片始终留在本机。"
    }
    elseif ($script:ProviderId -eq "codex") {
        $ConsentDetailText.Text = ('本次将以“{0}”模式，通过本机官方 Codex CLI 的 ChatGPT 登录翻译字幕，使用 CLI 默认模型。只发送日文字幕、中文翻译草稿及仅由字幕生成的上下文文本，用于全片语境、分批初译、二轮润色和识别疑点复核；音频、视频和生成的成片始终留在本机。使用 ChatGPT/Codex 账户额度，无需 API Key，不会自动切换到付费 API。' -f $profileLabel) + $fullDetail + $mp4Detail
    }
    elseif ($script:ProviderId -eq "deepseek" -and (Get-SelectedTaskMode) -ne "srt_translation") {
        $ConsentDetailText.Text = (('本次将以“{0}”模式调用 DeepSeek {1}（效果优先、默认思考模式），发送极短连接测试词、日文字幕、中文翻译草稿及仅由字幕生成的上下文文本，用于全片语境、分批初译、二轮润色和必要时一次假名修复。发送目标仅为 {2}，并按 DeepSeek 账户实际 token 用量计费；音频、视频和生成的成片始终留在本机。' -f $profileLabel, $script:ProviderModel, $script:ProviderHost) + $fullDetail + $mp4Detail)
    }
    else {
        $ConsentDetailText.Text = (('本次将以“{0}”模式调用 {1} / {2}，仅向 {3} 发送日文字幕、中文翻译草稿及由字幕生成的上下文，用于语境、分批初译、润色及必要的假名修复。费用按所选服务账户实际用量计收；音频、视频和生成的成片始终留在本机。' -f $profileLabel, $script:ProviderId, $script:ProviderModel, $script:ProviderHost) + $mp4Detail)
    }
    if ((Get-SelectedTaskMode) -eq "srt_translation") {
        $ConsentDetailText.Text += " 本次输入为已有 SRT，只交付中文字幕与报告，不执行音频识别或封装。"
    }
    if ($script:SelectedFiles.Count -gt 1) {
        $ConsentDetailText.Text = "本批共 $($script:SelectedFiles.Count) 个文件，按顺序处理。`n" + $ConsentDetailText.Text + "`n`n" + (($script:SelectedFiles | ForEach-Object { [IO.Path]::GetFileName($_) }) -join "`n")
    }
    if (Test-QueueSessionKeyAvailable) { $ConsentDetailText.Text += "`n后台仍保留本次 Key，可以直接继续。" }
    if (-not $script:RestoringTask -and -not $script:PendingQueueResumeId -and -not $script:PendingSrtRequestPath) {
        $ConsentDetailText.Text += "`n开始前先发送一次固定公开测试词，检查所选服务响应；不含文件内容，会消耗少量账户额度。检查失败不会开始处理影片。"
    }
    $ConsentOverlay.Visibility = [Windows.Visibility]::Visible
}

$TaskModeBox.Add_SelectionChanged({
    if (-not $script:IsRunning) { Update-TaskModeControls }
})
function Sync-FeatureMode {
    $mode = $(if ($SubtitleFeatureButton.IsChecked -and $PictureFeatureButton.IsChecked) { 'translation_picture_final' }
        elseif ($PictureFeatureButton.IsChecked) { 'picture_only' }
        elseif ($SubtitleFeatureButton.IsChecked) { if ([IO.Path]::GetExtension($InputVideoBox.Text) -ieq '.srt') { 'srt_translation' } else { 'translation' } }
        else { 'none' })
    Select-ComboBoxItemByTag $TaskModeBox $mode
    Update-TaskModeControls
}
$SubtitleFeatureButton.Add_Click({
    if (-not $script:IsRunning) {
        if ($script:ImportAutoDeselected -and $SubtitleFeatureButton.IsChecked) {
            $ForceSubtitleTranslationBox.IsChecked=$true
        }
        $script:ImportAutoDeselected=$false
        Sync-FeatureMode
        Update-SubtitleImportSummary
    }
})
$PictureFeatureButton.Add_Click({ if (-not $script:IsRunning) { Sync-FeatureMode } })
$RepackageToolButton.Add_Click({
    if ($script:IsRunning -or $null -ne $script:AccessBridge) { return }
    if (@($script:SelectedFiles + @($InputVideoBox.Text) | Where-Object { [IO.Path]::GetExtension($_) -ieq '.srt' }).Count) {
        $InputErrorText.Text = '换格式需要视频文件，请先选择已有中文字幕的视频。'
        return
    }
    Select-ComboBoxItemByTag $TaskModeBox 'media_repackage'
    $InputErrorText.Text = ''
    $MoreSettings.IsExpanded = $true
    Update-TaskModeControls
    $Window.FindName('SettingsOverlay').Visibility='Collapsed'
    Set-UiPage 'setup'
})
$ProviderBox.Add_SelectionChanged({
    if ($script:IsRunning) { return }
    $ApiKeyBox.Clear()
    if ([string]$ProviderBox.SelectedItem.Tag -eq "codex" -and (Get-SelectedTaskMode) -notin @('picture_only','media_repackage','none')) {
        Start-CodexStatusCheck
    }
    Update-TaskModeControls
})
$EndpointBox.Add_SelectionChanged({ if (-not $script:IsRunning) { $ApiKeyBox.Clear() } })
$RefreshCodexStatusButton.Add_Click({ Start-CodexStatusCheck })
$LoginCodexButton.Add_Click({ Start-DesktopAccess 'login' })
$InstallCodexButton.Add_Click({ Start-Process 'https://learn.chatgpt.com/docs/codex/cli' })
$StopRunButton.Add_Click({
    if ($script:QueueEnabled -and $script:CurrentQueueId) {
        try { [void](Start-QueueCall @{action='stop'; id=$script:CurrentQueueId}); $StopRunButton.IsEnabled=$false; $StageText.Text='正在保存当前进度，完成后会停止。' } catch { Add-Warning $_.Exception.Message }
        return
    }
    if (-not $script:IsRunning -or $script:CurrentWorkflowMode -notin @("translation", "srt_translation")) { return }
    try {
        [void](Request-TranslationFlowCancellation $script:CurrentRequestPath)
        $StopRunButton.IsEnabled = $false
        $StopRunButton.Content = "正在停止…"
        $StageText.Text = "正在保存当前进度，完成后会停止。"
        Add-EventLog "已请求停止当前字幕任务。"
    }
    catch { Add-Warning "停止请求未写入：$($_.Exception.Message)" }
})
$TaskHistoryGrid.Add_SelectionChanged({ Show-SelectedTaskDetail })
$RefreshTaskCenterButton.Add_Click({ Refresh-TaskCenter })
$ResumeTaskButton.Add_Click({
    $selected = $TaskHistoryGrid.SelectedItem
    if ($null -eq $selected) { return }
    Set-UiPage 'setup'
    try {
        $resume = Get-TaskCenterResumeRequest ([string]$selected.RequestPath)
        $script:CurrentWhisperAModel = $resume.AsrAModel
        Select-ComboBoxItemByTag $WhisperModelBox $resume.AsrAModel
        $script:SelectedFiles = @()
        $SelectedFilesText.Visibility = 'Collapsed'
        $script:PendingQueueResumeId = ''
        if ($script:QueueEnabled -and $null -ne $script:QueueSnapshot) {
            $queueMatch = @($script:QueueSnapshot.items | Where-Object { $_.request_path -eq $resume.RequestPath -and ((Test-QueueItemPaused $_) -or $_.status -in @('stopped','failed','interrupted')) })
            if ($queueMatch.Count) { $script:PendingQueueResumeId = $queueMatch[0].id }
        }
        Select-ComboBoxItemByTag $TaskModeBox $resume.TaskMode
        if ($resume.TaskMode -notin @("media_repackage", "picture_only")) {
            $restored = Get-TranslationProviderSettings $resume.ProviderId $resume.ProviderModel $resume.EndpointProfile $resume.MaxOutputTokens
            if ($restored.Host -ne $resume.ProviderHost) { throw "原任务翻译服务地址不受支持" }
            if ($restored.Id -eq "deepseek") { $script:DeepSeekModel = $restored.Model }
            Select-ComboBoxItemByTag $ProviderBox $restored.Id
            $ModelBox.Text = $restored.Model
            if ($restored.Id -notin @("deepseek", "codex")) {
                Select-ComboBoxItemByTag $EndpointBox $restored.EndpointProfile
                $TokenCapBox.Text = [string]$restored.MaxOutputTokens
            }
            Select-ComboBoxItemByTag $ProfileBox $resume.ProfileId
        }
        Select-ComboBoxItemByTag $OutputFormatBox $resume.OutputTarget
        $InputVideoBox.Text = $resume.SourcePath
        $OutputDirectoryBox.Text = $resume.OutputDirectory
        Update-TaskModeControls
        $InputErrorText.Text = ""
        $script:PendingSrtRequestPath = $(if ($resume.TaskMode -eq "srt_translation") { $resume.RequestPath } else { "" })
        $script:RestoringTask = $true
        try { Begin-ConfiguredRun }
        finally { $script:RestoringTask = $false }
    }
    catch {
        $InputErrorText.Text = "无法继续所选任务：$($_.Exception.Message)"
    }
})

$BrowseVideoButton.Add_Click({
    $dialog = New-Object Microsoft.Win32.OpenFileDialog
    $dialog.Title = "选择视频"
    $dialog.Filter = $(if ((Get-SelectedTaskMode) -eq "media_repackage") { "视频文件|*.mkv;*.mp4;*.mov;*.avi;*.webm;*.m4v;*.ts;*.mts;*.m2ts;*.wmv|所有文件|*.*" }
        else { "视频或字幕|*.mkv;*.mp4;*.mov;*.avi;*.webm;*.m4v;*.ts;*.mts;*.m2ts;*.wmv;*.srt|所有文件|*.*" })
    $dialog.Multiselect = $script:QueueEnabled
    if ($dialog.ShowDialog($Window) -eq $true) { Set-SelectedFiles $dialog.FileNames }
})

$BrowseOutputButton.Add_Click({
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "选择媒体结果和技术报告的输出目录"
    $dialog.ShowNewFolderButton = $true
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $OutputDirectoryBox.Text = [IO.Path]::GetFullPath($dialog.SelectedPath)
        $InputErrorText.Text = ""
        Refresh-TaskCenter
    }
    $dialog.Dispose()
})

$DropZone.Add_DragEnter({
    if ($_.Data.GetDataPresent([Windows.DataFormats]::FileDrop)) {
        $_.Effects = [Windows.DragDropEffects]::Copy
        $DropZoneInner.BorderBrush = Convert-ToBrush "#00A5A2"
    }
    else { $_.Effects = [Windows.DragDropEffects]::None }
    $_.Handled = $true
})
$DropZone.Add_DragLeave({ $DropZoneInner.BorderBrush = Convert-ToBrush "#E3E9EF" })
$DropZone.Add_Drop({
    $DropZoneInner.BorderBrush = Convert-ToBrush "#E3E9EF"
    if ($_.Data.GetDataPresent([Windows.DataFormats]::FileDrop)) {
        $paths = @($_.Data.GetData([Windows.DataFormats]::FileDrop))
        Set-SelectedFiles $paths
    }
})

$StartButton.Add_Click({
    $script:CurrentWhisperAModel = [string]$WhisperModelBox.SelectedItem.Tag
    $script:PendingSrtRequestPath = ""
    try { Begin-ConfiguredRun }
    catch { $InputErrorText.Text = "无法建立任务：$($_.Exception.Message)" }
})
$CancelConsentButton.Add_Click({
    $ConsentOverlay.Visibility = [Windows.Visibility]::Collapsed
    if (-not $script:PendingQueueResumeId) { $script:PendingSrtRequestPath = "" }
})
$ConfirmConsentButton.Add_Click({
    $ConsentOverlay.Visibility = [Windows.Visibility]::Collapsed
    try { Start-CheckedBatch }
    catch { $InputErrorText.Text = "无法建立任务：$($_.Exception.Message)" }
})

$OpenOutputButton.Add_Click({
    if (-not [string]::IsNullOrWhiteSpace($script:FinalMediaPath) -and [IO.File]::Exists($script:FinalMediaPath)) {
        Start-Process -FilePath "explorer.exe" -ArgumentList "/select,`"$($script:FinalMediaPath)`""
    }
    elseif ([IO.Directory]::Exists($script:SelectedOutputDirectory)) {
        Start-Process -FilePath "explorer.exe" -ArgumentList "`"$($script:SelectedOutputDirectory)`""
    }
})


$InputVideoBox.Add_TextChanged({
    $hasFile = -not [string]::IsNullOrWhiteSpace($InputVideoBox.Text)
    $DropTitle.ToolTip = $InputVideoBox.Text
    $DropTitle.Text = $(if ($hasFile) { [IO.Path]::GetFileName($InputVideoBox.Text) } elseif ((Get-SelectedTaskMode) -eq 'media_repackage') { '把已有字幕的视频拖到这里' } else { '把视频或字幕文件拖到这里' })
    $StartButton.IsEnabled = (-not $script:IsRunning) -and (-not $script:QueueSubmitting) -and [IO.File]::Exists($InputVideoBox.Text)
})
$NewTaskButton.Add_Click({
    if ($script:IsRunning) { Set-UiPage 'home'; return }
    Select-ComboBoxItemByTag $WhisperModelBox $WhisperAModel
    $script:CurrentWhisperAModel = $WhisperAModel
    $script:PendingQueueResumeId=''; $script:PendingSrtRequestPath=''; $script:SelectedFiles=@()
    $script:ImportPaths=@(); $script:ImportAutoDeselected=$false
    $ForceSubtitleTranslationBox.IsChecked=$false; $SubtitleImportPanel.Visibility='Collapsed'
    $SelectedFilesText.Visibility='Collapsed'
    $InputVideoBox.Clear(); $ApiKeyBox.Clear(); $InputErrorText.Text=''
    Select-ComboBoxItemByTag $TaskModeBox 'translation'
    Update-TaskModeControls
    Set-UiPage 'home'
})
$ShowTasksButton.Add_Click({ Refresh-TaskCenter; Set-UiPage 'tasks' })
$ShowLibraryButton.Add_Click({ Show-DeliveryLibrary })
$Window.FindName('BeautyPlayerButton').Add_Click({ Show-BeautyPlayer })
$BackFromTasksButton.Add_Click({ Set-UiPage $(if ($script:CurrentRequestPath) { 'run' } else { 'setup' }) })
$RetryRunButton.Add_Click({
    if ($script:QueueEnabled -and $script:CurrentQueueId) {
        $row=@($QueueGrid.ItemsSource | Where-Object { $_.id -eq $script:CurrentQueueId })
        if ($row.Count) { $QueueGrid.SelectedItem=$row[0]; $ResumeQueueItemButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent))); return }
    }
    Refresh-TaskCenter
    $found = @($TaskHistoryGrid.ItemsSource | Where-Object { $_.RequestPath -eq $script:CurrentRequestPath })
    if ($found.Count) {
        $TaskHistoryGrid.SelectedItem = $found[0]
        $ResumeTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent)))
    } else { Set-UiPage 'setup'; $InputErrorText.Text = '没有找到原任务记录，请检查保存目录。' }
})
$OpenResultButton.Add_Click({
    if ($script:FinalMediaPath -and [IO.File]::Exists($script:FinalMediaPath)) {
        Start-Process -FilePath $script:FinalMediaPath
    }
})
$script:ClockTimer = New-Object Windows.Threading.DispatcherTimer
$script:ClockTimer.Interval = [TimeSpan]::FromSeconds(1)
$script:ClockTimer.Add_Tick({ Update-ClockPresentation })
$script:ClockTimer.Start()

$Window.Add_Closing({
    if ($script:QueueEnabled) {
        if ($script:QueueSubmitting) { $_.Cancel=$true; $StageText.Text='正在把任务交给后台，确认接收后就可以关闭。' }
        return
    }
    if ($script:IsRunning) {
        $_.Cancel = $true
        [System.Windows.MessageBox]::Show(
            $Window,
            "任务还在进行。请先停止并保存进度，再关闭窗口。",
            "媒体任务正在运行",
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Information
        ) | Out-Null
    }
})

Initialize-QueueUi
Initialize-SubtitleImport
$BackgroundButton.Add_Click({ if ($script:QueueEnabled) { $Window.Close() } })
$PauseQueueButton.Add_Click({ try { [void](Start-QueueCall @{action='pause'}) } catch { $QueueStatusText.Text=$_.Exception.Message } })
$StopQueueButton.Add_Click({ try { [void](Start-QueueCall @{action='stop'}) } catch { $QueueStatusText.Text=$_.Exception.Message } })
$ViewQueueItemButton.Add_Click({
    if ($null -eq $QueueGrid.SelectedItem) { return }
    $script:CurrentQueueId=$QueueGrid.SelectedItem.id
    $script:QueueRunStamp=''
    Set-UiPage 'run'
    $script:QueuePollAt=[DateTime]::MinValue
})
$ResumeQueueItemButton.Add_Click({
    if ($null -eq $QueueGrid.SelectedItem) { return }
    $queueItem=$QueueGrid.SelectedItem.Item
    if (-not (Test-QueueItemPaused $queueItem) -and $queueItem.status -notin @('stopped','failed','interrupted')) { $QueueStatusText.Text='请选择已暂停或未完成的任务。'; return }
    try {
        $request=[IO.File]::ReadAllText($queueItem.request_path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $OutputDirectoryBox.Text=[string]$request.output_dir
    } catch { $QueueStatusText.Text='找不到原任务设置，请检查文件是否被移动。'; return }
    Refresh-TaskCenter
    $match=@($TaskHistoryGrid.ItemsSource | Where-Object { $_.RequestPath -eq $queueItem.request_path })
    if ($match.Count) { $TaskHistoryGrid.SelectedItem=$match[0]; $ResumeTaskButton.RaiseEvent((New-Object Windows.RoutedEventArgs ([Windows.Controls.Button]::ClickEvent))) }
})
Initialize-Design
Update-TaskModeControls
Refresh-TaskCenter
Start-CodexStatusCheck
[void]$Window.ShowDialog()
$script:ImportTimer.Stop()
if ($null -ne $script:ImportBridge) { $script:ImportBridge.Dispose() }
$script:ClockTimer.Stop()
if ($script:QueueEnabled) { $script:QueueTimer.Stop(); if ($null -ne $script:QueueCall) { $script:QueueCall.Shell.Dispose() } }
if ($null -ne $script:CodexStatusTimer) { $script:CodexStatusTimer.Stop() }
if ($null -ne $script:CodexStatusBridge) { $script:CodexStatusBridge.Dispose() }
if ($null -ne $script:AccessTimer) { $script:AccessTimer.Stop() }
if ($null -ne $script:AccessBridge) { $script:AccessBridge.Dispose() }

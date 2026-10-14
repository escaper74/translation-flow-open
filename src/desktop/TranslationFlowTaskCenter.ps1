Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "TranslationFlowProvider.ps1")

function Get-TaskCenterProperty($Value, [string]$Name, $Default = $null) {
    if ($null -eq $Value) { return $Default }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Read-TaskCenterJson([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $null }
    $text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    return $text | ConvertFrom-Json
}

function Get-TaskCenterEventEvidence([string]$Path) {
    $lastEvent = $null
    $lastFailure = $null
    $lastStage = ""
    if ([IO.File]::Exists($Path)) {
        foreach ($line in @(Get-Content -LiteralPath $Path -Encoding UTF8 -Tail 200)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try { $item = $line | ConvertFrom-Json }
            catch { continue }
            $lastEvent = $item
            $name = [string](Get-TaskCenterProperty $item "event" "")
            $stage = [string](Get-TaskCenterProperty $item "stage" "")
            if (-not [string]::IsNullOrWhiteSpace($stage)) { $lastStage = $stage }
            if ($name -in @("stage_failed", "job_failed", "job_cancelled")) { $lastFailure = $item }
        }
    }
    return [pscustomobject]@{
        LastEvent = $lastEvent
        LastFailure = $lastFailure
        LastStage = $lastStage
    }
}

function Get-TaskCenterEventText($Event) {
    foreach ($name in @("message", "detail", "warning", "error")) {
        $value = [string](Get-TaskCenterProperty $Event $name "")
        if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
    }
    return ""
}

function Get-TaskCenterPreferredEntry($Entries, [string]$PreviousRequestPath = "") {
    $items = @($Entries)
    if (-not [string]::IsNullOrWhiteSpace($PreviousRequestPath)) {
        $previous = $items | Where-Object {
            [string]$_.RequestPath -eq $PreviousRequestPath
        } | Select-Object -First 1
        if ($null -ne $previous) { return $previous }
    }
    $resumable = $items | Where-Object { $_.CanResume -eq $true } |
        Select-Object -First 1
    if ($null -ne $resumable) { return $resumable }
    return $items | Select-Object -First 1
}

function Get-TaskCenterEntries([string]$OutputDirectory) {
    if ([string]::IsNullOrWhiteSpace($OutputDirectory) -or
        -not [IO.Directory]::Exists($OutputDirectory)) {
        return @()
    }
    $specs = @(
        [pscustomobject]@{ Root = ".picture-workflow"; Mode = "picture"; Label = "去马" },
        [pscustomobject]@{ Root = ".full-workflow"; Mode = "full"; Label = "完整工作流" },
        [pscustomobject]@{ Root = ".translation-flow"; Mode = "translation"; Label = "仅翻译" },
        [pscustomobject]@{ Root = ".srt-translation"; Mode = "srt"; Label = "SRT 翻译" },
        [pscustomobject]@{ Root = ".media-repackage"; Mode = "media"; Label = "重新封装" }
    )
    $entries = New-Object System.Collections.ArrayList
    foreach ($spec in $specs) {
        $recordRoot = Join-Path $OutputDirectory $spec.Root
        if (-not [IO.Directory]::Exists($recordRoot)) { continue }
        foreach ($jobDirectory in @(Get-ChildItem -LiteralPath $recordRoot -Directory -Force -ErrorAction SilentlyContinue)) {
            $requestPath = Join-Path $jobDirectory.FullName "request.json"
            if (-not [IO.File]::Exists($requestPath)) { continue }
            try {
                $request = Read-TaskCenterJson $requestPath
                $sourcePath = $(if ($spec.Mode -eq "media") {
                    [string](Get-TaskCenterProperty $request "source_media" "")
                } elseif ($spec.Mode -eq "srt") {
                    [string](Get-TaskCenterProperty $request "input_srt" "")
                } else {
                    [string](Get-TaskCenterProperty $request "input_video" "")
                })
                $outputDir = [string](Get-TaskCenterProperty $request "output_dir" "")
                if ([string]::IsNullOrWhiteSpace($sourcePath) -or
                    [string]::IsNullOrWhiteSpace($outputDir)) {
                    throw "request 缺少源媒体或输出目录"
                }
                $eventPath = Join-Path $jobDirectory.FullName "events.jsonl"
                $eventEvidence = Get-TaskCenterEventEvidence $eventPath
                $lastEvent = $eventEvidence.LastEvent
                $lastFailure = $eventEvidence.LastFailure
                $result = $null
                $manifest = $null
                $resultPath = ""
                if ($spec.Mode -eq "full") {
                    $resultPath = Join-Path $jobDirectory.FullName "workflow.result.json"
                    $manifest = Read-TaskCenterJson (Join-Path $jobDirectory.FullName "workflow.json")
                }
                elseif ($spec.Mode -in @("translation", "srt", "picture")) {
                    $resultPath = Join-Path $jobDirectory.FullName "job.result.json"
                }
                else {
                    $resultPath = Join-Path $jobDirectory.FullName "technical-validation.report.json"
                }
                $result = Read-TaskCenterJson $resultPath

                $status = "未完成 / 可恢复"
                $failure = ""
                $technicalPassed = $false
                if ($null -ne $result) {
                    if ($spec.Mode -in @("translation", "srt", "picture")) {
                        $technical = Get-TaskCenterProperty $result "technical_validation" $null
                        $technicalPassed = [string](Get-TaskCenterProperty $technical "status" "") -eq "passed"
                    }
                    else {
                        $technicalPassed = [string](Get-TaskCenterProperty $result "status" "") -eq "technical_checks_passed"
                    }
                }
                if ($technicalPassed) {
                    $status = "技术通过 / 待观看"
                    if ($spec.Mode -eq "srt") { $status = "结构通过 / 待审阅" }
                    if ($spec.Mode -in @("full", "translation")) {
                        $stages = Get-TaskCenterProperty $result "stages" $null
                        $mp4Stage = Get-TaskCenterProperty $stages "mp4_delivery" $null
                        if ([string](Get-TaskCenterProperty $mp4Stage "status" "") -eq "failed") {
                            $status = "MKV 已通过 / MP4 失败"
                            $failure = [string](Get-TaskCenterProperty $mp4Stage "error" "")
                        }
                    }
                }
                else {
                    $manifestStatus = [string](Get-TaskCenterProperty $manifest "status" "")
                    $lastEventName = [string](Get-TaskCenterProperty $lastEvent "event" "")
                    if ($manifestStatus -eq "failed" -or $lastEventName -eq "job_failed") {
                        $status = "失败 / 可恢复"
                        $failure = [string](Get-TaskCenterProperty $manifest "error" "")
                        if ([string]::IsNullOrWhiteSpace($failure)) {
                            $failure = Get-TaskCenterEventText $lastFailure
                        }
                    }
                    elseif ($lastEventName -eq "job_cancelled") {
                        $status = "已停止 / 可恢复"
                        $failure = Get-TaskCenterEventText $lastFailure
                    }
                    elseif ($null -eq $lastEvent -and $null -eq $manifest -and $null -eq $result) {
                        $status = "请求已保存 / 无运行记录"
                        $failure = "仅找到任务请求，尚无后台运行记录。请检查后台提示，再按原请求重试。"
                    }
                }

                $lastStage = [string]$eventEvidence.LastStage
                $outputPaths = New-Object System.Collections.ArrayList
                if ($null -ne $result) {
                    $outputs = Get-TaskCenterProperty $result "outputs" $null
                    if ($null -ne $outputs) {
                        foreach ($property in $outputs.PSObject.Properties) {
                            $candidate = [string]$property.Value
                            if (-not [string]::IsNullOrWhiteSpace($candidate)) {
                                [void]$outputPaths.Add($candidate)
                            }
                        }
                    }
                    if ($spec.Mode -eq "media") {
                        $output = Get-TaskCenterProperty $result "output" $null
                        $candidate = [string](Get-TaskCenterProperty $output "path" "")
                        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
                            [void]$outputPaths.Add($candidate)
                        }
                    }
                }
                $evidencePaths = @($requestPath, $eventPath, $resultPath, (Join-Path $jobDirectory.FullName "workflow.json"))
                $updated = $jobDirectory.LastWriteTimeUtc
                foreach ($path in $evidencePaths) {
                    if ([IO.File]::Exists($path)) {
                        $candidateTime = [IO.File]::GetLastWriteTimeUtc($path)
                        if ($candidateTime -gt $updated) { $updated = $candidateTime }
                    }
                }
                $sourceName = [IO.Path]::GetFileName($sourcePath)
                $cleaned = [IO.File]::Exists((Join-Path $jobDirectory.FullName 'retention-cleanup.json'))
                if ($cleaned) { $status += ' / 检查点已清理'; $failure = '成片保留；大媒体检查点已经用户清理，此任务不再提供原地恢复。' }
                if ([string]::IsNullOrWhiteSpace($sourceName)) { $sourceName = $sourcePath }
                $provider = Get-TaskCenterProperty $request "provider" $null
                $providerId = [string](Get-TaskCenterProperty $provider "id" "")
                $providerModel = [string](Get-TaskCenterProperty $provider "model" "")
                $providerLabel = $(if ($providerId -eq "codex") { "Codex（ChatGPT 登录 / CLI 默认模型）" }
                    elseif ($providerId -eq "deepseek") { "DeepSeek API / $providerModel" }
                    elseif ($providerId) { "$providerId / $providerModel" } else { "本地媒体" })
                [void]$entries.Add([pscustomobject]@{
                    JobId = $jobDirectory.Name
                    SourceName = $sourceName
                    SourcePath = $sourcePath
                    ModeLabel = $spec.Label
                    ProviderLabel = $providerLabel
                    StatusLabel = $status
                    StageLabel = $lastStage
                    UpdatedText = $updated.ToLocalTime().ToString("yyyy-MM-dd HH:mm")
                    FailureText = $failure
                    OutputsText = (@($outputPaths | Select-Object -Unique) -join "`n")
                    RequestPath = $requestPath
                    OutputDirectory = $outputDir
                    CanResume = [IO.File]::Exists($sourcePath) -and -not $cleaned
                    SortTime = $updated
                })
            }
            catch {
                [void]$entries.Add([pscustomobject]@{
                    JobId = $jobDirectory.Name
                    SourceName = "（任务记录不可读）"
                    SourcePath = ""
                    ModeLabel = $spec.Label
                    ProviderLabel = ""
                    StatusLabel = "记录损坏"
                    StageLabel = ""
                    UpdatedText = $jobDirectory.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
                    FailureText = $_.Exception.Message
                    OutputsText = ""
                    RequestPath = $requestPath
                    OutputDirectory = $OutputDirectory
                    CanResume = $false
                    SortTime = $jobDirectory.LastWriteTimeUtc
                })
            }
        }
    }
    return @($entries | Sort-Object SortTime -Descending | Select-Object -First 50)
}

function Get-TaskCenterResumeRequest([string]$RequestPath) {
    if ([IO.File]::Exists((Join-Path (Split-Path -Parent $RequestPath) 'retention-cleanup.json'))) {
        throw '本任务的大媒体检查点已经清理，请直接使用保留成片；如需重新处理，请新建任务。'
    }
    if (-not [IO.File]::Exists($RequestPath) -or
        [IO.Path]::GetFileName($RequestPath) -ne "request.json") {
        throw "正式 request.json 不存在：$RequestPath"
    }
    $raw = [IO.File]::ReadAllText($RequestPath, [Text.Encoding]::UTF8)
    if ($raw -match '(?i)"(?:api_key|subtitle_api_key)"\s*:') {
        throw "任务 request 不应包含 API Key，拒绝加载"
    }
    $request = $raw | ConvertFrom-Json
    if ([int](Get-TaskCenterProperty $request "schema_version" 0) -ne 1) {
        throw "不支持的任务 request 协议版本"
    }
    $workflowMode = [string](Get-TaskCenterProperty $request "workflow_mode" "translation")
    $media = $workflowMode -in @("media_repackage", "picture_only")
    $full = $workflowMode -eq "translation_picture_final"
    $srt = $workflowMode -eq "srt_translation"
    if (-not $media -and -not $full -and -not $srt -and $workflowMode -ne "translation") {
        throw "不支持的任务模式：$workflowMode"
    }
    $sourcePath = $(if ($workflowMode -eq "media_repackage") {
        [string](Get-TaskCenterProperty $request "source_media" "")
    } elseif ($srt) {
        [string](Get-TaskCenterProperty $request "input_srt" "")
    } else {
        [string](Get-TaskCenterProperty $request "input_video" "")
    })
    if (-not [IO.File]::Exists($sourcePath)) {
        throw "原任务的源媒体不存在：$sourcePath"
    }
    $outputDirectory = [string](Get-TaskCenterProperty $request "output_dir" "")
    if ([string]::IsNullOrWhiteSpace($outputDirectory)) {
        throw "原任务缺少输出目录"
    }
    $outputTarget = "mkv"
    if ($media) {
        $outputTarget = [string](Get-TaskCenterProperty $request "output_target" "")
    }
    else {
        $delivery = Get-TaskCenterProperty $request "delivery" $null
        if ((Get-TaskCenterProperty $delivery "additional_mp4" $false) -eq $true) {
            $outputTarget = "mp4"
        }
    }
    $provider = Get-TaskCenterProperty $request "provider" $null
    if (-not $media) {
        $settings = Get-TranslationProviderSettings `
            ([string](Get-TaskCenterProperty $provider "id" "")) `
            ([string](Get-TaskCenterProperty $provider "model" "")) `
            ([string](Get-TaskCenterProperty $provider "endpoint_profile" "")) `
            ([int](Get-TaskCenterProperty $provider "max_output_tokens" 8192))
        if ($settings.Host -ne [string](Get-TaskCenterProperty $provider "host" "")) {
            throw "原任务翻译服务地址不受支持"
        }
    }
    $asrAModel = [string](Get-TaskCenterProperty $request "asr_a_model" "large-v3")
    if ($asrAModel -notin @("large-v3", "large-v3-turbo")) { throw "原任务的 Whisper A 模型不受支持" }
    return [pscustomobject]@{
        RequestPath = [IO.Path]::GetFullPath($RequestPath)
        AsrAModel = $asrAModel
        SourcePath = [IO.Path]::GetFullPath($sourcePath)
        OutputDirectory = [IO.Path]::GetFullPath($outputDirectory)
        TaskMode = $(if ($media) {
            $workflowMode
        } elseif ($full) {
            "translation_picture_final"
        } elseif ($srt) {
            "srt_translation"
        } else {
            "translation"
        })
        FullWorkflow = $full
        OutputTarget = $outputTarget
        ProfileId = [string](Get-TaskCenterProperty $request "profile_id" "")
        ProviderId = [string](Get-TaskCenterProperty $provider "id" "")
        ProviderHost = [string](Get-TaskCenterProperty $provider "host" "")
        ProviderModel = [string](Get-TaskCenterProperty $provider "model" "")
        EndpointProfile = [string](Get-TaskCenterProperty $provider "endpoint_profile" "")
        MaxOutputTokens = [int](Get-TaskCenterProperty $provider "max_output_tokens" 8192)
    }
}

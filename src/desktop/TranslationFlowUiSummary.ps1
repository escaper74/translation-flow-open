Set-StrictMode -Version Latest

function Get-UiObjectProperty($Value, [string]$Name, $Default = $null) {
    if ($null -eq $Value) { return $Default }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function ConvertTo-UiEventTime($Event) {
    $raw = [string](Get-UiObjectProperty $Event "timestamp_utc" "")
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    try {
        return [DateTimeOffset]::Parse(
            $raw,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch { return $null }
}

function Format-UiDuration([double]$Seconds) {
    if ([double]::IsNaN($Seconds) -or [double]::IsInfinity($Seconds) -or $Seconds -lt 0) {
        return "未知"
    }
    if ($Seconds -lt 1) { return "<1 秒" }
    $span = [TimeSpan]::FromSeconds([Math]::Round($Seconds))
    if ($span.TotalHours -ge 1) {
        return ("{0} 小时 {1} 分 {2} 秒" -f [Math]::Floor($span.TotalHours), $span.Minutes, $span.Seconds)
    }
    if ($span.TotalMinutes -ge 1) {
        return ("{0} 分 {1} 秒" -f [Math]::Floor($span.TotalMinutes), $span.Seconds)
    }
    return ("{0} 秒" -f $span.Seconds)
}

function Get-UiReuseItems($CompletedEvent, [hashtable]$ObservedResumes) {
    $items = New-Object System.Collections.ArrayList
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $stages = Get-UiObjectProperty $CompletedEvent "stages" $null
    if ($null -ne $stages) {
        foreach ($property in $stages.PSObject.Properties) {
            $name = [string]$property.Name
            $requested = Get-UiObjectProperty $property.Value "requested" $null
            if ($requested -eq $false) { continue }
            $reused = (Get-UiObjectProperty $property.Value "reused" $false) -eq $true
            [void]$items.Add([pscustomobject]@{ stage = $name; reused = $reused })
            [void]$seen.Add($name)
        }
    }
    if ($null -ne $ObservedResumes) {
        foreach ($name in $ObservedResumes.Keys) {
            if ($ObservedResumes[$name] -eq $true -and -not $seen.Contains([string]$name)) {
                [void]$items.Add([pscustomobject]@{ stage = [string]$name; reused = $true })
                [void]$seen.Add([string]$name)
            }
        }
    }
    return @($items)
}

function Get-UiFailureGuidance(
    [string]$ErrorText,
    [string]$Stage,
    [string]$StageLabel
) {
    $title = "这一步没能完成"
    $summary = "已有进度会保留。"
    $nextStep = "查看详情，确认原因后可以继续原任务。"
    if ($ErrorText -match "(?i)(Codex.*(登录|login|认证)|api_login_not_allowed|login_required)") {
        $title = "需要重新登录 Codex"
        $summary = "已完成的部分已经保存。"
        $nextStep = "在终端运行 codex login，登录 ChatGPT 后刷新状态，再继续任务。"
    }
    elseif ($ErrorText -match "(?i)(quota|额度|余额|insufficient.*balance|billing|credit)") {
        $title = "翻译服务的额度暂时不够"
        $summary = "已完成的部分已经保存。"
        $nextStep = "等待账户额度恢复后，再继续任务。"
    }
    elseif ($ErrorText -match "(?i)(job_cancelled|取消|已停止)") {
        $title = "已停止，进度已保留"
        $summary = "已完成的部分会直接复用。"
        $nextStep = "准备好后，可以按原请求继续，只补未完成部分。"
    }
    elseif ($ErrorText -match "(?i)(API Key|api.key|401|authentication|unauthorized|没有可复用的正式翻译结果)") {
        $title = "请检查 API Key"
        $summary = "本次 API Key 缺失或无法使用。"
        $nextStep = "填写所选服务对应的 Key，再继续任务。已有进度会保留。"
    }
    elseif ($ErrorText -match "(?i)(需要转码或改用 MKV|codec.*not.*support|not supported.*container)") {
        $title = "这个视频不能直接转成所选格式"
        $summary = "原视频和已有结果都还在。"
        $nextStep = "改选 MKV 后再试；如果需要其他格式，要先转换视频编码。"
    }
    elseif ($ErrorText -match "(?i)(源片.*不匹配|different source|谱系.*不匹配|checkpoint.*match|manifest.*不匹配|源文或翻译配置.*不一致)") {
        $title = "文件或设置与原任务不一致"
        $summary = "这次选择与原任务的检查点不一致，暂时不能接着做。"
        $nextStep = "核对文件、翻译服务和保存目录，再继续原任务。"
    }
    elseif ($ErrorText -match "(?i)(拒绝覆盖|already exists|FileExistsError|非空且没有 manifest|部分终态)") {
        $title = "保存位置已有同名结果"
        $summary = "已有文件没有被覆盖。"
        $nextStep = "继续原任务，或为新任务选择另一个保存目录。"
    }
    elseif ($ErrorText -match "(?i)(timeout|timed.out|connection|connect|network|URLError|网络|连接|超时|502|503|504)") {
        $title = "暂时连不上翻译服务"
        $summary = "已完成的部分已经保存。"
        $nextStep = "检查网络后，可以接着翻译。如果网络正常，稍后再试。"
    }
    elseif ($ErrorText -match "(?i)(429|rate.limit|限流|too many requests)") {
        $title = "翻译服务现在有点忙"
        $summary = "已完成的部分已经保存。"
        $nextStep = "等一会儿，再继续翻译。"
    }
    elseif ($ErrorText -match "(?i)(Python|找不到|不存在|No such file|not found)") {
        $title = "有个文件或工具没找到"
        $summary = "这一步暂时无法继续。"
        $nextStep = "在详情里查看缺少什么，再检查文件位置或本地工具设置。"
    }
    elseif ($ErrorText -match "(?i)(FFmpeg|FFprobe|mux|decode|编码|封装)") {
        $title = "视频导出没有完成"
        $summary = "已经完成的字幕和进度会保留。"
        $nextStep = "查看详情中的原因，修好后可以继续导出。"
    }
    elseif ($ErrorText -match "(?i)(validation|technical check|字幕.*结构|non-positive-duration|job_completed|JSONL|协议)") {
        $title = "结果还需要检查"
        $summary = "目前无法确认这次任务已经完成。"
        $nextStep = "先保留文件，在详情里查看原因后继续处理。"
    }
    return [pscustomobject]@{
        stage = $Stage
        stage_label = $StageLabel
        title = $title
        summary = $summary
        next_step = $nextStep
    }
}

Set-StrictMode -Version Latest

function Get-TranslationEndpoints([string]$ProviderId) {
    switch ($ProviderId) {
        "qwen" { return @(
            [pscustomobject]@{ Tag="qwen-cn"; Label="中国内地"; Host="dashscope.aliyuncs.com" },
            [pscustomobject]@{ Tag="qwen-intl"; Label="国际"; Host="dashscope-intl.aliyuncs.com" }) }
        "kimi" { return @(
            [pscustomobject]@{ Tag="kimi-cn"; Label="中国内地"; Host="api.moonshot.cn" },
            [pscustomobject]@{ Tag="kimi-intl"; Label="国际"; Host="api.moonshot.ai" }) }
        "openai" { return @([pscustomobject]@{ Tag="openai"; Label="官方"; Host="api.openai.com" }) }
        "anthropic" { return @([pscustomobject]@{ Tag="anthropic"; Label="官方"; Host="api.anthropic.com" }) }
        "gemini" { return @([pscustomobject]@{ Tag="gemini"; Label="官方"; Host="generativelanguage.googleapis.com" }) }
        default { return @() }
    }
}

function Get-TranslationProviderSettings([string]$ProviderId, [string]$Model = "",
    [string]$EndpointProfile = "", [int]$MaxOutputTokens = 8192, [switch]$AllowIncomplete) {
    if ($ProviderId -eq "codex") {
        if ($Model -and $Model -ne "cli-default") { throw "不支持的 Codex 模型设置：$Model" }
        return [pscustomobject]@{ Id = "codex"; Host = "chatgpt.com"; Model = "cli-default" }
    }
    if ($ProviderId -eq "deepseek") {
        if (-not $Model) { $Model = "deepseek-v4-pro" }
        if ($Model -notin @("deepseek-v4-pro", "deepseek-v4-flash")) {
            throw "不支持的 DeepSeek 模型：$Model"
        }
        return [pscustomobject]@{ Id = "deepseek"; Host = "api.deepseek.com"; Model = $Model }
    }
    $endpoints = @(Get-TranslationEndpoints $ProviderId)
    if ($endpoints.Count -eq 0) { throw "不支持的翻译方式：$ProviderId" }
    if (-not $EndpointProfile) { $EndpointProfile = $endpoints[0].Tag }
    $endpoint = $endpoints | Where-Object { $_.Tag -eq $EndpointProfile } | Select-Object -First 1
    if ($null -eq $endpoint) { throw "服务地域与所选翻译方式不匹配" }
    if ((-not $AllowIncomplete -and [string]::IsNullOrWhiteSpace($Model)) -or
        $Model -ne $Model.Trim() -or $Model -match '[\x00-\x1f]') {
        throw "请填写该服务支持的准确模型 ID。"
    }
    if ($MaxOutputTokens -le 0) { throw "输出 token 上限必须为正整数。" }
    return [pscustomobject]@{ Id=$ProviderId; Host=$endpoint.Host; Model=$Model;
        EndpointProfile=$EndpointProfile; MaxOutputTokens=$MaxOutputTokens }
}

function Request-TranslationFlowCancellation([string]$RequestPath) {
    $path = [IO.Path]::GetFullPath($RequestPath)
    $request = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $modeProperty = $request.PSObject.Properties["workflow_mode"]
    $mode = $(if ($null -eq $modeProperty) { "translation" } else { [string]$modeProperty.Value })
    $directory = [IO.Path]::GetDirectoryName($path)
    $recordRoot = [IO.Path]::GetFileName([IO.Path]::GetDirectoryName($directory))
    $validMode = ($mode -eq "translation" -and $recordRoot -eq ".translation-flow") -or
        ($mode -eq "srt_translation" -and $recordRoot -eq ".srt-translation")
    if ([IO.Path]::GetFileName($path) -ne "request.json" -or -not $validMode -or
        [string]$request.provider.id -notin @("codex","deepseek","qwen","kimi","openai","anthropic","gemini") -or
        [IO.Path]::GetFileName($directory) -ne [string]$request.job_id -or
        [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($directory))) -ne [IO.Path]::GetFullPath([string]$request.output_dir)) {
        throw "只能停止当前正式字幕任务。"
    }
    $cancelPath = Join-Path $directory "cancel.requested"
    [IO.File]::WriteAllText($cancelPath, "stop requested", (New-Object Text.UTF8Encoding($false)))
    return $cancelPath
}

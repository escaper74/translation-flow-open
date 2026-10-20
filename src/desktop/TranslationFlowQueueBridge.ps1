# JSON stdin/stdout adapter. Disposing this short-lived client does not stop jobs.
function Invoke-TranslationFlowQueue {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StateDirectory,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Payload
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $PythonPath
    # Only the public state directory is an argument. Keys go through stdin.
    if ($StateDirectory.Contains('"')) { throw "队列目录包含无效字符。" }
    $statePath = [IO.Path]::GetFullPath($StateDirectory)
    $quotedPath = $statePath -replace '(\\+)$', '$1$1'
    $info.Arguments = '-m translation_flow.queue_cli --state-dir "' + $quotedPath + '"'
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $info.StandardOutputEncoding = $encoding
    $info.StandardErrorEncoding = $encoding
    # Some Windows launchers inherit both Path and PATH. .NET Framework's first
    # getter throws while copying them; rebuild its initialized dictionary once.
    try { $environment = $info.get_EnvironmentVariables() }
    catch {
        if ($_.Exception.InnerException -isnot [ArgumentException]) { throw }
        $environment = $info.get_EnvironmentVariables()
        $environment.Clear()
        foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
            $environment[[string]$entry.Key] = [string]$entry.Value
        }
    }
    $environment['PYTHONIOENCODING'] = 'utf-8'
    $environment.Remove('SUBTITLE_API_KEY')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw "无法启动本机队列客户端。" }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        $inputBytes = $encoding.GetBytes(($Payload | ConvertTo-Json -Depth 12 -Compress))
        $inputStream = $process.StandardInput.BaseStream
        $inputStream.Write($inputBytes, 0, $inputBytes.Length)
        $inputStream.Flush()
        $inputStream.Close()
        if (-not $process.WaitForExit(15000)) {
            # Kill this RPC client only; the independent worker continues.
            $process.Kill()
            throw "队列暂未响应；任务可能已接收，请刷新状态后再操作。"
        }
        $output = $outTask.GetAwaiter().GetResult()
        [void]$errTask.GetAwaiter().GetResult()
        try { $result = $output | ConvertFrom-Json }
        catch { throw "后台返回无法识别的状态；请刷新任务列表。" }
        if (-not $result.ok) { throw [string]$result.error }
        return $result.queue
    }
    finally { $process.Dispose() }
}

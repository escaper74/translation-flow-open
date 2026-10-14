# Windows PowerShell 5.1 hosts WinRT OCR locally; images never leave this PC.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
    $language = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Where-Object { $_.LanguageTag -like 'zh*' }) | Select-Object -First 1
    if ($null -eq $language) { throw '本机未安装中文 OCR，烧录字幕暂未检查。' }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1 } | Select-Object -First 1
    function Await-Ocr($Operation, [Type]$Type) {
        $task = $asTask.MakeGenericMethod($Type).Invoke($null, @($Operation))
        try { if (-not $task.Wait(10000)) { throw '本地 OCR 超时。' } }
        catch { if ($null -ne $task.Exception) { throw $task.Exception.GetBaseException() }; throw }
        return $task.Result
    }
    $paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $frames = @()
    foreach ($path in $paths) {
        $file = Await-Ocr ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await-Ocr ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $bitmap = $null
        try {
            $decoder = Await-Ocr ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-Ocr ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Await-Ocr ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @()
            foreach ($line in $result.Lines) {
                $left = ($line.Words | ForEach-Object { $_.BoundingRect.X } | Measure-Object -Minimum).Minimum
                $right = ($line.Words | ForEach-Object { $_.BoundingRect.X + $_.BoundingRect.Width } | Measure-Object -Maximum).Maximum
                $center = ($left + $right) / 2 / $bitmap.PixelWidth
                if ($center -ge 0.15 -and $center -le 0.85 -and ($right-$left) / $bitmap.PixelWidth -ge 0.1) {
                    $lines += @{ text=$line.Text }
                }
            }
            $frames += @{ lines=@($lines) }
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            $stream.Dispose()
        }
    }
    @{status='ok';frames=@($frames)} | ConvertTo-Json -Depth 6 -Compress
} catch {
    @{status='unavailable';message=$_.Exception.Message;frames=@()} | ConvertTo-Json -Compress
}

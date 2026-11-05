param(
    [string]$PythonPath = '',
    [string]$ToolRoot = '',
    [ValidateSet('upscayl-lite-4x', 'nomosuni-otf-medium', 'all')]
    [string]$Model = 'all'
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$root = $sourceRoot
if ((Split-Path -Leaf $sourceRoot) -eq 'src') { $root = Split-Path -Parent $sourceRoot }
$config = Join-Path $root 'config/local.psd1'
if (Test-Path -LiteralPath $config) {
    $localConfig = Import-PowerShellDataFile -LiteralPath $config
    if (-not $PythonPath -and $localConfig.ContainsKey('PythonPath')) { $PythonPath = $localConfig.PythonPath }
    if (-not $ToolRoot -and $localConfig.ContainsKey('ToolRoot')) { $ToolRoot = $localConfig.ToolRoot }
}
if (-not $PythonPath) { $PythonPath = $env:TRANSLATION_FLOW_PYTHON }
if (-not $PythonPath -and (Test-Path -LiteralPath (Join-Path $root '.venv/Scripts/python.exe'))) {
    $PythonPath = Join-Path $root '.venv/Scripts/python.exe'
}
if (-not $PythonPath) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $found) { throw '请先配置本机 PythonPath，再安装超分组件。' }
    $PythonPath = $found.Source
}
$previousPythonPath = $env:PYTHONPATH
Push-Location -LiteralPath $root
try {
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = $sourceRoot
    if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
    $arguments = @('-B', '-m', 'video_enhancement.setup', '--model', $Model)
    if ($env:TRANSLATION_FLOW_SR_ROOT) {
        $arguments += @('--destination', $env:TRANSLATION_FLOW_SR_ROOT)
    } elseif ($ToolRoot) {
        $arguments += @('--destination', (Join-Path $ToolRoot 'super-resolution'))
    }
    & $PythonPath @arguments
    if ($LASTEXITCODE) { throw '超分组件下载未完成；已有文件保留。' }
} finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}

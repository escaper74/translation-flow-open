[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,
    [Parameter(Mandatory = $true)]
    [string]$OutputVideo,
    [ValidateSet("true", "false")]
    [string]$EnableDemosaic = "false",
    [ValidateSet("true", "false")]
    [string]$EnableQualityEnhancement = "false",
    [ValidateSet("true", "false")]
    [string]$EnableFrameInterpolation2x = "false",
    [string]$ToolRoot = "",
    [string]$JobDir = "",
    [ValidateSet("true", "false")]
    [string]$CleanupOnSuccess = "true",
    [int]$RifeChunkFrames = 900,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8NoBom
[Console]::InputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = "utf-8"

if (-not $ToolRoot) {
    $ToolRoot = Join-Path $RepoRoot "workflow\tools"
}
if (-not $Python) {
    $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $BundledPython -PathType Leaf) {
        $Python = $BundledPython
    }
    else {
        $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $PythonCommand) {
            throw "Python not found. Pass -Python with an explicit local python.exe path."
        }
        $Python = $PythonCommand.Source
    }
}

$Arguments = @(
    "-m", "video_pipeline.cli",
    "--input-video", $InputVideo,
    "--output-video", $OutputVideo,
    "--enable-demosaic", $EnableDemosaic.ToLowerInvariant(),
    "--enable-quality-enhancement", $EnableQualityEnhancement.ToLowerInvariant(),
    "--enable-frame-interpolation-2x", $EnableFrameInterpolation2x.ToLowerInvariant(),
    "--tool-root", $ToolRoot,
    "--cleanup-on-success", $CleanupOnSuccess.ToLowerInvariant(),
    "--rife-chunk-frames", "$RifeChunkFrames"
)
if ($JobDir) {
    $Arguments += @("--job-dir", $JobDir)
}

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = (Join-Path $RepoRoot "src") + [IO.Path]::PathSeparator + $previousPythonPath
Push-Location $RepoRoot
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}

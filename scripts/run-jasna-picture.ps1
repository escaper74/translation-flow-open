[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$PythonPath,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunnerArguments
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = (Join-Path $scriptRoot "src") + [IO.Path]::PathSeparator + $previousPythonPath
Push-Location $scriptRoot
try {
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
            throw "Python executable does not exist: $PythonPath"
        }
        $resolvedPython = $PythonPath
    }
    else {
        $candidates = @(
            (Join-Path $scriptRoot ".venv\Scripts\python.exe"),
            (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
        )
        $resolvedPython = $candidates |
            Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
            Select-Object -First 1
        if (-not $resolvedPython) {
            $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
            if ($pythonCommand) {
                $resolvedPython = $pythonCommand.Source
            }
        }
        if (-not $resolvedPython) {
            throw "No usable Python found. Pass -PythonPath explicitly; this workflow uses only the Python standard library."
        }
    }
    & $resolvedPython -m jasna_picture.cli @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}

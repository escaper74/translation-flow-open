[CmdletBinding()]
param(
    [string]$LabRoot = "",
    [string]$BootstrapPython = "",
    [switch]$SkipModel
)

$ErrorActionPreference = "Stop"

if (-not $LabRoot) {
    $DependencyRoot = $env:TRANSLATION_FLOW_TOOL_ROOT
    if (-not $DependencyRoot) { $DependencyRoot = Join-Path $PSScriptRoot "..\..\workflow\tools" }
    $LabRoot = [IO.Path]::GetFullPath((Join-Path $DependencyRoot "asr-lab"))
}

$Requirements = Join-Path $PSScriptRoot "requirements-faster-whisper.lock.txt"
$SnapshotDownloader = Join-Path $PSScriptRoot "download_huggingface_snapshot.py"
$VenvRoot = Join-Path $LabRoot "venvs\faster-whisper-1.2.1"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$CacheRoot = Join-Path $LabRoot "cache\huggingface"
$PipCacheRoot = Join-Path $LabRoot "cache\pip"
$TempRoot = Join-Path $LabRoot "tmp"
$ModelRoot = Join-Path $LabRoot "models\faster-whisper-large-v3"
$ModelRevision = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
$ManifestPath = Join-Path $LabRoot "asr-fusion-installation-manifest.json"

$ExpectedModelFiles = [ordered]@{
    ".gitattributes" = [pscustomobject]@{ bytes = 1519; sha256 = "11AD7EFA24975EE4B0C3C3A38ED18737F0658A5F75A0A96787B576A78A023361" }
    "config.json" = [pscustomobject]@{ bytes = 2394; sha256 = "A9306624F5EC14270A014B647E5C316B6E03A662C369758D1B90697A7B0655B9" }
    "model.bin" = [pscustomobject]@{ bytes = 3087284237; sha256 = "69F74147E3334731BC3A76048724833325D2EC74642FB52620EDA87352E3D4F1" }
    "preprocessor_config.json" = [pscustomobject]@{ bytes = 340; sha256 = "7CCC62C6F2765AF1F3B46C00C9B5894426835A05021C8B9C01EECB6DFB542711" }
    "README.md" = [pscustomobject]@{ bytes = 2052; sha256 = "39E96252229F5A3D0141DC81AFB65A36FD205461AC21E5B70F2CD1248EF0082C" }
    "tokenizer.json" = [pscustomobject]@{ bytes = 2480617; sha256 = "6D8CBD7CD0D8D5815E478DAC67B85A26BBE77C1F5E0C6D76D1CE2ABC0E5F21CA" }
    "vocabulary.json" = [pscustomobject]@{ bytes = 1068114; sha256 = "C69260F2AB26D659B7C398F9A2B2B48ED0DF16C3B47D7326782FD9CBA71690C1" }
}

function Resolve-Python {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Bootstrap Python not found: $Requested"
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $ProjectPython = Join-Path $PSScriptRoot "..\..\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ProjectPython -PathType Leaf) { return (Resolve-Path -LiteralPath $ProjectPython).Path }
    $Bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $Bundled -PathType Leaf) { return $Bundled }
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Command) { throw "Python was not found. Pass -BootstrapPython with an explicit python.exe path." }
    return $Command.Source
}

function Write-Utf8Json {
    param([object]$Value, [string]$Path)
    $Json = $Value | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($Path, $Json + "`n", [Text.UTF8Encoding]::new($false))
}

$BootstrapPython = Resolve-Python -Requested $BootstrapPython
foreach ($RequiredPath in @($Requirements, $SnapshotDownloader)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file not found: $RequiredPath"
    }
}

New-Item -ItemType Directory -Force -Path $LabRoot, $CacheRoot, $PipCacheRoot, $TempRoot | Out-Null
$OldPipCache = $env:PIP_CACHE_DIR
$OldTemp = $env:TEMP
$OldTmp = $env:TMP
try {
    # Process-local only. This never changes the user or machine environment.
    $env:PIP_CACHE_DIR = $PipCacheRoot
    $env:TEMP = $TempRoot
    $env:TMP = $TempRoot

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Host "[venv] creating isolated environment: $VenvRoot"
        & $BootstrapPython -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) { throw "Python venv creation failed: $LASTEXITCODE" }
    }

    Write-Host "[venv] installing pinned faster-whisper/CUDA dependencies"
    & $VenvPython -m pip install --disable-pip-version-check --no-input --timeout 120 --retries 10 --requirement $Requirements
    if ($LASTEXITCODE -ne 0) { throw "pip installation failed: $LASTEXITCODE" }

    if (-not $SkipModel) {
        $ModelSentinel = Join-Path $ModelRoot "model.bin"
        if (-not (Test-Path -LiteralPath $ModelSentinel -PathType Leaf)) {
            Write-Host "[model] downloading pinned faster-whisper large-v3 revision $ModelRevision"
            New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
            $OldHfHome = $env:HF_HOME
            $OldHfHubCache = $env:HF_HUB_CACHE
            $OldTelemetry = $env:HF_HUB_DISABLE_TELEMETRY
            try {
                $env:HF_HOME = $CacheRoot
                $env:HF_HUB_CACHE = Join-Path $CacheRoot "hub"
                $env:HF_HUB_DISABLE_TELEMETRY = "1"
                & $VenvPython $SnapshotDownloader --repo-id "Systran/faster-whisper-large-v3" --revision $ModelRevision --local-dir $ModelRoot
                if ($LASTEXITCODE -ne 0) { throw "model download failed: $LASTEXITCODE" }
            } finally {
                $env:HF_HOME = $OldHfHome
                $env:HF_HUB_CACHE = $OldHfHubCache
                $env:HF_HUB_DISABLE_TELEMETRY = $OldTelemetry
            }
        }
    }

    $VerifiedModelFiles = @()
    if (-not $SkipModel -or (Test-Path -LiteralPath $ModelRoot -PathType Container)) {
        foreach ($Name in $ExpectedModelFiles.Keys) {
            $Expected = $ExpectedModelFiles[$Name]
            $Path = Join-Path $ModelRoot $Name
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Pinned model file missing: $Path" }
            $Item = Get-Item -LiteralPath $Path
            if ($Item.Length -ne $Expected.bytes) { throw "Pinned model size mismatch: $Path" }
            $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
            if ($Hash -ne $Expected.sha256) { throw "Pinned model hash mismatch: $Path" }
            $VerifiedModelFiles += [pscustomobject]@{ path = $Name; bytes = $Item.Length; sha256 = $Hash }
        }
    }

    $Packages = @(& $VenvPython -m pip freeze --disable-pip-version-check)
    if ($LASTEXITCODE -ne 0) { throw "pip freeze failed: $LASTEXITCODE" }
    Write-Utf8Json -Path $ManifestPath -Value ([ordered]@{
        installed_at = (Get-Date).ToString("o")
        lab_root = (Resolve-Path -LiteralPath $LabRoot).Path
        python = $VenvPython
        requirements_sha256 = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
        packages = $Packages
        model = [ordered]@{
            repo_id = "Systran/faster-whisper-large-v3"
            revision = $ModelRevision
            root = $ModelRoot
            files = $VerifiedModelFiles
        }
    })
} finally {
    $env:PIP_CACHE_DIR = $OldPipCache
    $env:TEMP = $OldTemp
    $env:TMP = $OldTmp
}

Write-Host "[done] dependency manifest: $ManifestPath"
Write-Host "[done] faster-whisper Python: $VenvPython"

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,
    [string]$JobId = "",
    [string]$LabRoot = "",
    [string]$CacheRoot = "",
    [string]$DependencyRoot = "",
    [string]$Python = "",
    [ValidateRange(1, 64)]
    [int]$Threads = 8
)

$ErrorActionPreference = "Stop"

if (-not $DependencyRoot) {
    $DependencyRoot = $env:TRANSLATION_FLOW_TOOL_ROOT
    if (-not $DependencyRoot) { $DependencyRoot = Join-Path $RepoRoot "workflow\tools" }
}
if (-not $LabRoot) { $LabRoot = Join-Path $DependencyRoot "asr-lab" }

$PipelineVersion = "fusion-ac-v1"
$ExpectedFfmpegSha256 = "228D7A8556258DE907FDB55F36850078EBC7680B84EC30D84EA02E99BEC1D1EB"
$ExpectedFfprobeSha256 = "0FDE260F5ABD35C9CAFD96F594CC76365A780C1B73A90E35B6A3409EA1DB1BF0"
$ExpectedWhisperExeSha256 = "42DDFA60D45CC0A0422A0DCB75D11799DABCA47636C3BD74F64E4666A8179505"
$ExpectedWhisperModelSha256 = "64D182B440B98D5203C4F9BD541544D84C605196C4F7B845DFA11FB23594D1E2"
$ExpectedFasterModelSha256 = "69F74147E3334731BC3A76048724833325D2EC74642FB52620EDA87352E3D4F1"

function Assert-File {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required file not found: $Path" }
}

function Resolve-Python {
    param([string]$Requested)
    if ($Requested) {
        Assert-File $Requested
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $ProjectPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ProjectPython -PathType Leaf) { return (Resolve-Path -LiteralPath $ProjectPython).Path }
    $Bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $Bundled -PathType Leaf) { return $Bundled }
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Command) { throw "Python was not found. Pass -Python with an explicit python.exe path." }
    return $Command.Source
}

function Invoke-Checked {
    param([string]$Label, [string]$FilePath, [object[]]$Arguments)
    Write-Host "[$Label]"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

function Invoke-ReportCheck {
    param([string]$Label, [string]$FilePath, [object[]]$Arguments, [string]$ReportPath)
    Write-Host "[$Label]"
    & $FilePath @Arguments
    $ExitCode = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "$Label did not write its report (exit code $ExitCode)"
    }
    if ($ExitCode -ne 0) { Write-Warning "$Label reported failures; the report is preserved" }
}

function Write-Utf8Json {
    param([object]$Value, [string]$Path)
    $Json = $Value | ConvertTo-Json -Depth 16
    [IO.File]::WriteAllText($Path, $Json + "`n", [Text.UTF8Encoding]::new($false))
}

function Get-StringSha256 {
    param([string]$Value)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "")
    } finally {
        $Hasher.Dispose()
    }
}

function New-AsciiBridge {
    param([string]$Target)
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $TempParent = Join-Path $env:LOCALAPPDATA "Temp"
    $Base = Join-Path $TempParent ("asr_fusion_" + $PID + "_" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $Base | Out-Null
    $WorkLink = Join-Path $Base "work"
    New-Item -ItemType Junction -Path $WorkLink -Target $Target | Out-Null
    return [pscustomobject]@{ Base = $Base; Work = $WorkLink; ExpectedParent = $TempParent }
}

function Remove-AsciiBridge {
    param([object]$Bridge)
    $ResolvedBase = [IO.Path]::GetFullPath($Bridge.Base)
    $ResolvedParent = [IO.Path]::GetFullPath($Bridge.ExpectedParent)
    if (-not ([IO.Path]::GetDirectoryName($ResolvedBase)).Equals($ResolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean unexpected bridge path: $ResolvedBase"
    }
    if (-not ([IO.Path]::GetFileName($ResolvedBase)).StartsWith("asr_fusion_")) {
        throw "Refusing to clean bridge without the asr_fusion_ prefix: $ResolvedBase"
    }
    if (Test-Path -LiteralPath $Bridge.Work) {
        $Item = Get-Item -LiteralPath $Bridge.Work -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            throw "Refusing to remove bridge because it is not a reparse point: $($Bridge.Work)"
        }
        [IO.Directory]::Delete($Bridge.Work, $false)
    }
    if (Test-Path -LiteralPath $ResolvedBase) { [IO.Directory]::Delete($ResolvedBase, $false) }
}

function Assert-ArtifactSet {
    param([string]$Label, [string[]]$Paths)
    $Existing = @($Paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($Existing.Count -gt 0 -and $Existing.Count -lt $Paths.Count) {
        throw "$Label has a partial artifact set; refusing to overwrite. Inspect it or choose another -JobId."
    }
    return $Existing.Count -eq $Paths.Count
}

function Read-Json {
    param([string]$Path)
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json
}

function Write-ArtifactFingerprint {
    param([string]$Path, [string]$SourceSha256, [string[]]$Artifacts)
    $Files = @()
    foreach ($Artifact in $Artifacts) {
        Assert-File $Artifact
        $Files += [ordered]@{
            path = $Artifact
            sha256 = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash
        }
    }
    Write-Utf8Json -Path $Path -Value ([ordered]@{
        source_sha256 = $SourceSha256
        files = $Files
    })
}

function Assert-ArtifactFingerprint {
    param([string]$Label, [string]$Path, [string]$SourceSha256)
    Assert-File $Path
    $Fingerprint = Read-Json -Path $Path
    if ($Fingerprint.source_sha256 -ne $SourceSha256) {
        throw "$Label source hash mismatch in stage fingerprint: $Path"
    }
    foreach ($File in @($Fingerprint.files)) {
        Assert-File $File.path
        $Actual = (Get-FileHash -LiteralPath $File.path -Algorithm SHA256).Hash
        if ($Actual -ne $File.sha256) { throw "$Label artifact hash mismatch: $($File.path)" }
    }
}

function Add-ReportProvenance {
    param([string]$ReportPath, [string]$InputPath, [string]$OutputPath = "")
    $Report = Read-Json -Path $ReportPath
    $Report | Add-Member -NotePropertyName input_sha256 -NotePropertyValue ((Get-FileHash -LiteralPath $InputPath -Algorithm SHA256).Hash) -Force
    if ($OutputPath) {
        $Report | Add-Member -NotePropertyName output_sha256 -NotePropertyValue ((Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash) -Force
    }
    Write-Utf8Json -Path $ReportPath -Value $Report
}

function Assert-ReportProvenance {
    param([string]$Label, [string]$ReportPath, [string]$InputPath, [string]$OutputPath = "")
    Assert-File $ReportPath
    $Report = Read-Json -Path $ReportPath
    $ExpectedInput = (Get-FileHash -LiteralPath $InputPath -Algorithm SHA256).Hash
    if (-not $Report.input_sha256 -or $Report.input_sha256 -ne $ExpectedInput) {
        throw "$Label input hash mismatch or missing provenance: $ReportPath"
    }
    if ($OutputPath) {
        $ExpectedOutput = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash
        if (-not $Report.output_sha256 -or $Report.output_sha256 -ne $ExpectedOutput) {
            throw "$Label output hash mismatch or missing provenance: $ReportPath"
        }
    }
}

$InputCandidate = $InputVideo
Assert-File $InputCandidate
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$InputVideo = (Resolve-Path -LiteralPath $InputCandidate).Path
$Python = Resolve-Python -Requested $Python
if ([string]::IsNullOrWhiteSpace($CacheRoot)) {
    $CacheRoot = Join-Path $LabRoot "cache"
}
$CacheRoot = [IO.Path]::GetFullPath($CacheRoot)
New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null

$Ffmpeg = Join-Path $DependencyRoot "ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"
$Ffprobe = Join-Path $DependencyRoot "ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffprobe.exe"
$Whisper = Join-Path $DependencyRoot "whisper-cublas-12.4.0-bin-x64\Release\whisper-cli.exe"
$WhisperModel = Join-Path $DependencyRoot "LLPlayer-v0.3.0\whispermodels\ggml-large-v3.bin"
$FasterModel = Join-Path $LabRoot "models\faster-whisper-large-v3"
$FasterModelBin = Join-Path $FasterModel "model.bin"
$FasterPython = Join-Path $LabRoot "venvs\faster-whisper-1.2.1\Scripts\python.exe"

$FrozenTranscriber = Join-Path $RepoRoot "workflow\scripts\transcribe_whisper_chunked.py"
$FrozenCleaner = Join-Path $RepoRoot "workflow\scripts\clean_whisper_srt.py"
$FrozenQuality = Join-Path $RepoRoot "workflow\scripts\validate_srt_quality.py"
$CTranscriber = Join-Path $PSScriptRoot "transcribe_faster_whisper.py"
$StructureValidator = Join-Path $PSScriptRoot "validate_asr_srt.py"
$Fusion = Join-Path $PSScriptRoot "fuse_asr_tracks.py"

foreach ($Path in @(
    $InputVideo, $Python, $Ffmpeg, $Ffprobe, $Whisper, $WhisperModel, $FasterModelBin,
    $FasterPython, $FrozenTranscriber, $FrozenCleaner, $FrozenQuality,
    $CTranscriber, $StructureValidator, $Fusion
)) { Assert-File $Path }

$Hashes = [ordered]@{
    ffmpeg = (Get-FileHash -LiteralPath $Ffmpeg -Algorithm SHA256).Hash
    ffprobe = (Get-FileHash -LiteralPath $Ffprobe -Algorithm SHA256).Hash
    whisper_exe = (Get-FileHash -LiteralPath $Whisper -Algorithm SHA256).Hash
    whisper_model = (Get-FileHash -LiteralPath $WhisperModel -Algorithm SHA256).Hash
    faster_model = (Get-FileHash -LiteralPath $FasterModelBin -Algorithm SHA256).Hash
}
$ExpectedHashes = [ordered]@{
    ffmpeg = $ExpectedFfmpegSha256
    ffprobe = $ExpectedFfprobeSha256
    whisper_exe = $ExpectedWhisperExeSha256
    whisper_model = $ExpectedWhisperModelSha256
    faster_model = $ExpectedFasterModelSha256
}
foreach ($Name in $ExpectedHashes.Keys) {
    if ($Hashes[$Name] -ne $ExpectedHashes[$Name]) {
        throw "$Name hash mismatch: expected $($ExpectedHashes[$Name]), got $($Hashes[$Name])"
    }
}

$InputItem = Get-Item -LiteralPath $InputVideo
$InputHash = (Get-FileHash -LiteralPath $InputVideo -Algorithm SHA256).Hash
$DurationText = & $Ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $InputVideo
if ($LASTEXITCODE -ne 0) { throw "ffprobe failed for input video" }
$InputDuration = [double]::Parse($DurationText.Trim(), [Globalization.CultureInfo]::InvariantCulture)

if (-not $JobId) {
    $Stem = [IO.Path]::GetFileNameWithoutExtension($InputVideo) -replace '[^\p{L}\p{Nd}._-]+', '-'
    $Stem = $Stem.Trim('-', '.', '_')
    if (-not $Stem) { $Stem = "job" }
    if ($Stem.Length -gt 48) { $Stem = $Stem.Substring(0, 48).Trim('-', '.', '_') }
    $JobId = "$Stem-$($InputHash.Substring(0, 12).ToLowerInvariant())"
}
if ($JobId -in @('.', '..') -or $JobId -notmatch '^[\p{L}\p{Nd}][\p{L}\p{Nd}._-]{0,79}$') {
    throw "Invalid -JobId. Use 1-80 letters, digits, dot, underscore, or hyphen; path separators are forbidden."
}

$OutputBase = [IO.Path]::GetFullPath((Join-Path $RepoRoot "workflow\output\asr-fusion"))
$JobRoot = [IO.Path]::GetFullPath((Join-Path $OutputBase $JobId))
if (-not ([IO.Path]::GetDirectoryName($JobRoot)).Equals($OutputBase, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing output outside workflow/output/asr-fusion: $JobRoot"
}

$Settings = [ordered]@{
    threads = $Threads
    a_chunk_seconds = 600
    c = [ordered]@{
        beam_size = 5
        vad_threshold = 0.35
        min_speech_ms = 100
        min_silence_ms = 500
        speech_pad_ms = 250
        max_chars = 28
        max_line_chars = 16
        max_duration_seconds = 6.0
        max_gap_seconds = 0.65
    }
    fusion = [ordered]@{
        backbone = "A"
        c_policy = "unique one-to-one high-confidence text replacement"
        preserve_a_timeline = $true
        missing_c_never_removes_a = $true
    }
}
$ScriptHashes = [ordered]@{
    run = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash
    frozen_transcriber = (Get-FileHash -LiteralPath $FrozenTranscriber -Algorithm SHA256).Hash
    frozen_cleaner = (Get-FileHash -LiteralPath $FrozenCleaner -Algorithm SHA256).Hash
    frozen_quality = (Get-FileHash -LiteralPath $FrozenQuality -Algorithm SHA256).Hash
    c_transcriber = (Get-FileHash -LiteralPath $CTranscriber -Algorithm SHA256).Hash
    structure_validator = (Get-FileHash -LiteralPath $StructureValidator -Algorithm SHA256).Hash
    fusion = (Get-FileHash -LiteralPath $Fusion -Algorithm SHA256).Hash
}
$FingerprintMaterial = [ordered]@{
    pipeline_version = $PipelineVersion
    source_sha256 = $InputHash
    source_bytes = $InputItem.Length
    source_duration_seconds = $InputDuration
    cache_root = $CacheRoot
    tools = $Hashes
    scripts = $ScriptHashes
    settings = $Settings
}
$LegacyFingerprintMaterial = [ordered]@{
    pipeline_version = $PipelineVersion
    source_sha256 = $InputHash
    source_bytes = $InputItem.Length
    source_duration_seconds = $InputDuration
    tools = $Hashes
    scripts = $ScriptHashes
    settings = $Settings
}
$FingerprintId = Get-StringSha256 -Value ($FingerprintMaterial | ConvertTo-Json -Depth 12 -Compress)
$LegacyFingerprintId = Get-StringSha256 -Value ($LegacyFingerprintMaterial | ConvertTo-Json -Depth 12 -Compress)
$FingerprintPath = Join-Path $JobRoot "task-fingerprint.json"
$JobRunPath = Join-Path $JobRoot "job.run.json"

if (Test-Path -LiteralPath $JobRoot -PathType Container) {
    $Existing = @(Get-ChildItem -LiteralPath $JobRoot -Force)
    if ($Existing.Count -gt 0 -and -not (Test-Path -LiteralPath $FingerprintPath -PathType Leaf)) {
        throw "Existing job directory has no task fingerprint; refusing to reuse: $JobRoot"
    }
}
New-Item -ItemType Directory -Force -Path $JobRoot | Out-Null
if (Test-Path -LiteralPath $FingerprintPath -PathType Leaf) {
    $ExistingFingerprint = Read-Json -Path $FingerprintPath
    if ($ExistingFingerprint.fingerprint_id -ne $FingerprintId) {
        $ToolsMatch = (
            $ExistingFingerprint.tools -and
            (($ExistingFingerprint.tools | ConvertTo-Json -Depth 12 -Compress) -eq ($Hashes | ConvertTo-Json -Depth 12 -Compress))
        )
        $SettingsMatch = (
            $ExistingFingerprint.settings -and
            (($ExistingFingerprint.settings | ConvertTo-Json -Depth 12 -Compress) -eq ($Settings | ConvertTo-Json -Depth 12 -Compress))
        )
        $ScriptsMatchExceptRun = $true
        foreach ($ScriptName in $ScriptHashes.Keys) {
            if ($ScriptName -eq "run") { continue }
            if ($ExistingFingerprint.scripts.$ScriptName -ne $ScriptHashes[$ScriptName]) {
                $ScriptsMatchExceptRun = $false
                break
            }
        }
        $CanMigrateCacheRoot = (
            (-not $ExistingFingerprint.cache_root) -and
            (-not (Test-Path -LiteralPath $JobRunPath -PathType Leaf)) -and
            ($ExistingFingerprint.job_id -eq $JobId) -and
            ($ExistingFingerprint.pipeline_version -eq $PipelineVersion) -and
            ($ExistingFingerprint.source.sha256 -eq $InputHash) -and
            ($ExistingFingerprint.source.bytes -eq $InputItem.Length) -and
            $ToolsMatch -and
            $SettingsMatch -and
            $ScriptsMatchExceptRun
        )
        if (-not $CanMigrateCacheRoot) {
            throw "Task fingerprint mismatch; use another -JobId. Existing=$($ExistingFingerprint.fingerprint_id), current=$FingerprintId"
        }
        $LegacyFingerprintPath = Join-Path $JobRoot "task-fingerprint.legacy.json"
        if (-not (Test-Path -LiteralPath $LegacyFingerprintPath -PathType Leaf)) {
            Copy-Item -LiteralPath $FingerprintPath -Destination $LegacyFingerprintPath
        }
        Write-Utf8Json -Path $FingerprintPath -Value ([ordered]@{
            schema_version = 1
            fingerprint_id = $FingerprintId
            job_id = $JobId
            migrated_from_cache_rootless_fingerprint = $ExistingFingerprint.fingerprint_id
            cache_root = $CacheRoot
            source = [ordered]@{
                path = $InputVideo
                bytes = $InputItem.Length
                duration_seconds = $InputDuration
                sha256 = $InputHash
            }
            pipeline_version = $PipelineVersion
            tools = $Hashes
            scripts = $ScriptHashes
            settings = $Settings
            resources = [ordered]@{
                gpu_exclusive = $true
                scheduler_contract = "serialize with all other GPU jobs, including Lada and RIFE"
            }
            external_effects = [ordered]@{
                translation = $false
                mux = $false
                video_processing = $false
                media_upload = $false
            }
        })
    }
} else {
    Write-Utf8Json -Path $FingerprintPath -Value ([ordered]@{
        schema_version = 1
        fingerprint_id = $FingerprintId
        job_id = $JobId
        source = [ordered]@{
            path = $InputVideo
            bytes = $InputItem.Length
            duration_seconds = $InputDuration
            sha256 = $InputHash
        }
        pipeline_version = $PipelineVersion
        cache_root = $CacheRoot
        tools = $Hashes
        scripts = $ScriptHashes
        settings = $Settings
        resources = [ordered]@{
            gpu_exclusive = $true
            scheduler_contract = "serialize with all other GPU jobs, including Lada and RIFE"
        }
        external_effects = [ordered]@{
            translation = $false
            mux = $false
            video_processing = $false
            media_upload = $false
        }
    })
}

if (Test-Path -LiteralPath $JobRunPath -PathType Leaf) {
    $ExistingJob = Read-Json -Path $JobRunPath
    if ($ExistingJob.fingerprint_id -ne $FingerprintId) { throw "Completed job fingerprint mismatch: $JobRunPath" }
    $ExpectedOutputs = [ordered]@{
        a_ja_srt = Join-Path $JobRoot "A\A.ja.srt"
        a_quality_report = Join-Path $JobRoot "A\A.quality-report.json"
        a_structure_report = Join-Path $JobRoot "A\A.structure-report.json"
        c_ja_srt = Join-Path $JobRoot "C\C.ja.srt"
        c_quality_report = Join-Path $JobRoot "C\C.quality-report.json"
        c_structure_report = Join-Path $JobRoot "C\C.structure-report.json"
        fusion_ja_srt = Join-Path $JobRoot "fusion\fusion.ja.srt"
        fusion_report = Join-Path $JobRoot "fusion\fusion.report.json"
        fusion_quality_report = Join-Path $JobRoot "fusion\fusion.quality-report.json"
        fusion_structure_report = Join-Path $JobRoot "fusion\fusion.structure-report.json"
    }
    foreach ($Name in $ExpectedOutputs.Keys) {
        $Output = $ExpectedOutputs[$Name]
        Assert-File $Output
        $RecordedProperty = $ExistingJob.output_sha256.PSObject.Properties[$Name]
        if (-not $RecordedProperty) { throw "Completed job is missing recorded output hash: $Name" }
        $Actual = (Get-FileHash -LiteralPath $Output -Algorithm SHA256).Hash
        if ($Actual -ne $RecordedProperty.Value) { throw "Completed job output hash mismatch: $Output" }
    }
    Write-Host "[reuse] completed A+C job: $JobRoot"
    if ($ExistingJob.status -ne "complete") { throw "Reused job completed with gate failures; inspect $JobRunPath" }
    return
}

$OldPipCache = $env:PIP_CACHE_DIR
$OldTemp = $env:TEMP
$OldTmp = $env:TMP
$ProcessPipCache = Join-Path $CacheRoot "pip"
$ProcessTemp = Join-Path $CacheRoot "tmp"
$JobWatch = [Diagnostics.Stopwatch]::StartNew()
try {
    New-Item -ItemType Directory -Force -Path $ProcessPipCache, $ProcessTemp | Out-Null
    $env:PIP_CACHE_DIR = $ProcessPipCache
    $env:TEMP = $ProcessTemp
    $env:TMP = $ProcessTemp

    # A: completeness and timing backbone, using the unchanged frozen transcriber.
    $AOut = Join-Path $JobRoot "A"
    New-Item -ItemType Directory -Force -Path $AOut | Out-Null
    $ATranscription = @(
        (Join-Path $AOut "A.raw.srt"), (Join-Path $AOut "A.transcribed.srt"),
        (Join-Path $AOut "A.txt"), (Join-Path $AOut "A.chunk-report.json")
    )
    $ATranscriptionFingerprint = Join-Path $AOut "A.transcription-fingerprint.json"
    if (-not (Assert-ArtifactSet -Label "A transcription" -Paths ($ATranscription + @($ATranscriptionFingerprint)))) {
        $Cache = Join-Path $CacheRoot "asr-fusion\$($InputHash.Substring(0, 16).ToLowerInvariant())\A"
        $Bridge = New-AsciiBridge -Target $Cache
        try {
            Invoke-Checked -Label "A frozen whisper.cpp transcription" -FilePath $Python -Arguments @(
                $FrozenTranscriber,
                "--input-video", $InputVideo,
                "--output-srt", (Join-Path $AOut "A.transcribed.srt"),
                "--raw-output-srt", (Join-Path $AOut "A.raw.srt"),
                "--output-txt", (Join-Path $AOut "A.txt"),
                "--work-dir", $Bridge.Work,
                "--ffmpeg", $Ffmpeg, "--ffprobe", $Ffprobe,
                "--whisper", $Whisper, "--model", $WhisperModel,
                "--threads", "$Threads", "--chunk-seconds", "600",
                "--report", (Join-Path $AOut "A.chunk-report.json")
            )
            Write-ArtifactFingerprint -Path $ATranscriptionFingerprint -SourceSha256 $InputHash -Artifacts $ATranscription
        } finally {
            Remove-AsciiBridge -Bridge $Bridge
        }
    } else {
        Assert-ArtifactFingerprint -Label "A transcription" -Path $ATranscriptionFingerprint -SourceSha256 $InputHash
        Write-Host "[resume] A transcription artifacts"
    }

    $ASrt = Join-Path $AOut "A.ja.srt"
    $ACleanReport = Join-Path $AOut "A.clean-report.json"
    if (-not (Assert-ArtifactSet -Label "A cleanup" -Paths @($ASrt, $ACleanReport))) {
        Invoke-Checked -Label "A cleanup" -FilePath $Python -Arguments @(
            $FrozenCleaner, "--input", (Join-Path $AOut "A.raw.srt"),
            "--output", $ASrt, "--report", $ACleanReport
        )
        Add-ReportProvenance -ReportPath $ACleanReport -InputPath (Join-Path $AOut "A.raw.srt") -OutputPath $ASrt
    } else {
        Assert-ReportProvenance -Label "A cleanup" -ReportPath $ACleanReport -InputPath (Join-Path $AOut "A.raw.srt") -OutputPath $ASrt
        Write-Host "[resume] A clean subtitle"
    }

    $AQuality = Join-Path $AOut "A.quality-report.json"
    if (-not (Test-Path -LiteralPath $AQuality -PathType Leaf)) {
        Invoke-ReportCheck -Label "A quality gate" -FilePath $Python -ReportPath $AQuality -Arguments @(
            $FrozenQuality, "--input", $ASrt, "--report", $AQuality
        )
        Add-ReportProvenance -ReportPath $AQuality -InputPath $ASrt
    } else { Assert-ReportProvenance -Label "A quality gate" -ReportPath $AQuality -InputPath $ASrt }
    $AStructure = Join-Path $AOut "A.structure-report.json"
    if (-not (Test-Path -LiteralPath $AStructure -PathType Leaf)) {
        Invoke-ReportCheck -Label "A structure gate" -FilePath $Python -ReportPath $AStructure -Arguments @(
            $StructureValidator, "--input", $ASrt, "--source-duration", "$InputDuration", "--report", $AStructure
        )
        Add-ReportProvenance -ReportPath $AStructure -InputPath $ASrt
    } else { Assert-ReportProvenance -Label "A structure gate" -ReportPath $AStructure -InputPath $ASrt }
    $ARun = Join-Path $AOut "A.run.json"
    if (-not (Test-Path -LiteralPath $ARun -PathType Leaf)) {
        $AClean = Read-Json -Path $ACleanReport
        Write-Utf8Json -Path $ARun -Value ([ordered]@{
            group = "A"
            role = "completeness and timing backbone"
            source_sha256 = $InputHash
            srt_sha256 = (Get-FileHash -LiteralPath $ASrt -Algorithm SHA256).Hash
            raw_blocks = $AClean.input_blocks
            cleaned_blocks = $AClean.kept_blocks
            quality_failures = @((Read-Json -Path $AQuality).failures)
            structure_failures = @((Read-Json -Path $AStructure).failures)
        })
    }
    $AState = Read-Json -Path $ARun
    if ($AState.source_sha256 -ne $InputHash -or $AState.srt_sha256 -ne (Get-FileHash -LiteralPath $ASrt -Algorithm SHA256).Hash) {
        throw "A stage run report does not match the current source/subtitle: $ARun"
    }

    # C: local text candidate generator. It never controls the final timing.
    $COut = Join-Path $JobRoot "C"
    New-Item -ItemType Directory -Force -Path $COut | Out-Null
    $CTranscription = @(
        (Join-Path $COut "C.words.json"), (Join-Path $COut "C.model-segments.srt"),
        (Join-Path $COut "C.raw.srt"), (Join-Path $COut "C.txt"),
        (Join-Path $COut "C.resegment-report.json")
    )
    $CTranscriptionFingerprint = Join-Path $COut "C.transcription-fingerprint.json"
    if (-not (Assert-ArtifactSet -Label "C transcription" -Paths ($CTranscription + @($CTranscriptionFingerprint)))) {
        $SitePackages = Join-Path (Split-Path -Parent (Split-Path -Parent $FasterPython)) "Lib\site-packages"
        $RuntimeDirs = @(
            (Join-Path $SitePackages "nvidia\cublas\bin"),
            (Join-Path $SitePackages "nvidia\cudnn\bin"),
            (Join-Path $SitePackages "nvidia\cuda_nvrtc\bin")
        )
        foreach ($RuntimeDir in $RuntimeDirs) {
            if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) { throw "Local CUDA runtime not found: $RuntimeDir" }
        }
        $OldPath = $env:PATH
        $OldHfOffline = $env:HF_HUB_OFFLINE
        $OldHfHome = $env:HF_HOME
        $OldTelemetry = $env:HF_HUB_DISABLE_TELEMETRY
        try {
            $env:PATH = ($RuntimeDirs -join ';') + ";$OldPath"
            $env:HF_HUB_OFFLINE = "1"
            $env:HF_HOME = Join-Path $CacheRoot "huggingface"
            $env:HF_HUB_DISABLE_TELEMETRY = "1"
            Invoke-Checked -Label "C faster-whisper transcription" -FilePath $FasterPython -Arguments @(
                $CTranscriber,
                "--input", $InputVideo, "--model", $FasterModel,
                "--raw-json", (Join-Path $COut "C.words.json"),
                "--model-srt", (Join-Path $COut "C.model-segments.srt"),
                "--output-srt", (Join-Path $COut "C.raw.srt"),
                "--output-txt", (Join-Path $COut "C.txt"),
                "--report", (Join-Path $COut "C.resegment-report.json"),
                "--device", "cuda", "--compute-type", "float16", "--beam-size", "5",
                "--vad-threshold", "0.35", "--min-speech-ms", "100", "--min-silence-ms", "500", "--speech-pad-ms", "250",
                "--max-chars", "28", "--max-line-chars", "16", "--max-duration", "6.0", "--max-gap", "0.65"
            )
            Write-ArtifactFingerprint -Path $CTranscriptionFingerprint -SourceSha256 $InputHash -Artifacts $CTranscription
        } finally {
            $env:PATH = $OldPath
            $env:HF_HUB_OFFLINE = $OldHfOffline
            $env:HF_HOME = $OldHfHome
            $env:HF_HUB_DISABLE_TELEMETRY = $OldTelemetry
        }
    } else {
        Assert-ArtifactFingerprint -Label "C transcription" -Path $CTranscriptionFingerprint -SourceSha256 $InputHash
        Write-Host "[resume] C transcription artifacts"
    }

    $CSrt = Join-Path $COut "C.ja.srt"
    $CCleanReport = Join-Path $COut "C.clean-report.json"
    if (-not (Assert-ArtifactSet -Label "C cleanup" -Paths @($CSrt, $CCleanReport))) {
        Invoke-Checked -Label "C cleanup" -FilePath $Python -Arguments @(
            $FrozenCleaner, "--input", (Join-Path $COut "C.raw.srt"),
            "--output", $CSrt, "--report", $CCleanReport
        )
        Add-ReportProvenance -ReportPath $CCleanReport -InputPath (Join-Path $COut "C.raw.srt") -OutputPath $CSrt
    } else {
        Assert-ReportProvenance -Label "C cleanup" -ReportPath $CCleanReport -InputPath (Join-Path $COut "C.raw.srt") -OutputPath $CSrt
        Write-Host "[resume] C clean subtitle"
    }

    $CQuality = Join-Path $COut "C.quality-report.json"
    if (-not (Test-Path -LiteralPath $CQuality -PathType Leaf)) {
        Invoke-ReportCheck -Label "C quality gate" -FilePath $Python -ReportPath $CQuality -Arguments @(
            $FrozenQuality, "--input", $CSrt, "--report", $CQuality
        )
        Add-ReportProvenance -ReportPath $CQuality -InputPath $CSrt
    } else { Assert-ReportProvenance -Label "C quality gate" -ReportPath $CQuality -InputPath $CSrt }
    $CStructure = Join-Path $COut "C.structure-report.json"
    if (-not (Test-Path -LiteralPath $CStructure -PathType Leaf)) {
        Invoke-ReportCheck -Label "C structure gate" -FilePath $Python -ReportPath $CStructure -Arguments @(
            $StructureValidator, "--input", $CSrt, "--source-duration", "$InputDuration", "--report", $CStructure,
            "--max-lines", "2", "--max-line-chars", "16"
        )
        Add-ReportProvenance -ReportPath $CStructure -InputPath $CSrt
    } else { Assert-ReportProvenance -Label "C structure gate" -ReportPath $CStructure -InputPath $CSrt }
    $CRun = Join-Path $COut "C.run.json"
    if (-not (Test-Path -LiteralPath $CRun -PathType Leaf)) {
        $CClean = Read-Json -Path $CCleanReport
        $CDetails = Read-Json -Path (Join-Path $COut "C.resegment-report.json")
        Write-Utf8Json -Path $CRun -Value ([ordered]@{
            group = "C"
            role = "high-confidence text replacement candidate"
            source_sha256 = $InputHash
            srt_sha256 = (Get-FileHash -LiteralPath $CSrt -Algorithm SHA256).Hash
            model_revision = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
            model_bin_sha256 = $Hashes.faster_model
            raw_blocks = $CClean.input_blocks
            cleaned_blocks = $CClean.kept_blocks
            word_timestamps = $CDetails.word_timestamps
            resegmentation = $CDetails.resegmentation
            quality_failures = @((Read-Json -Path $CQuality).failures)
            structure_failures = @((Read-Json -Path $CStructure).failures)
        })
    }
    $CState = Read-Json -Path $CRun
    if ($CState.source_sha256 -ne $InputHash -or $CState.srt_sha256 -ne (Get-FileHash -LiteralPath $CSrt -Algorithm SHA256).Hash) {
        throw "C stage run report does not match the current source/subtitle: $CRun"
    }

    # Fusion: text-only substitution. Cue count and every A timestamp must remain unchanged.
    $FusionOut = Join-Path $JobRoot "fusion"
    New-Item -ItemType Directory -Force -Path $FusionOut | Out-Null
    $FusionSrt = Join-Path $FusionOut "fusion.ja.srt"
    $FusionReport = Join-Path $FusionOut "fusion.report.json"
    if (-not (Assert-ArtifactSet -Label "A+C fusion" -Paths @($FusionSrt, $FusionReport))) {
        Invoke-Checked -Label "A+C deterministic fusion" -FilePath $Python -Arguments @(
            $Fusion, "--a-srt", $ASrt, "--c-srt", $CSrt,
            "--output", $FusionSrt, "--report", $FusionReport
        )
    } else {
        $ExistingFusion = Read-Json -Path $FusionReport
        if ($ExistingFusion.inputs.a_sha256 -ne (Get-FileHash -LiteralPath $ASrt -Algorithm SHA256).Hash -or
            $ExistingFusion.inputs.c_sha256 -ne (Get-FileHash -LiteralPath $CSrt -Algorithm SHA256).Hash -or
            $ExistingFusion.output.sha256 -ne (Get-FileHash -LiteralPath $FusionSrt -Algorithm SHA256).Hash) {
            throw "Fusion report input/output hash mismatch: $FusionReport"
        }
        Write-Host "[resume] fusion output and decision report"
    }

    $FusionQuality = Join-Path $FusionOut "fusion.quality-report.json"
    if (-not (Test-Path -LiteralPath $FusionQuality -PathType Leaf)) {
        Invoke-ReportCheck -Label "fusion quality gate" -FilePath $Python -ReportPath $FusionQuality -Arguments @(
            $FrozenQuality, "--input", $FusionSrt, "--report", $FusionQuality
        )
        Add-ReportProvenance -ReportPath $FusionQuality -InputPath $FusionSrt
    } else { Assert-ReportProvenance -Label "fusion quality gate" -ReportPath $FusionQuality -InputPath $FusionSrt }
    $FusionStructure = Join-Path $FusionOut "fusion.structure-report.json"
    if (-not (Test-Path -LiteralPath $FusionStructure -PathType Leaf)) {
        Invoke-ReportCheck -Label "fusion structure gate" -FilePath $Python -ReportPath $FusionStructure -Arguments @(
            $StructureValidator, "--input", $FusionSrt, "--source-duration", "$InputDuration", "--report", $FusionStructure
        )
        Add-ReportProvenance -ReportPath $FusionStructure -InputPath $FusionSrt
    } else { Assert-ReportProvenance -Label "fusion structure gate" -ReportPath $FusionStructure -InputPath $FusionSrt }

    $JobWatch.Stop()
    $FusionDetails = Read-Json -Path $FusionReport
    $GateReports = [ordered]@{
        a_quality = @((Read-Json -Path $AQuality).failures)
        a_structure = @((Read-Json -Path $AStructure).failures)
        c_quality = @((Read-Json -Path $CQuality).failures)
        c_structure = @((Read-Json -Path $CStructure).failures)
        fusion_quality = @((Read-Json -Path $FusionQuality).failures)
        fusion_structure = @((Read-Json -Path $FusionStructure).failures)
    }
    $AllFailures = @()
    foreach ($Name in $GateReports.Keys) {
        foreach ($Failure in $GateReports[$Name]) { $AllFailures += "$Name`: $Failure" }
    }
    foreach ($Failure in @($FusionDetails.output.structure_failures)) { $AllFailures += "fusion_internal: $Failure" }
    $Status = if ($AllFailures.Count -eq 0) { "complete" } else { "failed_gates" }
    $OutputHashes = [ordered]@{
        a_ja_srt = (Get-FileHash -LiteralPath $ASrt -Algorithm SHA256).Hash
        a_quality_report = (Get-FileHash -LiteralPath $AQuality -Algorithm SHA256).Hash
        a_structure_report = (Get-FileHash -LiteralPath $AStructure -Algorithm SHA256).Hash
        c_ja_srt = (Get-FileHash -LiteralPath $CSrt -Algorithm SHA256).Hash
        c_quality_report = (Get-FileHash -LiteralPath $CQuality -Algorithm SHA256).Hash
        c_structure_report = (Get-FileHash -LiteralPath $CStructure -Algorithm SHA256).Hash
        fusion_ja_srt = (Get-FileHash -LiteralPath $FusionSrt -Algorithm SHA256).Hash
        fusion_report = (Get-FileHash -LiteralPath $FusionReport -Algorithm SHA256).Hash
        fusion_quality_report = (Get-FileHash -LiteralPath $FusionQuality -Algorithm SHA256).Hash
        fusion_structure_report = (Get-FileHash -LiteralPath $FusionStructure -Algorithm SHA256).Hash
    }
    Write-Utf8Json -Path $JobRunPath -Value ([ordered]@{
        schema_version = 1
        status = $Status
        pipeline_version = $PipelineVersion
        fingerprint_id = $FingerprintId
        job_id = $JobId
        elapsed_seconds = [Math]::Round($JobWatch.Elapsed.TotalSeconds, 3)
        output_root = $JobRoot
        cache_root = $CacheRoot
        outputs = [ordered]@{
            a_ja_srt = $ASrt
            c_ja_srt = $CSrt
            fusion_ja_srt = $FusionSrt
            fusion_report = $FusionReport
            fusion_quality_report = $FusionQuality
            fusion_structure_report = $FusionStructure
        }
        output_sha256 = $OutputHashes
        fusion = [ordered]@{
            a_cues = $FusionDetails.inputs.a_cues
            c_cues = $FusionDetails.inputs.c_cues
            c_replacements = $FusionDetails.output.c_replacements
            a_backbone_cues = $FusionDetails.output.a_backbone_cues
            timeline_preserved_from_a = $FusionDetails.output.timeline_preserved_from_a
        }
        gate_failures = $GateReports
        failures = $AllFailures
        resource_requirement = [ordered]@{
            gpu_exclusive = $true
            scheduler_contract = "serialize with all other GPU jobs, including Lada and RIFE"
        }
    })
} finally {
    $env:PIP_CACHE_DIR = $OldPipCache
    $env:TEMP = $OldTemp
    $env:TMP = $OldTmp
}

Write-Host "[done] A+C output: $JobRoot"
if ($Status -ne "complete") { throw "A+C job finished with gate failures; inspect $JobRunPath" }

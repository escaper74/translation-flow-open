"""Portable entry points; no downloads, media tools, models or services."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jasna_picture.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


class InstallationPathTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows command entry")
    def test_python_entry_from_another_directory_preserves_imports_and_exit_code(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["TRANSLATION_FLOW_PYTHON"] = sys.executable
        entry = ROOT / "scripts/python.cmd"
        with tempfile.TemporaryDirectory(prefix="source entry ") as raw:
            command = "from translation_flow.runtime import PROJECT_ROOT; print(PROJECT_ROOT)"
            result = subprocess.run(["cmd.exe", "/d", "/c", str(entry), "-c", command],
                                    cwd=raw, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(ROOT, Path(result.stdout.strip()))
            result = subprocess.run(["cmd.exe", "/d", "/c", str(entry), "-c", "raise SystemExit(17)"],
                                    cwd=raw, env=env, capture_output=True, timeout=15)
            self.assertEqual(17, result.returncode)

    def test_picture_experiment_requires_user_selected_paths(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["plan"])
        args = build_parser().parse_args(["plan", "--source", "my-input.mkv", "--work-dir", "my-work"])
        self.assertEqual(Path("my-input.mkv"), args.source)
        self.assertEqual(Path("my-work"), args.work_dir)

    def test_cli_tools_honor_external_root_without_private_config(self):
        with tempfile.TemporaryDirectory() as raw:
            env = {k: v for k, v in os.environ.items() if not k.startswith("TRANSLATION_FLOW_")}
            env["TRANSLATION_FLOW_TOOL_ROOT"] = raw
            command = ("import json; from final_mux.cli import DEFAULT_FFMPEG; "
                       "from jasna_picture.cli import DEFAULT_JASNA; "
                       "print(json.dumps([str(DEFAULT_FFMPEG),str(DEFAULT_JASNA)]))")
            completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", command],
                                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, completed.returncode, completed.stderr)
            ffmpeg, jasna = map(Path, json.loads(completed.stdout))
            self.assertTrue(ffmpeg.is_relative_to(raw))
            self.assertEqual(Path(raw) / "jasna/jasna.exe", jasna)

    def powershell(self, script):
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
                                   cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell entry")
    def test_desktop_uses_project_venv_and_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            venv = root / ".venv/Scripts/python.exe"
            venv.parent.mkdir(parents=True)
            venv.touch()
            explicit = root / "explicit.exe"
            explicit.touch()
            script = r'''
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'src/desktop/Start-TranslationFlow.ps1'),[ref]$null,[ref]$null)
$node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resolve-TranslationPython'},$true)
Invoke-Expression $node.Extent.Text
$PythonPathBox=@{Text=''}
$first=Resolve-TranslationPython
$PythonPathBox.Text=$explicit
$second=Resolve-TranslationPython
@($first,$second) | ConvertTo-Json -Compress
'''
            prefix = "$ProjectRoot='{}';$explicit='{}';".format(str(root).replace("'", "''"), str(explicit).replace("'", "''"))
            self.assertEqual([str(venv), str(explicit)], json.loads(self.powershell(prefix + script)))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer")
    def test_installer_defaults_are_portable_without_running_installation(self):
        script = r'''
$ErrorActionPreference='Stop'
$file=Join-Path (Get-Location) 'scripts/asr_fusion/setup-dependencies.ps1'
$fixtureScriptRoot=Split-Path -Parent $file
$text=[IO.File]::ReadAllText($file)
# Only parameter/default path setup; do not evaluate installation or download code.
$prefix=$text.Substring(0,$text.IndexOf('$ExpectedModelFiles =')).Replace('$PSScriptRoot','$fixtureScriptRoot')
$block=[ScriptBlock]::Create($prefix+'; Write-Output $LabRoot')
$env:TRANSLATION_FLOW_TOOL_ROOT=''
$default=& $block
$expected=[IO.Path]::GetFullPath((Join-Path (Get-Location) 'workflow/tools/asr-lab'))
if ($default -ne $expected) { throw 'default root is not relative to this checkout' }
$env:TRANSLATION_FLOW_TOOL_ROOT=Join-Path $env:TEMP 'translation-install-fixture'
$configured=& $block
if ($configured -ne (Join-Path $env:TRANSLATION_FLOW_TOOL_ROOT 'asr-lab')) { throw 'tool-root ignored' }
$explicit=& $block -LabRoot (Join-Path $env:TEMP 'explicit-asr-fixture')
if ($explicit -ne (Join-Path $env:TEMP 'explicit-asr-fixture')) { throw 'explicit root ignored' }
Write-Output 'passed'
'''
        self.assertIn("passed", self.powershell(script))

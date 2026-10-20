Set-StrictMode -Version Latest

if (-not ("TranslationFlow.ProcessBridge" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;

namespace TranslationFlow
{
    public sealed class ProcessBridge : IDisposable
    {
        private readonly ConcurrentQueue<string> outputLines = new ConcurrentQueue<string>();
        private readonly ConcurrentQueue<string> errorLines = new ConcurrentQueue<string>();
        private Process process;

        public int ProcessId { get { return process == null ? 0 : process.Id; } }
        public bool HasExited { get { return process != null && process.HasExited; } }
        public int ExitCode
        {
            get
            {
                if (process == null || !process.HasExited)
                    throw new InvalidOperationException("The child process has not exited.");
                return process.ExitCode;
            }
        }

        private static string QuoteArgument(string value)
        {
            if (String.IsNullOrEmpty(value))
                return "\"\"";

            // CommandLineToArgvW-compatible quoting. Backslashes are doubled only
            // when they precede a quote or the closing quote.
            var quoted = new StringBuilder();
            quoted.Append('\"');
            int backslashes = 0;
            foreach (char character in value)
            {
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (character == '\"')
                {
                    quoted.Append('\\', backslashes * 2 + 1);
                    quoted.Append('\"');
                    backslashes = 0;
                    continue;
                }
                quoted.Append('\\', backslashes);
                backslashes = 0;
                quoted.Append(character);
            }
            quoted.Append('\\', backslashes * 2);
            quoted.Append('\"');
            return quoted.ToString();
        }

        public void Start(string pythonPath, string workingDirectory, string requestPath, string apiKey)
        {
            Start(pythonPath, workingDirectory, requestPath, apiKey, "translation_flow.cli");
        }

        public void Start(string pythonPath, string workingDirectory, string requestPath, string apiKey, string backendModule)
        {
            Start(pythonPath, workingDirectory, requestPath, apiKey, backendModule, "");
        }

        public void Start(string pythonPath, string workingDirectory, string requestPath, string apiKey, string backendModule, string toolRoot)
        {
            if (backendModule != "translation_flow.cli" &&
                backendModule != "full_workflow.cli" &&
                backendModule != "full_workflow.picture_cli" &&
                backendModule != "media_planning.cli" &&
                backendModule != "translation_flow.srt_desktop")
                throw new ArgumentException("Unsupported backend module.", "backendModule");

            string commandPrefix;
            if (backendModule == "translation_flow.cli")
                commandPrefix = "-m translation_flow.cli --request ";
            else if (backendModule == "full_workflow.picture_cli")
                commandPrefix = "-m full_workflow.picture_cli --request ";
            else if (backendModule == "full_workflow.cli")
                commandPrefix = "-m full_workflow.cli --request ";
            else if (backendModule == "translation_flow.srt_desktop")
                commandPrefix = "-m translation_flow.srt_desktop --request ";
            else
                commandPrefix = "-m media_planning.cli --request ";
            StartCommand(pythonPath, workingDirectory, commandPrefix + QuoteArgument(requestPath), apiKey, toolRoot);
        }

        public void StartCodexStatus(string pythonPath, string workingDirectory)
        {
            StartCommand(pythonPath, workingDirectory, "-m translation_flow.cli --codex-status", "");
        }

        public void StartSubtitleInspection(string pythonPath, string workingDirectory, string source, string toolRoot)
        {
            StartCommand(pythonPath, workingDirectory,
                "-m translation_flow.subtitle_detection --source " + QuoteArgument(source), "", toolRoot);
        }

        public void StartCodexLogin(string pythonPath, string workingDirectory)
        {
            StartCommand(pythonPath, workingDirectory, "-m translation_flow.desktop_access --login", "");
        }

        public void StartBeautyPlayer(string pythonPath, string workingDirectory, string source, string toolRoot)
        {
            StartCommand(pythonPath, workingDirectory,
                "-m beauty_player.server --media " + QuoteArgument(source) +
                " --tool-root " + QuoteArgument(toolRoot), "", toolRoot);
        }

        public void StartReadiness(string pythonPath, string workingDirectory, string mode,
                                   string provider, string model, string endpoint, string tokenCap,
                                   string toolRoot, string apiKey)
        {
            string args = "-m translation_flow.desktop_access --check --probe --mode " + QuoteArgument(mode)
                + " --provider " + QuoteArgument(provider) + " --model " + QuoteArgument(model);
            if (!String.IsNullOrWhiteSpace(endpoint)) args += " --endpoint " + QuoteArgument(endpoint);
            if (!String.IsNullOrWhiteSpace(tokenCap)) args += " --max-output-tokens " + QuoteArgument(tokenCap);
            StartCommand(pythonPath, workingDirectory, args, apiKey, toolRoot);
        }

        private void StartCommand(string pythonPath, string workingDirectory, string arguments, string apiKey, string toolRoot = "")
        {
            if (process != null)
                throw new InvalidOperationException("This bridge has already started a child process.");
            var startInfo = new ProcessStartInfo();
            startInfo.FileName = pythonPath;
            startInfo.Arguments = arguments;
            startInfo.WorkingDirectory = workingDirectory;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;
            startInfo.StandardOutputEncoding = new UTF8Encoding(false);
            startInfo.StandardErrorEncoding = new UTF8Encoding(false);
            // Match QueueBridge: Explorer/embedded hosts can supply both Path
            // and PATH; .NET Framework throws while first copying that block.
            System.Collections.Specialized.StringDictionary environment;
            try { environment = startInfo.EnvironmentVariables; }
            catch (ArgumentException)
            {
                environment = startInfo.EnvironmentVariables;
                environment.Clear();
                foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
                    environment[(string)entry.Key] = (string)entry.Value;
            }
            environment["PYTHONIOENCODING"] = "utf-8";
            startInfo.EnvironmentVariables["PYTHONUTF8"] = "1";
            if (!String.IsNullOrWhiteSpace(toolRoot))
                startInfo.EnvironmentVariables["TRANSLATION_FLOW_TOOL_ROOT"] = toolRoot;
            startInfo.EnvironmentVariables.Remove("SUBTITLE_API_KEY");
            if (!String.IsNullOrEmpty(apiKey))
                startInfo.EnvironmentVariables["SUBTITLE_API_KEY"] = apiKey;

            process = new Process();
            process.StartInfo = startInfo;
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data != null)
                    outputLines.Enqueue(args.Data);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data != null)
                    errorLines.Enqueue(args.Data);
            };

            if (!process.Start())
                throw new InvalidOperationException("Unable to start the translation backend.");

            // The child inherited the value. Do not retain the credential in ProcessStartInfo.
            startInfo.EnvironmentVariables.Remove("SUBTITLE_API_KEY");
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }

        public bool TryGetOutput(out string line) { return outputLines.TryDequeue(out line); }
        public bool TryGetError(out string line) { return errorLines.TryDequeue(out line); }
        public void FinishReading()
        {
            if (process != null && process.HasExited)
                process.WaitForExit();
        }

        public void Dispose()
        {
            if (process != null)
            {
                process.Dispose();
                process = null;
            }
        }
    }
}
'@
}

function Start-TranslationFlowBackend {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$RequestPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ApiKey,
        [ValidateSet("translation_flow.cli", "full_workflow.cli", "full_workflow.picture_cli", "media_planning.cli", "translation_flow.srt_desktop")]
        [string]$BackendModule = "translation_flow.cli",
        [string]$ToolRoot = ""
    )

    $providerId = "deepseek"
    if ($BackendModule -notin @("media_planning.cli", "full_workflow.picture_cli") -and [IO.File]::Exists($RequestPath)) {
        $request = [IO.File]::ReadAllText($RequestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $providerId = [string]$request.provider.id
        if ($providerId -notin @("deepseek", "codex", "qwen", "kimi", "openai", "anthropic", "gemini")) { throw "不支持的翻译方式：$providerId" }
    }
    if (
        [string]::IsNullOrWhiteSpace($ApiKey) -and
        $BackendModule -eq "translation_flow.cli" -and $providerId -eq "deepseek"
    ) {
        throw "DeepSeek API Key 不能为空。"
    }

    if ($providerId -eq "codex" -or $BackendModule -in @("media_planning.cli", "full_workflow.picture_cli")) { $ApiKey = "" }

    $bridge = New-Object TranslationFlow.ProcessBridge
    try {
        $bridge.Start($PythonPath, $WorkingDirectory, $RequestPath, $ApiKey, $BackendModule, $ToolRoot)
        return $bridge
    }
    catch {
        $bridge.Dispose()
        throw
    }
}

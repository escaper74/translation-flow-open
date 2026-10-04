# Copy to local.psd1. Use literal absolute paths; expressions/$env are not allowed.
# Keep credentials out of this file. Empty values use the documented defaults.
@{
    PythonPath = ''            # e.g. C:\TranslationFlow\.venv\Scripts\python.exe
    ToolRoot = ''              # default: <project>\workflow\tools
    JasnaPath = ''             # default: <ToolRoot>\jasna\jasna.exe
    AcceptanceGateReport = ''  # your own accepted 30-minute workflow-report.json
}

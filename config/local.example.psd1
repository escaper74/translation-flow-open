# Copy to local.psd1. Use literal absolute paths; expressions/$env are not allowed.
# Keep credentials out of this file. Empty values use the documented defaults.
@{
    PythonPath = ''            # e.g. C:\TranslationFlow\.venv\Scripts\python.exe
    ToolRoot = ''              # default: <project>\workflow\tools
    JasnaPath = ''             # default: <ToolRoot>\jasna\jasna.exe
    # Older AcceptanceGateReport values are only retained for old checkpoint identity.
    # Ordinary picture/full tasks do not require a historical acceptance report.
}

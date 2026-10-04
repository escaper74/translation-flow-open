Set-StrictMode -Version Latest
$script:BeautyStartup=@{Busy=$false}

function Show-BeautyPlayer([string]$Source='') {
    $owner=$script:BeautyStartup
    if ($owner.Busy) { return }
    if (-not $Source) {
        $dialog=New-Object Microsoft.Win32.OpenFileDialog
        $dialog.Title='选择视频 · 实时美颜播放'
        $dialog.Filter='视频文件|*.mp4;*.mkv;*.webm;*.mov;*.m4v|所有文件|*.*'
        if ($dialog.ShowDialog($Window) -ne $true) { return }
        $Source=$dialog.FileName
    }
    try {
        if (-not [IO.File]::Exists($Source)) {throw '视频文件不存在。'}
        $bridge=New-Object TranslationFlow.ProcessBridge
        $bridge.StartBeautyPlayer($PythonPathBox.Text,$ProjectRoot,$Source,$ToolRootBox.Text)
        $state=@{Bridge=$bridge;Error=''}
        $timer=New-Object Windows.Threading.DispatcherTimer
        $timer.Interval=[TimeSpan]::FromMilliseconds(200)
        $state.Timer=$timer;$owner.Busy=$true
        $timer.Add_Tick({
            $line=''
            while($state.Bridge.TryGetError([ref]$line)) {$state.Error+=$line+"`n"}
            while($state.Bridge.TryGetOutput([ref]$line)) {
                try {$reply=$line|ConvertFrom-Json} catch {continue}
                if ($reply.PSObject.Properties['url']) {
                    $state.Timer.Stop();$state.Bridge.Dispose();$owner.Busy=$false
                    return
                }
            }
            if ($state.Bridge.HasExited) {
                $state.Timer.Stop();$state.Bridge.FinishReading()
                while($state.Bridge.TryGetError([ref]$line)) {$state.Error+=$line+"`n"}
                $state.Bridge.Dispose();$owner.Busy=$false
                [void][Windows.MessageBox]::Show($Window,"美颜播放器未能启动：`n"+$state.Error,'实时美颜','OK','Warning')
            }
        }.GetNewClosure())
        $timer.Start()
    } catch {
        $owner.Busy=$false
        [void][Windows.MessageBox]::Show($Window,$_.Exception.Message,'实时美颜','OK','Warning')
    }
}

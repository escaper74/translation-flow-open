# Product actions run in a separate local process; no credentials or model service.
Set-StrictMode -Version Latest

function Invoke-LibraryBackend([string]$Python, [string]$Root, [string]$Payload) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName=$Python; $info.Arguments='-m translation_flow.library'; $info.WorkingDirectory=$Root
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true; $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $encoding=New-Object Text.UTF8Encoding($false)
    $info.StandardOutputEncoding=$encoding; $info.StandardErrorEncoding=$encoding
    try { $environment=$info.get_EnvironmentVariables() } catch {
        if ($_.Exception.InnerException -isnot [ArgumentException]) { throw }
        $environment=$info.get_EnvironmentVariables(); $environment.Clear()
        foreach($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) { $environment[[string]$entry.Key]=[string]$entry.Value }
    }
    $environment.Remove('SUBTITLE_API_KEY'); $environment['PYTHONIOENCODING']='utf-8'
    $process=New-Object Diagnostics.Process; $process.StartInfo=$info
    try {
        if (-not $process.Start()) { throw '无法启动本地成品工具。' }
        $out=$process.StandardOutput.ReadToEndAsync(); $err=$process.StandardError.ReadToEndAsync()
        $bytes=$encoding.GetBytes($Payload); $stream=$process.StandardInput.BaseStream
        $stream.Write($bytes,0,$bytes.Length); $stream.Close()
        $process.WaitForExit()
        $output=$out.GetAwaiter().GetResult(); $errorText=$err.GetAwaiter().GetResult()
        if (-not $output) { throw "本地工具没有返回结果：$errorText" }
        $reply=$output | ConvertFrom-Json
        if (-not $reply.ok) { throw [string]$reply.error }
        return $reply.value
    } finally { $process.Dispose() }
}

function Start-LibraryAction($State, [hashtable]$Payload, [scriptblock]$Then) {
    if ($null -ne $State.Call) { return }
    $State.Actions.IsEnabled=$false; $State.Status.Text='正在处理，请稍候…'
    if ($State.ContainsKey('Controls')) { $State.Controls.LibraryCards.IsEnabled=$false }
    $shell=[PowerShell]::Create()
    [void]$shell.AddScript(${function:Invoke-LibraryBackend}.ToString()).AddArgument($PythonPathBox.Text).AddArgument($ProjectRoot).AddArgument(($Payload|ConvertTo-Json -Depth 15 -Compress))
    $State.Call=@{Shell=$shell; Async=$shell.BeginInvoke(); Then=$Then}
}

function Initialize-LibraryWindow($State) {
    $State.Call=$null
    $timer=New-Object Windows.Threading.DispatcherTimer
    $timer.Interval=[TimeSpan]::FromMilliseconds(150)
    $tick={
        $call=$State.Call
        if ($null -eq $call -or -not $call.Async.IsCompleted) { return }
        $State.Call=$null; $State.Actions.IsEnabled=$true
        if ($State.ContainsKey('Controls')) { $State.Controls.LibraryCards.IsEnabled=$true }
        try {
            $values=@($call.Shell.EndInvoke($call.Async))
            if ($call.Shell.HadErrors) { throw ($call.Shell.Streams.Error | Out-String) }
            if (-not $values.Count) { throw '没有收到处理结果。' }
            & $call.Then $values[-1] $State
        } catch {
            $State.Status.Text=$_.Exception.Message
            $State.Status.ToolTip=$_.ScriptStackTrace
            if ($State.ContainsKey('Review')) { $State.Grid.IsReadOnly=$false; $State.ExportAfterSave=$false }
        }
        finally { $call.Shell.Dispose() }
    }.GetNewClosure()
    $timer.Add_Tick($tick); $timer.Start(); $State.Timer=$timer
    $State.Window.Add_Closing({ if ($null -ne $State.Call) { $_.Cancel=$true; $State.Status.Text='正在保存或封装，请等当前操作结束后关闭。' } }.GetNewClosure())
    $State.Window.Add_Closed({ $timer.Stop() }.GetNewClosure())
}

function New-LibraryWindow([string]$Name, $Owner) {
    [xml]$xml=[IO.File]::ReadAllText((Join-Path $script:LibraryScriptRoot $Name),[Text.Encoding]::UTF8)
    $base=[Uri]($script:LibraryScriptRoot + [IO.Path]::DirectorySeparatorChar)
    $reader=[Xml.XmlReader]::Create([IO.StringReader]::new($xml.OuterXml),[Xml.XmlReaderSettings]::new(),$base.AbsoluteUri)
    $view=[Windows.Markup.XamlReader]::Load($reader); $view.Owner=$Owner
    return $view
}

function Show-CleanupConfirmation($Owner, $Plan) {
    if (-not @($Plan.files).Count) { return $false }
    $dialog=New-Object Windows.Window
    $dialog.Title='确认清理中间文件'; $dialog.Width=820; $dialog.Height=570; $dialog.Owner=$Owner; $dialog.WindowStartupLocation='CenterOwner'
    $panel=New-Object Windows.Controls.DockPanel; $panel.Margin='20'; $dialog.Content=$panel
    $buttons=New-Object Windows.Controls.StackPanel; $buttons.Orientation='Horizontal'; $buttons.HorizontalAlignment='Right'
    [Windows.Controls.DockPanel]::SetDock($buttons,'Bottom'); [void]$panel.Children.Add($buttons)
    $cancel=New-Object Windows.Controls.Button; $cancel.Content='取消'; $cancel.Padding='14,8'; $cancel.Margin='8'
    $confirm=New-Object Windows.Controls.Button; $confirm.Content='已看过成片，确认永久删除'; $confirm.Padding='14,8'; $confirm.Margin='8'
    [void]$buttons.Children.Add($cancel); [void]$buttons.Children.Add($confirm)
    $text=New-Object Windows.Controls.TextBox; $text.IsReadOnly=$true; $text.AcceptsReturn=$true; $text.TextWrapping='Wrap'; $text.VerticalScrollBarVisibility='Auto'
    $size='{0:N2}' -f ($Plan.bytes/1GB)
    $text.Text="将永久删除 $($Plan.count) 个普通文件，逻辑大小约 $size GiB（硬链接等会影响实际释放量）。`r`n$($Plan.warning)`r`n`r`n"+(@($Plan.files|ForEach-Object { "$($_.path)  [$($_.bytes) bytes]" }) -join "`r`n")
    [void]$panel.Children.Add($text)
    $cancel.Add_Click({$dialog.DialogResult=$false}.GetNewClosure()); $confirm.Add_Click({$dialog.DialogResult=$true}.GetNewClosure())
    return $dialog.ShowDialog() -eq $true
}

function Show-DeliveryLibrary {
    $hostPanel=$null; if($null -ne $Window){$hostPanel=$Window.FindName('LibraryPageHost')}
    if ($null -ne $hostPanel -and $null -ne $script:EmbeddedLibraryState) {
        Set-UiPage 'library'; & $script:EmbeddedLibraryState.Refresh; return
    }
    Initialize-DesignThumbnails
    $view=New-LibraryWindow 'TranslationFlowLibrary.xaml' $Window
    $controls=@{}; foreach($name in @('LibraryCards','LibraryEmpty','LibrarySelection','LibrarySelectedTitle','LibraryVersionLabel','LibraryVersionChoices','LibraryVersionsButton','LibraryPlay','LibraryBeauty','LibraryReview','LibraryFolderLabel','LibraryCleanup')) { $controls[$name]=$view.FindName($name) }
    $state=@{Window=$view; View=$view; Controls=$controls; Cards=@(); Actions=$view.FindName('LibraryActions'); Status=$view.FindName('LibraryStatus'); Grid=$view.FindName('LibraryGrid'); Variants=$view.FindName('LibraryVariants'); Directory=$view.FindName('LibraryDirectory')}
    if($null -ne $hostPanel) {
        $content=$view.Content; $view.Content=$null; $content.Resources=$view.Resources; $hostPanel.Content=$content
        $state.Window=$Window; $script:EmbeddedLibraryState=$state
    } else { $view.Content.Margin='28' }
    $state.Directory.Text=Get-TaskCenterOutputDirectory
    Initialize-LibraryWindow $state
    $refresh={
        Start-LibraryAction $state @{action='list';output_dir=$state.Directory.Text} {
            param($value, $state)
            $state.Grid.ItemsSource=@($value.items)
            Update-DesignLibraryCards $state
            $state.Status.Text="找到 $(@($value.items).Count) 项正式成品。"
            if (@($value.warnings).Count) { $state.Status.Text+=" $(@($value.warnings).Count) 项记录需检查（悬停查看）。" }
            $state.Status.ToolTip=@($value.warnings)-join "`n"
            if (@($value.items).Count) { $state.Grid.SelectedIndex=0 }
        }.GetNewClosure()
    }.GetNewClosure()
    $state.Refresh=$refresh
    $view.FindName('LibraryRefresh').Add_Click($refresh)
    $view.FindName('LibraryBrowse').Add_Click({
        if ($null -ne $state.Call) { return }
        $picker=New-Object Windows.Forms.FolderBrowserDialog
        try { if ($picker.ShowDialog() -eq 'OK') { $state.Directory.Text=$picker.SelectedPath; & $refresh } } finally {$picker.Dispose()}
    }.GetNewClosure())
    $state.Grid.Add_SelectionChanged({
        $item=$state.Grid.SelectedItem
        $variants=@()
        if ($null -ne $item) {$variants=@($item.variants)}
        $state.Variants.ItemsSource=$variants
        if ($state.Variants.Items.Count) {$state.Variants.SelectedIndex=0}
        Update-DesignLibrarySelection $state
    }.GetNewClosure())
    $state.Variants.Add_SelectionChanged({Update-DesignLibrarySelection $state}.GetNewClosure())
    $controls.LibraryVersionsButton.Add_Click({$state.Controls.LibraryVersionChoices.Visibility=$(if($state.Controls.LibraryVersionChoices.Visibility -eq 'Visible'){'Collapsed'}else{'Visible'})}.GetNewClosure())
    $view.FindName('LibraryPlay').Add_Click({if ($state.Variants.SelectedItem -and [IO.File]::Exists([string]$state.Variants.SelectedItem)) {Start-Process -FilePath ([string]$state.Variants.SelectedItem)}}.GetNewClosure())
    $view.FindName('LibraryBeauty').Add_Click({if ($state.Variants.SelectedItem) {Show-BeautyPlayer ([string]$state.Variants.SelectedItem)}}.GetNewClosure())
    $view.FindName('LibraryFolder').Add_Click({if ($null -ne $state.Grid.SelectedItem) {Start-Process -FilePath $state.Grid.SelectedItem.output_dir}}.GetNewClosure())
    $view.FindName('LibraryReview').Add_Click({
        if ($null -eq $state.Grid.SelectedItem) { return }
        Start-LibraryAction $state @{action='review';request_path=$state.Grid.SelectedItem.request_path} {param($value, $state) $state.Status.Text='校对只另存修订副本。'; Show-SubtitleReview $state.Window $value}.GetNewClosure()
    }.GetNewClosure())
    $view.FindName('LibraryCleanup').Add_Click({
        if ($null -eq $state.Grid.SelectedItem) { return }
        Start-LibraryAction $state @{action='plan';request_path=$state.Grid.SelectedItem.request_path} {
            param($plan, $state)
            if (-not $plan.count) { $state.Status.Text='这个成品没有可清理的媒体中间文件；成片、字幕和日志已保留。'; return }
            if (Show-CleanupConfirmation $state.Window $plan) {
                Start-LibraryAction $state @{action='clean';plan=$plan;confirmed=$true} {
                    param($done, $state)
                    $size='{0:N2}' -f ($done.plan.bytes/1GB)
                    $state.Status.Text="已永久删除 $($done.deleted_count) 个中间文件（逻辑大小约 $size GiB）；源片、全部成片、字幕、缓存和日志保留。被删检查点无法恢复。"
                    Refresh-TaskCenter
                }.GetNewClosure()
            } else { $state.Status.Text='已取消，没有删除文件。' }
        }.GetNewClosure()
    }.GetNewClosure())
    & $refresh
    if($null -ne $hostPanel) { Set-UiPage 'library' } else { [void]$view.ShowDialog() }
}

function Show-SubtitleReview($Owner, $Review) {
    $view=New-LibraryWindow 'TranslationFlowReview.xaml' $Owner
    $state=@{Window=$view;Actions=$view.FindName('ReviewActions');Status=$view.FindName('ReviewStatus');Grid=$view.FindName('ReviewGrid');Player=$view.FindName('ReviewPlayer');Review=$Review;Revision='';ExportAfterSave=$false;Dirty=$false;ToolRoot=$ToolRootBox.Text;OpenFolder='';RevisedMedia=''}
    Initialize-LibraryWindow $state
    $state.Grid.ItemsSource=@($Review.rows)
    if ($Review.media) { $state.Player.Source=[Uri]$Review.media }
    else { $state.Player.Visibility='Collapsed'; $view.FindName('ReviewExport').IsEnabled=$false }
    $state.Player.Add_MediaFailed({$view.FindName('ReviewPlaybackHint').Text='此视频无法在内置播放器解码，可用系统播放器观看；字幕编辑不受影响。'}.GetNewClosure())
    $state.Grid.Add_SelectionChanged({
        if ($null -ne $state.Grid.SelectedItem -and $state.Review.media) {$state.Player.Position=[TimeSpan]::FromMilliseconds([double]$state.Grid.SelectedItem.start_ms);$state.Player.Play()}
    }.GetNewClosure())
    $state.Grid.Add_BeginningEdit({$state.Dirty=$true}.GetNewClosure())
    $view.FindName('ReviewPlay').Add_Click({$state.Player.Play()}.GetNewClosure())
    $view.FindName('ReviewPause').Add_Click({$state.Player.Pause()}.GetNewClosure())
    $save={
        [void]$state.Grid.CommitEdit([Windows.Controls.DataGridEditingUnit]::Cell,$true)
        [void]$state.Grid.CommitEdit([Windows.Controls.DataGridEditingUnit]::Row,$true)
        $state.Review.rows=@($state.Grid.ItemsSource)
        $state.Grid.IsReadOnly=$true
        Start-LibraryAction $state @{action='save_review';review=$state.Review} {
            param($saved, $state)
            $state.Grid.IsReadOnly=$false; $state.Dirty=$false; $state.Revision=$saved.revision_path
            $state.Window.FindName('ReviewOpen').IsEnabled=$true
            $state.OpenFolder=Split-Path -Parent $saved.chinese_srt
            $state.RevisedMedia='';$state.Window.FindName('ReviewPlayResult').IsEnabled=$false
            $state.Status.Text="已另存人工修订 SRT，修改 $($saved.changed_count) 条：$($saved.chinese_srt)"
            if ($state.ExportAfterSave -and $state.Review.media) {
                $state.ExportAfterSave=$false
                Start-LibraryAction $state @{action='export_review';revision_path=$saved.revision_path;tool_root=$state.ToolRoot} {
                    param($done, $state)
                    $state.Status.Text="修订成片技术通过，待观看：$($done.media)"
                    $state.OpenFolder=Split-Path -Parent $done.media
                    $state.RevisedMedia=$done.media;$state.Window.FindName('ReviewPlayResult').IsEnabled=$true
                }.GetNewClosure()
            }
        }.GetNewClosure()
    }.GetNewClosure()
    $view.FindName('ReviewSave').Add_Click({$state.ExportAfterSave=$false;& $save}.GetNewClosure())
    $view.FindName('ReviewExport').Add_Click({$state.ExportAfterSave=$true;& $save}.GetNewClosure())
    $view.FindName('ReviewOpen').Add_Click({if($state.OpenFolder){Start-Process -FilePath $state.OpenFolder}}.GetNewClosure())
    $view.FindName('ReviewPlayResult').Add_Click({if($state.RevisedMedia){Start-Process -FilePath $state.RevisedMedia}}.GetNewClosure())
    $view.Add_Closing({
        if ($null -ne $state.Call) { return }
        if ($state.Dirty -and [Windows.MessageBox]::Show($view,'修改尚未保存，是否放弃修改？','字幕校对','YesNo','Warning') -ne 'Yes') {$_.Cancel=$true}
    }.GetNewClosure())
    $view.Add_Closed({$state.Player.Close()}.GetNewClosure())
    [void]$view.ShowDialog()
}

$script:LibraryScriptRoot=$PSScriptRoot
if (-not (Get-Command Update-DesignLibraryCards -ErrorAction SilentlyContinue)) { . (Join-Path $script:LibraryScriptRoot 'TranslationFlowDesign.ps1') }

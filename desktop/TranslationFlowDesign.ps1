# Approved A presentation. All actions delegate to the existing request/queue/library paths.
Set-StrictMode -Version Latest
$script:DesignRoot = $PSScriptRoot
$script:DesignArt = @{}
$script:DesignThumbnails = New-Object System.Collections.ArrayList
$script:EmbeddedLibraryState = $null

function Read-DesignXaml([string]$Text) {
    $context=New-Object Windows.Markup.ParserContext
    $context.BaseUri=[Uri]($script:DesignRoot + [IO.Path]::DirectorySeparatorChar)
    $reader=[Xml.XmlReader]::Create([IO.StringReader]::new($Text),[Xml.XmlReaderSettings]::new(),$context.BaseUri.AbsoluteUri)
    return [Windows.Markup.XamlReader]::Load($reader)
}

function Get-DesignArtwork([int]$Index) {
    if (-not $script:DesignArt.ContainsKey($Index)) {
        $frame=[Windows.Media.Imaging.BitmapFrame]::Create([Uri](Join-Path $script:DesignRoot 'assets/feature-art.png'))
        $cell=[int]($frame.PixelWidth/3)
        # Frame the three transparent illustrations consistently inside the cards.
        $art=New-Object Windows.Media.Imaging.CroppedBitmap $frame,([Windows.Int32Rect]::new(($cell*$Index+75),85,630,550))
        $art.Freeze(); $script:DesignArt[$Index]=$art
    }
    return $script:DesignArt[$Index]
}

function Initialize-DesignThumbnails {
    if ('TranslationFlow.ShellPreview' -as [type]) { return }
    # The Windows shell supplies local thumbnails. One background STA worker keeps
    # slow shell codecs off the dispatcher; no FFmpeg, model, or production task runs.
    Add-Type -ReferencedAssemblies @('System','System.Core','WindowsBase','PresentationCore') -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media.Imaging;
namespace TranslationFlow {
 public static class ShellPreview {
  [StructLayout(LayoutKind.Sequential)] struct Size { public int X,Y; public Size(int x,int y){X=x;Y=y;} }
  [ComImport,Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IImageFactory { [PreserveSig] int GetImage(Size size,int flags,out IntPtr bitmap); }
  [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=false)]
  static extern void SHCreateItemFromParsingName(string path,IntPtr bind,ref Guid id,[MarshalAs(UnmanagedType.Interface)]out IImageFactory item);
  [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
  static readonly ConcurrentDictionary<string,BitmapSource> Cache=new ConcurrentDictionary<string,BitmapSource>();
  static readonly ConcurrentDictionary<string,bool> Requested=new ConcurrentDictionary<string,bool>();
  static readonly BlockingCollection<string[]> Work=new BlockingCollection<string[]>();
  static ShellPreview(){var worker=new Thread(Run){IsBackground=true,Name="TranslationFlow thumbnails"};worker.SetApartmentState(ApartmentState.STA);worker.Start();}
  public static BitmapSource Get(string path){
   if(String.IsNullOrWhiteSpace(path)||!File.Exists(path))return null;
   string key=path+"|"+File.GetLastWriteTimeUtc(path).Ticks;
   BitmapSource image;if(Cache.TryGetValue(key,out image))return image;
   if(Requested.TryAdd(key,true))Work.Add(new[]{key,path});return null;
  }
  static void Run(){foreach(var job in Work.GetConsumingEnumerable()){
   IImageFactory item=null;IntPtr handle=IntPtr.Zero;
   try{Guid id=typeof(IImageFactory).GUID;SHCreateItemFromParsingName(job[1],IntPtr.Zero,ref id,out item);
    if(item.GetImage(new Size(640,360),8,out handle)==0 && handle!=IntPtr.Zero){var image=Imaging.CreateBitmapSourceFromHBitmap(handle,IntPtr.Zero,Int32Rect.Empty,BitmapSizeOptions.FromEmptyOptions());image.Freeze();Cache[job[0]]=image;}
   }catch{}finally{if(handle!=IntPtr.Zero)DeleteObject(handle);if(item!=null)Marshal.ReleaseComObject(item);}
  }}
 }
}
'@
}

function Set-DesignThumbnail($Image,[string]$Path) {
    $Image.Source=Get-DesignArtwork 0
    $Image.Stretch='Uniform'
    if (-not $Path) { return }
    $source=[TranslationFlow.ShellPreview]::Get($Path)
    if ($null -ne $source) { $Image.Source=$source; $Image.Stretch='UniformToFill' }
    else { [void]$script:DesignThumbnails.Add(@{Image=[WeakReference]::new($Image);Path=$Path;Until=[DateTime]::UtcNow.AddSeconds(30)}) }
}

function Set-DesignNavigation([string]$Page) {
    foreach($pair in @(@('HomeNavBorder','home'),@('TasksNavBorder','tasks'),@('LibraryNavBorder','library'))) {
        $active=$Page -eq $pair[1] -or ($pair[1] -eq 'home' -and $Page -eq 'setup') -or ($pair[1] -eq 'tasks' -and $Page -eq 'run')
        ($Window.FindName($pair[0])).BorderBrush=Convert-ToBrush $(if($active){'#00A5A2'}else{'#00FFFFFF'})
        ($Window.FindName($pair[0])).Child.Foreground=Convert-ToBrush $(if($active){'#007D88'}else{'#64748B'})
        ($Window.FindName($pair[0])).Child.FontWeight=$(if($active){'SemiBold'}else{'Normal'})
    }
}

function Open-DesignInput([string]$Mode) {
    if ($script:IsRunning -or $null -ne $script:AccessBridge) { Set-UiPage 'tasks'; return }
    Select-ComboBoxItemByTag $WhisperModelBox $WhisperAModel
    $script:CurrentWhisperAModel = $WhisperAModel
    $script:PendingQueueResumeId='';$script:PendingSrtRequestPath='';$script:SelectedFiles=@()
    $script:ImportPaths=@();$script:ImportAutoDeselected=$false
    $ForceSubtitleTranslationBox.IsChecked=$false;$SubtitleImportPanel.Visibility='Collapsed'
    $InputVideoBox.Clear();$SelectedFilesText.Visibility='Collapsed';$InputErrorText.Text=''
    Select-ComboBoxItemByTag $TaskModeBox $Mode
    Update-TaskModeControls
    $BrowseVideoButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
    if ($InputVideoBox.Text) { Set-UiPage 'setup' }
}

function Get-DesignModeLabel([string]$Mode) {
    switch($Mode) { 'picture_only' {'去马'} 'translation_picture_final' {'去马 + 翻译'} 'media_repackage' {'视频换格式'} 'srt_translation' {'翻译字幕'} default {'翻译'} }
}

function New-DesignTaskCard($Item,[string]$Kind,$History=$null) {
    $active=$Kind -eq 'active';$waiting=$Kind -eq 'waiting'
    $card=Read-DesignXaml ('<Border xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Background="'+$(if($active){'#EFFBFA'}else{'White'})+'" BorderBrush="'+$(if($active){'#88DAD4'}else{'#E3E9EF'})+'" BorderThickness="1" CornerRadius="16" Padding="20" Margin="0,0,0,10"><Grid><Grid.ColumnDefinitions><ColumnDefinition Width="'+$(if($active){'200'}else{'108'})+'"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions><Border CornerRadius="10" Background="#EDF6F6" Margin="0,0,20,0"><Image Name="Thumb" Height="'+$(if($active){'142'}else{'64'})+'"/></Border><StackPanel Grid.Column="1"><TextBlock Name="Title" FontSize="'+$(if($active){'21'}else{'17'})+'" FontWeight="SemiBold" TextTrimming="CharacterEllipsis" TextWrapping="NoWrap"/><TextBlock Name="Mode" Foreground="#7A899A" Margin="0,5,0,0"/><TextBlock Name="Status" Foreground="#00858C" FontSize="17" FontWeight="Medium" Margin="0,9,0,0"/><Grid Name="ProgressRow" Margin="0,10,0,8"><Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="60"/></Grid.ColumnDefinitions><ProgressBar Name="Progress" Height="8" Minimum="0" Maximum="100" Foreground="#00B4AB" Background="#D4F0EE" BorderThickness="0"/><TextBlock Name="Percent" Grid.Column="1" HorizontalAlignment="Right" Foreground="#67768A"/></Grid><DockPanel Name="Actions" Margin="0,5,0,0"><StackPanel Name="Buttons" DockPanel.Dock="Right" Orientation="Horizontal"/><TextBlock Name="Elapsed" Foreground="#8390A0" VerticalAlignment="Center"/></DockPanel></StackPanel></Grid></Border>')
    if(-not $active){
        $card=Read-DesignXaml '<Border xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Background="White" BorderBrush="#E3E9EF" BorderThickness="1" CornerRadius="16" Padding="14" Margin="0,0,0,10"><Grid><Grid.ColumnDefinitions><ColumnDefinition Width="104"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Border CornerRadius="10" Background="#EDF6F6" Margin="0,0,18,0"><Image Name="Thumb" Height="68"/></Border><StackPanel Grid.Column="1" VerticalAlignment="Center"><TextBlock Name="Title" FontSize="17" FontWeight="SemiBold" TextTrimming="CharacterEllipsis" TextWrapping="NoWrap"/><TextBlock Name="Mode" Foreground="#7A899A" FontSize="13" Margin="0,3,0,0"/><TextBlock Name="Status" Foreground="#00858C" FontSize="14" Margin="0,3,0,0"/><Grid Name="ProgressRow" Visibility="Collapsed"/></StackPanel><StackPanel Name="Buttons" Grid.Column="2" Orientation="Horizontal" VerticalAlignment="Center" Margin="14,0,0,0"/></Grid></Border>'
    }
    $path=$(if($null -ne $History){$History.SourcePath}else{[string](Get-JsonProperty $Item 'source' '')})
    $card.FindName('Title').Text=[IO.Path]::GetFileName($path)
    $card.FindName('Title').ToolTip=$path
    Set-DesignThumbnail ($card.FindName('Thumb')) $path
    $mode=$(if($null -ne $History){$History.ModeLabel}else{Get-DesignModeLabel ([string](Get-JsonProperty $Item 'mode' 'translation'))})
    $card.FindName('Mode').Text=$mode
    $status=[string](Get-JsonProperty $Item 'status' '')
    if ($null -ne $Item -and (Test-QueueItemPaused $Item)) { $status='paused' }
    $stage=[string](Get-JsonProperty $Item 'stage' '')
    $card.FindName('Status').Text=$(if($active){if($status -eq 'stopping'){'正在保存进度，完成后停止'}elseif($stage){Get-StageLabel $stage}else{'正在准备'}}elseif($waiting){'等待中'}elseif($History){$History.StatusLabel}else{switch($status){'paused' {'已暂停 · 进度已保留'} 'stopped' {'已停止 · 进度已保留'} default {'尚未完成 · 可以继续'}}})
    $card.FindName('ProgressRow').Visibility=$(if($active){'Visible'}else{'Collapsed'})
    if($active) {
        $unknown=$stage -in @('asr_a','whisper_a','asr_c','whisper_c')
        $card.FindName('Progress').IsIndeterminate=$unknown
        $progress=[Math]::Max(0,[Math]::Min(100,[double](Get-JsonProperty $Item 'progress' 0)))
        $card.FindName('Progress').Value=$progress
        $card.FindName('Percent').Text=$(if($unknown){'识别中'}else{"$([Math]::Round($progress))%"})
        $started=Get-JsonProperty $Item 'started_at' $null
        if($null -ne $started){$elapsed=[Math]::Max(0,([DateTimeOffset]::UtcNow-[DateTimeOffset]::FromUnixTimeSeconds([long]$started)).TotalSeconds);$card.FindName('Elapsed').Text='已用 '+(Format-UiDuration $elapsed)}
    }
    $buttons=$card.FindName('Buttons')
    if($active){
        $detail=New-Object Windows.Controls.Button;$detail.Content='查看详情';$detail.Style=$Window.FindResource('Quiet')
        $id=[string]$Item.id
        $detail.Add_Click({$QueueGrid.SelectedItem=@($QueueGrid.ItemsSource|Where-Object{$_.id -eq $id})|Select-Object -First 1;$ViewQueueItemButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))}.GetNewClosure())
        [void]$buttons.Children.Add($detail)
        $stop=New-Object Windows.Controls.Button;$stop.Content='停止并保留进度';$stop.Style=$Window.FindResource('ButtonOutline');$stop.Margin='12,0,0,0';$stop.IsEnabled=$status -ne 'stopping'
        $stop.Add_Click({try{[void](Start-QueueCall @{action='stop';id=$id})}catch{$QueueStatusText.Text=$_.Exception.Message}}.GetNewClosure());[void]$buttons.Children.Add($stop)
    }elseif(-not $waiting){
        $resume=New-Object Windows.Controls.Button;$resume.Content='继续处理  →';$resume.Style=$Window.FindResource('Primary')
        if($null -ne $History){$entry=$History;$resume.Add_Click({$TaskHistoryGrid.SelectedItem=$entry;$ResumeTaskButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))}.GetNewClosure())}
        else{$id=[string]$Item.id;$resume.Add_Click({$QueueGrid.SelectedItem=@($QueueGrid.ItemsSource|Where-Object{$_.id -eq $id})|Select-Object -First 1;$ResumeQueueItemButton.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))}.GetNewClosure())}
        [void]$buttons.Children.Add($resume)
    }
    return $card
}

function Update-DesignTaskCards {
    if($null -eq $Window.FindName('ActiveTasksHost')){return}
    $activeHost=$Window.FindName('ActiveTasksHost');$waitingHost=$Window.FindName('WaitingTasksHost');$recoverHost=$Window.FindName('RecoverTasksHost')
    $activeHost.Children.Clear();$waitingHost.Children.Clear();$recoverHost.Children.Clear()
    $queue=Get-Variable QueueSnapshot -Scope Script -ValueOnly -ErrorAction SilentlyContinue
    $items=@();if($null -ne $queue){$items=@($queue.items)}
    foreach($item in $items){
        $kind=switch([string]$item.status){ {$_ -in @('starting','running','stopping')} {'active'} 'queued' {'waiting'} {$_ -in @('paused','stopped','failed','interrupted')} {'recover'} default {'skip'} }
        if (Test-QueueItemPaused $item) { $kind='recover' }
        if($kind -eq 'skip'){continue}
        $hostPanel=switch($kind){'active'{$activeHost}'waiting'{$waitingHost}default{$recoverHost}}
        [void]$hostPanel.Children.Add((New-DesignTaskCard $item $kind))
    }
    $queuedPaths=@($items|ForEach-Object{[string]$_.request_path})
    foreach($entry in @($TaskHistoryGrid.ItemsSource)){
        if($null -ne $entry -and $entry.CanResume -and $entry.RequestPath -notin $queuedPaths -and ($entry.StatusLabel -match '失败|可恢复|无运行记录' -or $entry.StatusLabel -notmatch '通过|完成|运行中')){
            [void]$recoverHost.Children.Add((New-DesignTaskCard $null 'recover' $entry))
        }
    }
    $hasAny=($activeHost.Children.Count+$waitingHost.Children.Count+$recoverHost.Children.Count) -gt 0
    $Window.FindName('TaskEmptyText').Visibility=$(if($hasAny){'Collapsed'}else{'Visible'})
    $Window.FindName('WaitingSectionHeader').Visibility=$(if($waitingHost.Children.Count){'Visible'}else{'Collapsed'})
    $Window.FindName('RecoverSectionHeader').Visibility=$(if($recoverHost.Children.Count){'Visible'}else{'Collapsed'})
    $PauseQueueButton.IsEnabled=$waitingHost.Children.Count -gt 0
    $StopQueueButton.IsEnabled=($activeHost.Children.Count+$waitingHost.Children.Count) -gt 0
    $Window.FindName('TaskBackgroundHint').Visibility=$(if($activeHost.Children.Count){'Visible'}else{'Collapsed'})
}

function Update-DesignLibraryCards($State) {
    $hostPanel=$State.Controls.LibraryCards; $hostPanel.Children.Clear(); $State.Cards=@()
    $items=@($State.Grid.ItemsSource)
    $State.Controls.LibraryEmpty.Visibility=$(if($items.Count){'Collapsed'}else{'Visible'})
    $State.Controls.LibraryFolderLabel.ToolTip=$State.Directory.Text
    foreach($item in $items) {
        $card=Read-DesignXaml '<Border xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Width="326" Margin="8,0,8,16" Padding="14" CornerRadius="18" BorderBrush="#E3E9EF" BorderThickness="1" Background="White" Cursor="Hand"><StackPanel><Border Background="#EDF6F6" CornerRadius="10"><Image Name="Thumb" Height="168"/></Border><TextBlock Name="Title" FontSize="20" FontWeight="SemiBold" Margin="3,15,3,8" TextTrimming="CharacterEllipsis" TextWrapping="NoWrap"/><TextBlock Name="Description" Foreground="#008F90" Margin="3,0,3,8"/><DockPanel Margin="3,0,3,13"><TextBlock Name="Format" Foreground="#8391A1"/><TextBlock Name="Size" Foreground="#8391A1" HorizontalAlignment="Right"/></DockPanel><Button Name="Play" Content="▶   播放" Height="42"/><Button Name="Cleanup" Content="清理中间文件" Height="36" Margin="0,6,0,0"/></StackPanel></Border>'
        $card.FindName('Title').Text=[IO.Path]::GetFileNameWithoutExtension([string]$item.title);$card.ToolTip=$item.title
        $description=switch([string]$item.kind){'.full-workflow'{'去马 · 中文字幕'}'.picture-workflow'{'去马'}'.media-repackage'{'已换格式'}default{'中文字幕'}}
        $card.FindName('Description').Text='✓  '+$description
        $card.FindName('Format').Text=$(if($item.media){[IO.Path]::GetExtension([string]$item.media).TrimStart('.').ToUpperInvariant()}else{'SRT'})
        $card.FindName('Size').Text=$(if($item.media){'{0:N2} GiB' -f [double]$item.size_gib}else{'字幕文件'})
        $play=$card.FindName('Play');$play.Style=$State.View.FindResource('Primary')
        $entry=$item
        $play.Content=$(if($item.media){'▶   播放'}else{'字幕校对'})
        $play.Add_Click({$State.Grid.SelectedItem=$entry;$button=$(if($entry.media){$State.Controls.LibraryPlay}else{$State.Controls.LibraryReview});$button.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))}.GetNewClosure())
        $cleanup=$card.FindName('Cleanup');$cleanup.Style=$State.View.FindResource('Quiet')
        $cleanup.Visibility=$(if($item.media){'Visible'}else{'Collapsed'})
        $cleanup.ToolTip='先查看文件清单和可释放空间，确认后删除。保留源片、全部完整成片、字幕和日志。'
        $cleanup.Add_Click({
            if ($null -ne $State.Call) { return }
            $State.Grid.SelectedItem=$entry
            $State.Controls.LibraryCleanup.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Button]::ClickEvent))
        }.GetNewClosure())
        $card.Add_MouseLeftButtonUp({$State.Grid.SelectedItem=$entry}.GetNewClosure())
        Set-DesignThumbnail ($card.FindName('Thumb')) ([string]$item.media)
        [void]$hostPanel.Children.Add($card);$State.Cards+=@{Border=$card;Item=$item}
    }
    $resize={
        $width=$hostPanel.ActualWidth
        if($width -le 0){return}
        $columns=$(if($width -ge 880){3}elseif($width -ge 560){2}else{1})
        foreach($card in $State.Cards){$card.Border.Width=[Math]::Max(230,[Math]::Floor($width/$columns)-16)}
    }.GetNewClosure()
    if(-not $State.ContainsKey('ResizeCards')){$hostPanel.Add_SizeChanged($resize);$State.ResizeCards=$resize}
    & $State.ResizeCards
    if($null -ne $Window -and $null -ne $Window.FindName('RecentTitle') -and $items.Count) {
        $recent=$items[0];$Window.FindName('RecentTitle').Text=[IO.Path]::GetFileNameWithoutExtension([string]$recent.title)
        $Window.FindName('RecentDescription').Text='已完成 · '+$(if($recent.media){'随时播放'}else{'中文字幕'})
        $script:RecentDesignMedia=[string]$recent.media;$Window.FindName('RecentPlayButton').IsEnabled=[bool]$recent.media
        $Window.FindName('RecentPlayButton').Visibility=$(if($recent.media){'Visible'}else{'Collapsed'})
        Set-DesignThumbnail ($Window.FindName('RecentArtwork')) ([string]$recent.media)
    }
}

function Update-DesignLibrarySelection($State) {
    $item=$State.Grid.SelectedItem
    $State.Controls.LibrarySelection.Visibility=$(if($null -ne $item){'Visible'}else{'Collapsed'})
    foreach($card in $State.Cards){$selected=$card.Item -eq $item;$card.Border.BorderBrush=[Windows.Media.BrushConverter]::new().ConvertFromString($(if($selected){'#00AAA6'}else{'#E3E9EF'}))}
    if($null -eq $item){return}
    $State.Controls.LibrarySelectedTitle.Text=[IO.Path]::GetFileNameWithoutExtension([string]$item.title)
    $State.Controls.LibrarySelectedTitle.ToolTip=$item.title
    $State.Controls.LibraryBeauty.IsEnabled=[bool]$item.media
    $State.Controls.LibraryReview.IsEnabled=[bool]$item.chinese_srt
    $current=[string]$State.Variants.SelectedItem
    $State.Controls.LibraryVersionLabel.Text=$(if($current){'当前版本   ·   '+[IO.Path]::GetFileName($current)}else{'中文字幕   ·   SRT'})
    $State.Controls.LibraryVersionsButton.Visibility=$(if(@($item.variants).Count -gt 1){'Visible'}else{'Collapsed'})
    $choices=$State.Controls.LibraryVersionChoices;$choices.Children.Clear()
    foreach($path in @($item.variants)){
        $button=New-Object Windows.Controls.Button;$button.Content=[IO.Path]::GetFileName([string]$path);$button.ToolTip=$path;$button.Style=$State.View.FindResource('Quiet');$button.Margin='0,0,8,4'
        $variant=$path;$button.Add_Click({$State.Variants.SelectedItem=$variant;$State.Controls.LibraryVersionChoices.Visibility='Collapsed'}.GetNewClosure());[void]$choices.Children.Add($button)
    }
}

function Update-DesignRecent($Entries) {
    $script:RecentDesignMedia=''
    $Window.FindName('RecentPlayButton').Visibility='Collapsed'
    $Window.FindName('RecentTitle').Text='还没有处理过的视频'
    $Window.FindName('RecentDescription').Text='从上面选择一个功能，开始第一项任务'
    foreach($entry in @($Entries)) {
        if($entry.StatusLabel -notmatch '通过'){continue}
        $job=Split-Path -Parent $entry.RequestPath
        foreach($name in @('workflow.result.json','job.result.json','technical-validation.report.json')){
            $result=Read-TaskCenterJson (Join-Path $job $name)
            if($null -eq $result){continue}
            $outputs=Get-JsonProperty $result 'outputs' $null
            foreach($key in @('final_mp4','final_mkv','final_media','translation_original_mkv')){
                $path=[string](Get-JsonProperty $outputs $key '')
                if($path -and [IO.File]::Exists($path)){$script:RecentDesignMedia=$path;break}
            }
            if(-not $script:RecentDesignMedia){$output=Get-JsonProperty $result 'output' $null;$path=[string](Get-JsonProperty $output 'path' '');if($path -and [IO.File]::Exists($path)){$script:RecentDesignMedia=$path}}
            if($script:RecentDesignMedia){break}
        }
        if($script:RecentDesignMedia){
            $Window.FindName('RecentTitle').Text=$entry.SourceName
            $Window.FindName('RecentDescription').Text=$entry.ModeLabel+' · 已完成'
            $Window.FindName('RecentPlayButton').Visibility='Visible';$Window.FindName('RecentPlayButton').IsEnabled=$true
            Set-DesignThumbnail ($Window.FindName('RecentArtwork')) $script:RecentDesignMedia
            break
        }
    }
}

function Initialize-Design {
    Initialize-DesignThumbnails
    $Window.FindName('PictureArtwork').Source=Get-DesignArtwork 0
    $Window.FindName('SubtitleArtwork').Source=Get-DesignArtwork 1
    $Window.FindName('BeautyArtwork').Source=Get-DesignArtwork 2
    $Window.FindName('RecentArtwork').Source=Get-DesignArtwork 0
    $Window.Width=[Math]::Min($Window.Width,[Windows.SystemParameters]::WorkArea.Width-32)
    $Window.Height=[Math]::Min($Window.Height,[Windows.SystemParameters]::WorkArea.Height-32)
    $Window.FindName('TitleBar').Add_MouseLeftButtonDown({if($_.OriginalSource -is [Windows.Controls.TextBlock] -or $_.OriginalSource -is [Windows.Controls.Grid] -or $_.OriginalSource -is [Windows.Controls.Border]){if($_.ClickCount -eq 2){$Window.WindowState=$(if($Window.WindowState -eq 'Maximized'){'Normal'}else{'Maximized'})}else{$Window.DragMove()}}})
    $Window.FindName('MinimizeButton').Add_Click({$Window.WindowState='Minimized'})
    $Window.FindName('MaximizeButton').Add_Click({$Window.WindowState=$(if($Window.WindowState -eq 'Maximized'){'Normal'}else{'Maximized'})})
    $Window.FindName('CloseWindowButton').Add_Click({$Window.Close()})
    $Window.FindName('HomePictureButton').Add_Click({Open-DesignInput 'picture_only'})
    $Window.FindName('HomeTranslationButton').Add_Click({Open-DesignInput 'translation'})
    $Window.FindName('SetupBackButton').Add_Click({Set-UiPage 'home'})
    $Window.FindName('TaskNewButton').Add_Click({Set-UiPage 'home'})
    $Window.FindName('HomeViewAllButton').Add_Click({Show-DeliveryLibrary})
    $Window.FindName('SettingsButton').Add_Click({$Window.FindName('SettingsOverlay').Visibility='Visible'})
    $Window.FindName('ProviderSettingsButton').Add_Click({$Window.FindName('SettingsOverlay').Visibility='Visible'})
    $Window.FindName('CloseSettingsButton').Add_Click({$Window.FindName('SettingsOverlay').Visibility='Collapsed'})
    $OutputDirectoryBox.Add_TextChanged({$label=$Window.FindName('OutputFolderLabel');$label.Text=$(if($OutputDirectoryBox.Text -eq (Join-Path $ProjectRoot 'deliverables')){'成品文件夹'}else{Split-Path -Leaf $OutputDirectoryBox.Text});$label.ToolTip=$OutputDirectoryBox.Text})
    $ProviderBox.Add_SelectionChanged({$Window.FindName('ProviderSettingsButton').Content='翻译服务  ·  '+[string]$ProviderBox.SelectedItem.Tag+'  ›'})
    $Window.FindName('RecentPlayButton').Add_Click({if($script:RecentDesignMedia -and [IO.File]::Exists($script:RecentDesignMedia)){Start-Process -FilePath $script:RecentDesignMedia}})
    $script:RecentDesignMedia=''
    $script:DesignImageTimer=New-Object Windows.Threading.DispatcherTimer
    $script:DesignImageTimer.Interval=[TimeSpan]::FromMilliseconds(350)
    $script:DesignImageTimer.Add_Tick({
        for($i=$script:DesignThumbnails.Count-1;$i -ge 0;$i--){
            $pending=$script:DesignThumbnails[$i];$image=$pending.Image.Target
            if($null -eq $image -or [DateTime]::UtcNow -gt $pending.Until){$script:DesignThumbnails.RemoveAt($i);continue}
            $bitmap=[TranslationFlow.ShellPreview]::Get($pending.Path)
            if($null -ne $bitmap){$image.Source=$bitmap;$image.Stretch='UniformToFill';$script:DesignThumbnails.RemoveAt($i)}
        }
    })
    $script:DesignImageTimer.Start()
    $Window.Add_Closed({$script:DesignImageTimer.Stop()})
    Update-DesignTaskCards
    Set-UiPage 'home'
}

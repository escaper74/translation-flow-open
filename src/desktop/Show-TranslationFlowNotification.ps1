param([ValidateSet('completed','failed','interrupted')][string]$Status)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon=New-Object Windows.Forms.NotifyIcon
$icon.Icon=[Drawing.SystemIcons]::Information
$icon.Text='翻译流'; $icon.Visible=$true
$icon.BalloonTipTitle=$(if($Status -eq 'completed'){'翻译流：处理完成'}else{'翻译流：任务需要处理'})
$icon.BalloonTipText=$(if($Status -eq 'completed'){'成品已生成。打开翻译流的“成品”页查看，实际效果仍需观看确认。'}else{'已有进度保留。打开翻译流的“任务”页查看原因和继续操作。'})
$timer=New-Object Windows.Forms.Timer
$timer.Interval=8000
$timer.Add_Tick({$timer.Stop();[Windows.Forms.Application]::ExitThread()})
try { $icon.ShowBalloonTip(7000);$timer.Start();[Windows.Forms.Application]::Run() }
finally { $timer.Dispose();$icon.Visible=$false;$icon.Dispose() }

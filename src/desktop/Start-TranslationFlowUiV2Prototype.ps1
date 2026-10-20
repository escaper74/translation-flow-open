[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [ValidateSet("ready", "running", "success", "failure")]
    [string]$InitialState = "ready",
    [switch]$InitialAdvanced,
    [string]$ScreenshotPath = "",
    [switch]$CloseAfterScreenshot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

[xml]$Xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="翻译流 · 轻科幻舰桥视觉原型"
        Width="1180" Height="840" MinWidth="940" MinHeight="700"
        WindowStartupLocation="CenterScreen"
        Background="#07111F"
        FontFamily="Microsoft YaHei UI" FontSize="14">
    <Window.Resources>
        <LinearGradientBrush x:Key="WindowBrush" StartPoint="0,0" EndPoint="1,1">
            <GradientStop Color="#081421" Offset="0"/>
            <GradientStop Color="#06101C" Offset="0.58"/>
            <GradientStop Color="#091526" Offset="1"/>
        </LinearGradientBrush>
        <LinearGradientBrush x:Key="PrimaryBrush" StartPoint="0,0.5" EndPoint="1,0.5">
            <GradientStop Color="#1567E5" Offset="0"/>
            <GradientStop Color="#159FD5" Offset="0.55"/>
            <GradientStop Color="#29BED8" Offset="1"/>
        </LinearGradientBrush>
        <LinearGradientBrush x:Key="ProgressBrush" StartPoint="0,0.5" EndPoint="1,0.5">
            <GradientStop Color="#115BE3" Offset="0"/>
            <GradientStop Color="#148FF0" Offset="0.52"/>
            <GradientStop Color="#2ED4F2" Offset="1"/>
        </LinearGradientBrush>
        <RadialGradientBrush x:Key="CoreBrush">
            <GradientStop Color="#5530A9FF" Offset="0"/>
            <GradientStop Color="#D90A1D2F" Offset="0.58"/>
            <GradientStop Color="#F2081523" Offset="1"/>
        </RadialGradientBrush>

        <Style x:Key="PanelStyle" TargetType="Border">
            <Setter Property="Background" Value="#DB0B1B2D"/>
            <Setter Property="BorderBrush" Value="#267ED3FF"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="CornerRadius" Value="18"/>
        </Style>

        <Style x:Key="SecondaryButton" TargetType="Button">
            <Setter Property="Foreground" Value="#BCD4E6"/>
            <Setter Property="Background" Value="#A8122A41"/>
            <Setter Property="BorderBrush" Value="#327ED3FF"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Padding" Value="14,9"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="ButtonBorder"
                                Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}"
                                CornerRadius="11" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="ButtonBorder" Property="Background" Value="#D11B3A56"/>
                                <Setter TargetName="ButtonBorder" Property="BorderBrush" Value="#7056D9FF"/>
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="ButtonBorder" Property="Opacity" Value="0.84"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="ButtonBorder" Property="Opacity" Value="0.42"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PrimaryButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="Foreground" Value="#F5FDFF"/>
            <Setter Property="Background" Value="{StaticResource PrimaryBrush}"/>
            <Setter Property="BorderBrush" Value="#755DE2FF"/>
            <Setter Property="Padding" Value="22,12"/>
            <Setter Property="Effect">
                <Setter.Value>
                    <DropShadowEffect Color="#2E8CFF" BlurRadius="22" ShadowDepth="5" Opacity="0.24"/>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="CompactButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="Padding" Value="10,7"/>
            <Setter Property="Foreground" Value="#7F9DB4"/>
            <Setter Property="Background" Value="Transparent"/>
            <Setter Property="BorderBrush" Value="Transparent"/>
        </Style>

        <Style x:Key="SceneButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
            <Setter Property="Padding" Value="11"/>
            <Setter Property="Background" Value="#9405101C"/>
            <Setter Property="BorderBrush" Value="#237ED3FF"/>
        </Style>

        <Style x:Key="SwitchStyle" TargetType="ToggleButton">
            <Setter Property="Width" Value="44"/>
            <Setter Property="Height" Value="24"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ToggleButton">
                        <Grid Width="44" Height="24">
                            <Border x:Name="SwitchTrack" Background="#142538" BorderBrush="#317ED3FF"
                                    BorderThickness="1" CornerRadius="12"/>
                            <Ellipse x:Name="SwitchKnob" Width="16" Height="16" Fill="#7891A4"
                                     HorizontalAlignment="Left" Margin="3,0,0,0"/>
                        </Grid>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="SwitchTrack" Property="Background" Value="#8C147FCC"/>
                                <Setter TargetName="SwitchTrack" Property="BorderBrush" Value="#7A4ADAFF"/>
                                <Setter TargetName="SwitchKnob" Property="HorizontalAlignment" Value="Right"/>
                                <Setter TargetName="SwitchKnob" Property="Margin" Value="0,0,3,0"/>
                                <Setter TargetName="SwitchKnob" Property="Fill" Value="#BAF3FF"/>
                                <Setter TargetName="SwitchKnob" Property="Effect">
                                    <Setter.Value>
                                        <DropShadowEffect Color="#66E2FF" BlurRadius="10" ShadowDepth="0" Opacity="0.64"/>
                                    </Setter.Value>
                                </Setter>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>

    <Grid Background="{StaticResource WindowBrush}">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <Border Grid.Row="0" Background="#F2071321" BorderBrush="#247ED3FF" BorderThickness="0,0,0,1" Padding="22,16">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                    <Border Width="36" Height="36" CornerRadius="11" BorderBrush="#B74DD9FF" BorderThickness="1"
                            Background="#552080FF" Margin="0,0,12,0">
                        <Grid>
                            <Rectangle Width="19" Height="2" Fill="#66E2FF" RenderTransformOrigin="0.5,0.5">
                                <Rectangle.RenderTransform><SkewTransform AngleX="-24"/></Rectangle.RenderTransform>
                            </Rectangle>
                            <Rectangle Width="19" Height="2" Fill="#66E2FF" Margin="0,10,0,0" RenderTransformOrigin="0.5,0.5">
                                <Rectangle.RenderTransform><SkewTransform AngleX="-24"/></Rectangle.RenderTransform>
                            </Rectangle>
                        </Grid>
                    </Border>
                    <StackPanel>
                        <TextBlock Text="ESCAPER // 翻译流" Foreground="#F5FBFF" FontWeight="SemiBold"/>
                        <TextBlock Text="本地字幕与成片制作" Foreground="#7F9DB4" FontSize="12" Margin="0,3,0,0"/>
                    </StackPanel>
                </StackPanel>

                <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                    <Border Background="#17615BFF" BorderBrush="#4D979FFF" BorderThickness="1" CornerRadius="12"
                            Padding="10,6" Margin="0,0,10,0">
                        <TextBlock Text="视觉原型 · 未接入" Foreground="#BFC8FF" FontSize="12"/>
                    </Border>
                    <Border Background="#C9040D18" BorderBrush="#257ED3FF" BorderThickness="1" CornerRadius="12" Padding="3">
                        <StackPanel Orientation="Horizontal">
                            <Button x:Name="ShowReady" Content="准备" Style="{StaticResource CompactButton}"/>
                            <Button x:Name="ShowRunning" Content="运行" Style="{StaticResource CompactButton}"/>
                            <Button x:Name="ShowSuccess" Content="成功" Style="{StaticResource CompactButton}"/>
                            <Button x:Name="ShowFailure" Content="失败" Style="{StaticResource CompactButton}"/>
                        </StackPanel>
                    </Border>
                </StackPanel>
            </Grid>
        </Border>

        <Grid Grid.Row="1" Margin="28">
            <Grid x:Name="ReadyScreen">
                <ScrollViewer VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled">
                    <StackPanel>
                        <Grid Margin="0,0,0,20">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="Auto"/>
                            </Grid.ColumnDefinitions>
                            <StackPanel>
                                <TextBlock Text="READY FOR LAUNCH" Foreground="#5FD5FF" FontSize="12" FontWeight="SemiBold"/>
                                <TextBlock Text="让影片听懂你" Foreground="#F7FBFF" FontSize="34" FontWeight="SemiBold" Margin="0,6,0,0"/>
                                <TextBlock Text="放入源片，选择翻译场景；其余步骤交给翻译流。" Foreground="#92AABD" Margin="0,8,0,0"/>
                            </StackPanel>
                            <Border Grid.Column="1" Background="#A4050F1B" BorderBrush="#297ED3FF" BorderThickness="1"
                                    CornerRadius="14" Padding="4" VerticalAlignment="Center">
                                <StackPanel Orientation="Horizontal">
                                    <Button x:Name="SimpleMode" Content="简单" Style="{StaticResource CompactButton}" Padding="16,9"/>
                                    <Button x:Name="AdvancedMode" Content="高级" Style="{StaticResource CompactButton}" Padding="16,9"/>
                                </StackPanel>
                            </Border>
                        </Grid>

                        <Grid>
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="6*" MinWidth="480"/>
                                <ColumnDefinition Width="16"/>
                                <ColumnDefinition Width="4*" MinWidth="360"/>
                            </Grid.ColumnDefinitions>

                            <Border x:Name="DropZone" Grid.Column="0" Style="{StaticResource PanelStyle}" Padding="26" MinHeight="390" AllowDrop="True">
                                <Grid>
                                    <Grid.RowDefinitions>
                                        <RowDefinition Height="*"/>
                                        <RowDefinition Height="Auto"/>
                                    </Grid.RowDefinitions>
                                    <Border BorderBrush="#5C53CDFF" BorderThickness="1" CornerRadius="14" Padding="24">
                                        <Border.Resources>
                                            <Style TargetType="Border">
                                                <Setter Property="SnapsToDevicePixels" Value="True"/>
                                            </Style>
                                        </Border.Resources>
                                        <Grid>
                                            <Grid.ColumnDefinitions>
                                                <ColumnDefinition Width="Auto"/>
                                                <ColumnDefinition Width="*"/>
                                            </Grid.ColumnDefinitions>
                                            <Border Width="64" Height="64" CornerRadius="19" Background="#2B1D80FF"
                                                    BorderBrush="#3D53CDFF" BorderThickness="1" Margin="0,0,18,0"
                                                    VerticalAlignment="Top">
                                                <Grid>
                                                    <Path Data="M 20,14 L 39,14 L 47,22 L 47,50 L 20,50 Z M 39,14 L 39,23 L 47,23"
                                                          Stroke="#74E3FF" StrokeThickness="2" Fill="Transparent"/>
                                                    <Path Data="M 26,31 L 41,31 M 26,38 L 41,38"
                                                          Stroke="#74E3FF" StrokeThickness="2"/>
                                                </Grid>
                                            </Border>
                                            <StackPanel Grid.Column="1">
                                                <TextBlock Text="把影片拖到这里" Foreground="#82A0B7"/>
                                                <TextBlock x:Name="DemoFileName" Text="星海列车_第03话.mkv" Foreground="#F4FBFF"
                                                           FontSize="20" FontWeight="SemiBold" Margin="0,8,0,0" TextWrapping="Wrap"/>
                                                <StackPanel Orientation="Horizontal" Margin="0,10,0,0">
                                                    <TextBlock Text="MKV" Foreground="#708DA4" Margin="0,0,10,0"/>
                                                    <TextBlock Text="·  等待开始后检查" Foreground="#708DA4" Margin="0,0,10,0"/>
                                                    <TextBlock Text="·  源片只读" Foreground="#708DA4"/>
                                                </StackPanel>
                                                <Button x:Name="ChooseDemoFile" Content="重新选择影片" Style="{StaticResource CompactButton}"
                                                        HorizontalAlignment="Left" Foreground="#6FDCFF" Margin="-10,13,0,0"/>
                                            </StackPanel>
                                        </Grid>
                                    </Border>

                                    <Border Grid.Row="1" Background="#9C030D18" BorderBrush="#237ED3FF" BorderThickness="1"
                                            CornerRadius="13" Padding="14" Margin="0,18,0,0">
                                        <Grid>
                                            <Grid.ColumnDefinitions>
                                                <ColumnDefinition Width="*"/>
                                                <ColumnDefinition Width="Auto"/>
                                            </Grid.ColumnDefinitions>
                                            <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                                                <TextBlock Text="保存到" Foreground="#78A0BA" Margin="0,0,12,0"/>
                                                <TextBlock Text="D:\成片输出" Foreground="#C5D9E7"/>
                                            </StackPanel>
                                            <Button x:Name="ChooseOutput" Grid.Column="1" Content="选择文件夹" Style="{StaticResource SecondaryButton}"/>
                                        </Grid>
                                    </Border>
                                </Grid>
                            </Border>

                            <Border Grid.Column="2" Style="{StaticResource PanelStyle}" Padding="20">
                                <StackPanel>
                                    <TextBlock Text="翻译场景" Foreground="#DCEBFF" FontWeight="SemiBold" Margin="0,0,0,11"/>
                                    <Grid>
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="*"/>
                                            <ColumnDefinition Width="8"/>
                                            <ColumnDefinition Width="*"/>
                                            <ColumnDefinition Width="8"/>
                                            <ColumnDefinition Width="*"/>
                                        </Grid.ColumnDefinitions>
                                        <Button x:Name="AnimeScene" Grid.Column="0" Tag="动漫" Style="{StaticResource SceneButton}">
                                            <StackPanel>
                                                <TextBlock Text="动漫"/>
                                                <TextBlock Text="ANIME" Foreground="#4FCFF8" FontSize="11" Margin="0,8,0,0"/>
                                            </StackPanel>
                                        </Button>
                                        <Button x:Name="FilmScene" Grid.Column="2" Tag="电影 / 电视剧" Style="{StaticResource SceneButton}">
                                            <StackPanel>
                                                <TextBlock Text="电影 / 电视剧" TextWrapping="Wrap"/>
                                                <TextBlock Text="FILM" Foreground="#4FCFF8" FontSize="11" Margin="0,8,0,0"/>
                                            </StackPanel>
                                        </Button>
                                        <Button x:Name="AdultScene" Grid.Column="4" Tag="成人直白" Style="{StaticResource SceneButton}">
                                            <StackPanel>
                                                <TextBlock Text="成人直白"/>
                                                <TextBlock Text="ADULT" Foreground="#4FCFF8" FontSize="11" Margin="0,8,0,0"/>
                                            </StackPanel>
                                        </Button>
                                    </Grid>

                                    <TextBlock Text="翻译 API 密钥" Foreground="#DCEBFF" FontWeight="SemiBold" Margin="0,20,0,9"/>
                                    <Grid>
                                        <PasswordBox x:Name="ApiKeyPassword" Password="demo-key-not-real-9Q2M"
                                                     Background="#A804101C" Foreground="#DCEBFF" BorderBrush="#317ED3FF"
                                                     BorderThickness="1" Padding="12,10"/>
                                        <TextBox x:Name="ApiKeyPlain" Text="demo-key-not-real-9Q2M" Visibility="Collapsed"
                                                 Background="#A804101C" Foreground="#DCEBFF" BorderBrush="#317ED3FF"
                                                 BorderThickness="1" Padding="12,10"/>
                                        <Button x:Name="ToggleKey" Content="显示" Style="{StaticResource CompactButton}"
                                                HorizontalAlignment="Right" Margin="0,3,4,3"/>
                                    </Grid>

                                    <StackPanel x:Name="AdvancedPanel" Margin="0,18,0,0" Visibility="Collapsed">
                                        <TextBlock Text="画面处理" Foreground="#DCEBFF" FontWeight="SemiBold" Margin="0,0,0,10"/>
                                        <Grid Margin="0,5">
                                            <TextBlock Text="去马赛克" Foreground="#AFC7D8" VerticalAlignment="Center"/>
                                            <ToggleButton x:Name="DemosaicToggle" Style="{StaticResource SwitchStyle}" HorizontalAlignment="Right" IsChecked="True"/>
                                        </Grid>
                                        <Grid Margin="0,5">
                                            <TextBlock Text="画质增强" Foreground="#AFC7D8" VerticalAlignment="Center"/>
                                            <ToggleButton x:Name="EnhanceToggle" Style="{StaticResource SwitchStyle}" HorizontalAlignment="Right" IsChecked="True"/>
                                        </Grid>
                                        <Grid Margin="0,5">
                                            <TextBlock Text="2 倍插帧（耗时较长）" Foreground="#AFC7D8" VerticalAlignment="Center"/>
                                            <ToggleButton x:Name="InterpolationToggle" Style="{StaticResource SwitchStyle}" HorizontalAlignment="Right"/>
                                        </Grid>
                                    </StackPanel>

                                    <Grid Margin="0,22,0,0">
                                        <TextBlock Text="所有文件名、路径和进度都是虚构演示。" Foreground="#6F8AA0" VerticalAlignment="Center"/>
                                        <Button x:Name="StartDemo" Content="开始制作" Style="{StaticResource PrimaryButton}"
                                                HorizontalAlignment="Right" MinWidth="156"/>
                                    </Grid>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </StackPanel>
                </ScrollViewer>
            </Grid>

            <Grid x:Name="RunningScreen" Visibility="Collapsed">
                <Border Style="{StaticResource PanelStyle}" Padding="38" MaxWidth="940" HorizontalAlignment="Center" VerticalAlignment="Center">
                    <StackPanel>
                        <TextBlock Text="PROCESSING STREAM" Foreground="#5FD5FF" FontSize="12" FontWeight="SemiBold" HorizontalAlignment="Center"/>
                        <Grid Width="190" Height="190" Margin="0,18,0,8" HorizontalAlignment="Center">
                            <Ellipse Stroke="#316EEBFF" StrokeThickness="2" StrokeDashArray="2,5" RenderTransformOrigin="0.5,0.5">
                                <Ellipse.RenderTransform><RotateTransform x:Name="CoreRotate"/></Ellipse.RenderTransform>
                            </Ellipse>
                            <Ellipse x:Name="CorePulse" Width="128" Height="128" Fill="{StaticResource CoreBrush}" Stroke="#5E55DFFF" StrokeThickness="1"/>
                            <TextBlock x:Name="CorePercent" Text="48%" Foreground="#F2FBFF" FontSize="31" FontWeight="SemiBold"
                                       HorizontalAlignment="Center" VerticalAlignment="Center"/>
                            <Canvas ClipToBounds="True" Width="146" Height="52" VerticalAlignment="Bottom" Margin="0,0,0,15">
                                <Path Data="M 0,28 C 20,4 36,48 58,24 S 95,4 118,26 S 145,46 170,18"
                                      Stroke="#55E2FF" StrokeThickness="3" Fill="Transparent">
                                    <Path.RenderTransform><TranslateTransform x:Name="WaveTranslate" X="-130"/></Path.RenderTransform>
                                </Path>
                            </Canvas>
                        </Grid>
                        <TextBlock x:Name="StageIndex" Text="阶段 03 / 05" Foreground="#7596AE" HorizontalAlignment="Center"/>
                        <TextBlock x:Name="CurrentTitle" Text="正在翻译字幕" Foreground="#F4FBFF" FontSize="27" FontWeight="SemiBold"
                                   HorizontalAlignment="Center" Margin="0,8,0,0"/>
                        <TextBlock x:Name="CurrentDetail" Text="正在把日语对白整理为自然中文；音视频仍在本机。" Foreground="#8FA9BC"
                                   TextAlignment="Center" TextWrapping="Wrap" HorizontalAlignment="Center" Margin="0,8,0,0"/>
                        <Border x:Name="ProgressTrack" Height="10" Background="#08131F" BorderBrush="#2C6687A0" BorderThickness="1"
                                CornerRadius="5" Margin="0,24,0,0">
                            <Grid HorizontalAlignment="Left">
                                <Border x:Name="ProgressFill" Width="320" Background="{StaticResource ProgressBrush}" CornerRadius="4"/>
                            </Grid>
                        </Border>
                        <TextBlock x:Name="ProgressLabel" Text="48%" Foreground="#67DFFF" HorizontalAlignment="Right" Margin="0,7,0,0"/>
                        <Grid Margin="0,20,0,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/><ColumnDefinition Width="*"/><ColumnDefinition Width="*"/><ColumnDefinition Width="*"/><ColumnDefinition Width="*"/>
                            </Grid.ColumnDefinitions>
                            <StackPanel Grid.Column="0" HorizontalAlignment="Center"><Ellipse x:Name="StageDot0" Width="12" Height="12"/><TextBlock x:Name="StageText0" Text="准备" FontSize="11" Margin="0,7,0,0"/></StackPanel>
                            <StackPanel Grid.Column="1" HorizontalAlignment="Center"><Ellipse x:Name="StageDot1" Width="12" Height="12"/><TextBlock x:Name="StageText1" Text="识别" FontSize="11" Margin="0,7,0,0"/></StackPanel>
                            <StackPanel Grid.Column="2" HorizontalAlignment="Center"><Ellipse x:Name="StageDot2" Width="12" Height="12"/><TextBlock x:Name="StageText2" Text="翻译" FontSize="11" Margin="0,7,0,0"/></StackPanel>
                            <StackPanel Grid.Column="3" HorizontalAlignment="Center"><Ellipse x:Name="StageDot3" Width="12" Height="12"/><TextBlock x:Name="StageText3" Text="画面" FontSize="11" Margin="0,7,0,0"/></StackPanel>
                            <StackPanel Grid.Column="4" HorizontalAlignment="Center"><Ellipse x:Name="StageDot4" Width="12" Height="12"/><TextBlock x:Name="StageText4" Text="合成" FontSize="11" Margin="0,7,0,0"/></StackPanel>
                        </Grid>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,23,0,0">
                            <Button x:Name="AdvanceDemo" Content="演示推进" Style="{StaticResource PrimaryButton}" Margin="0,0,10,0"/>
                            <Button x:Name="ShowDetailToast" Content="阶段详情" Style="{StaticResource SecondaryButton}"/>
                        </StackPanel>
                    </StackPanel>
                </Border>
            </Grid>

            <Grid x:Name="SuccessScreen" Visibility="Collapsed">
                <Border Style="{StaticResource PanelStyle}" Padding="44" MaxWidth="720" HorizontalAlignment="Center" VerticalAlignment="Center">
                    <StackPanel>
                        <Border Width="112" Height="112" CornerRadius="56" Background="#351BD79A" BorderBrush="#7057F3D0" BorderThickness="1" HorizontalAlignment="Center">
                            <TextBlock Text="✓" Foreground="#9BFFE0" FontSize="48" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <TextBlock Text="成片已经准备好" Foreground="#F4FBFF" FontSize="29" FontWeight="SemiBold" HorizontalAlignment="Center" Margin="0,20,0,0"/>
                        <TextBlock Text="字幕、画面与声音已经合成。这里仍然只是视觉演示。" Foreground="#8FA9BC" TextAlignment="Center" Margin="0,10,0,0"/>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,24,0,0">
                            <Button x:Name="OpenFolderDemo" Content="打开成片位置" Style="{StaticResource PrimaryButton}" Margin="0,0,10,0"/>
                            <Button x:Name="NextDemo" Content="制作下一部" Style="{StaticResource SecondaryButton}"/>
                        </StackPanel>
                    </StackPanel>
                </Border>
            </Grid>

            <Grid x:Name="FailureScreen" Visibility="Collapsed">
                <Border Style="{StaticResource PanelStyle}" Padding="44" MaxWidth="720" HorizontalAlignment="Center" VerticalAlignment="Center">
                    <StackPanel>
                        <Border Width="112" Height="112" CornerRadius="56" Background="#342B1425" BorderBrush="#805D354D" BorderThickness="1" HorizontalAlignment="Center">
                            <TextBlock Text="!" Foreground="#FFD1DA" FontSize="38" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <TextBlock Text="这次没有完成" Foreground="#F4FBFF" FontSize="29" FontWeight="SemiBold" HorizontalAlignment="Center" Margin="0,20,0,0"/>
                        <TextBlock Text="字幕识别意外停止。源片没有被改动，已经完成的步骤仍然保留。"
                                   Foreground="#8FA9BC" TextAlignment="Center" TextWrapping="Wrap" Margin="0,10,0,0"/>
                        <Border Background="#9E030C16" BorderBrush="#237ED3FF" BorderThickness="1" CornerRadius="13" Padding="15" Margin="0,22,0,0">
                            <TextBlock Text="建议：确认显卡没有被其他程序占用，然后继续恢复。" Foreground="#C8DCE9" TextWrapping="Wrap"/>
                        </Border>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,22,0,0">
                            <Button x:Name="RetryDemo" Content="继续恢复" Style="{StaticResource PrimaryButton}" Margin="0,0,10,0"/>
                            <Button x:Name="NewDemo" Content="使用新任务" Style="{StaticResource SecondaryButton}"/>
                        </StackPanel>
                        <Expander Header="查看技术详情" Foreground="#9AB6CA" Margin="0,20,0,0">
                            <Border Background="#A8020A12" CornerRadius="10" Padding="12" Margin="0,9,0,0">
                                <TextBlock Text="演示错误：GPU_RESOURCE_BUSY · 任务恢复点已保留" Foreground="#91AABD"/>
                            </Border>
                        </Expander>
                    </StackPanel>
                </Border>
            </Grid>

            <Border x:Name="Toast" Visibility="Collapsed" HorizontalAlignment="Right" VerticalAlignment="Bottom"
                    Background="#F210273B" BorderBrush="#405FDBFF" BorderThickness="1" CornerRadius="11"
                    Padding="14,10" Margin="0,0,0,4">
                <TextBlock x:Name="ToastText" Foreground="#D9F6FF"/>
            </Border>

            <Grid x:Name="ConfirmOverlay" Visibility="Collapsed" Background="#D6020812">
                <Border Width="500" Style="{StaticResource PanelStyle}" Padding="26" HorizontalAlignment="Center" VerticalAlignment="Center">
                    <StackPanel>
                        <TextBlock Text="确认本次制作" Foreground="#F4FBFF" FontSize="23" FontWeight="SemiBold"/>
                        <TextBlock Text="本次只会把字幕文本发送给 DeepSeek；音视频始终留在本机。"
                                   Foreground="#91AABD" TextWrapping="Wrap" Margin="0,10,0,0"/>
                        <Border Background="#A9040E18" CornerRadius="12" Padding="14" Margin="0,18,0,0">
                            <StackPanel>
                                <Grid><TextBlock Text="翻译场景" Foreground="#7F9DB4"/><TextBlock x:Name="ConfirmScene" Text="动漫" Foreground="#D7EAFF" HorizontalAlignment="Right"/></Grid>
                                <Grid Margin="0,9,0,0"><TextBlock Text="输出位置" Foreground="#7F9DB4"/><TextBlock Text="D:\成片输出" Foreground="#D7EAFF" HorizontalAlignment="Right"/></Grid>
                                <Grid Margin="0,9,0,0"><TextBlock Text="2 倍插帧" Foreground="#7F9DB4"/><TextBlock x:Name="ConfirmInterpolation" Text="未开启" Foreground="#D7EAFF" HorizontalAlignment="Right"/></Grid>
                            </StackPanel>
                        </Border>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,20,0,0">
                            <Button x:Name="CancelConfirm" Content="返回修改" Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"/>
                            <Button x:Name="ConfirmStart" Content="同意并开始" Style="{StaticResource PrimaryButton}"/>
                        </StackPanel>
                    </StackPanel>
                </Border>
            </Grid>
        </Grid>
    </Grid>
</Window>
'@

$Reader = New-Object System.Xml.XmlNodeReader $Xaml
$Window = [Windows.Markup.XamlReader]::Load($Reader)

function Get-Control([string]$Name) {
    $control = $Window.FindName($Name)
    if ($null -eq $control) {
        throw "WPF prototype control not found: $Name"
    }
    return $control
}

$ReadyScreen = Get-Control "ReadyScreen"
$RunningScreen = Get-Control "RunningScreen"
$SuccessScreen = Get-Control "SuccessScreen"
$FailureScreen = Get-Control "FailureScreen"
$ShowReady = Get-Control "ShowReady"
$ShowRunning = Get-Control "ShowRunning"
$ShowSuccess = Get-Control "ShowSuccess"
$ShowFailure = Get-Control "ShowFailure"
$SimpleMode = Get-Control "SimpleMode"
$AdvancedMode = Get-Control "AdvancedMode"
$AdvancedPanel = Get-Control "AdvancedPanel"
$DropZone = Get-Control "DropZone"
$DemoFileName = Get-Control "DemoFileName"
$ChooseDemoFile = Get-Control "ChooseDemoFile"
$ChooseOutput = Get-Control "ChooseOutput"
$AnimeScene = Get-Control "AnimeScene"
$FilmScene = Get-Control "FilmScene"
$AdultScene = Get-Control "AdultScene"
$ApiKeyPassword = Get-Control "ApiKeyPassword"
$ApiKeyPlain = Get-Control "ApiKeyPlain"
$ToggleKey = Get-Control "ToggleKey"
$DemosaicToggle = Get-Control "DemosaicToggle"
$EnhanceToggle = Get-Control "EnhanceToggle"
$InterpolationToggle = Get-Control "InterpolationToggle"
$StartDemo = Get-Control "StartDemo"
$ConfirmOverlay = Get-Control "ConfirmOverlay"
$ConfirmScene = Get-Control "ConfirmScene"
$ConfirmInterpolation = Get-Control "ConfirmInterpolation"
$CancelConfirm = Get-Control "CancelConfirm"
$ConfirmStart = Get-Control "ConfirmStart"
$ProgressTrack = Get-Control "ProgressTrack"
$ProgressFill = Get-Control "ProgressFill"
$ProgressLabel = Get-Control "ProgressLabel"
$CorePercent = Get-Control "CorePercent"
$StageIndex = Get-Control "StageIndex"
$CurrentTitle = Get-Control "CurrentTitle"
$CurrentDetail = Get-Control "CurrentDetail"
$WaveTranslate = Get-Control "WaveTranslate"
$CoreRotate = Get-Control "CoreRotate"
$CorePulse = Get-Control "CorePulse"
$AdvanceDemo = Get-Control "AdvanceDemo"
$ShowDetailToast = Get-Control "ShowDetailToast"
$OpenFolderDemo = Get-Control "OpenFolderDemo"
$NextDemo = Get-Control "NextDemo"
$RetryDemo = Get-Control "RetryDemo"
$NewDemo = Get-Control "NewDemo"
$Toast = Get-Control "Toast"
$ToastText = Get-Control "ToastText"

$script:SelectedScene = "动漫"
$script:DemoProgress = 48
$script:ToastTimer = $null
$script:KeyTimer = $null

$StageDots = @(0..4 | ForEach-Object { Get-Control "StageDot$_" })
$StageTexts = @(0..4 | ForEach-Object { Get-Control "StageText$_" })
$StateButtons = @{
    ready = $ShowReady
    running = $ShowRunning
    success = $ShowSuccess
    failure = $ShowFailure
}
$StateScreens = @{
    ready = $ReadyScreen
    running = $RunningScreen
    success = $SuccessScreen
    failure = $FailureScreen
}
$SceneButtons = @($AnimeScene, $FilmScene, $AdultScene)

function Convert-ToBrush([string]$Color) {
    return (New-Object System.Windows.Media.BrushConverter).ConvertFromString($Color)
}

function Set-ButtonActive($Button, [bool]$Active) {
    if ($Active) {
        $Button.Background = Convert-ToBrush "#5C1C69C9"
        $Button.BorderBrush = Convert-ToBrush "#704FD0FF"
        $Button.Foreground = Convert-ToBrush "#F1FBFF"
    }
    else {
        $Button.Background = [Windows.Media.Brushes]::Transparent
        $Button.BorderBrush = [Windows.Media.Brushes]::Transparent
        $Button.Foreground = Convert-ToBrush "#7F9DB4"
    }
}

function Set-SceneActive($Button, [bool]$Active) {
    if ($Active) {
        $Button.Background = Convert-ToBrush "#B31A4772"
        $Button.BorderBrush = Convert-ToBrush "#6B4CD9FF"
        $Button.Foreground = Convert-ToBrush "#F3FBFF"
    }
    else {
        $Button.Background = Convert-ToBrush "#9405101C"
        $Button.BorderBrush = Convert-ToBrush "#237ED3FF"
        $Button.Foreground = Convert-ToBrush "#9DB5C8"
    }
}

function Show-Toast([string]$Message) {
    if ($null -ne $script:ToastTimer) {
        $script:ToastTimer.Stop()
    }
    $ToastText.Text = $Message
    $Toast.Visibility = [Windows.Visibility]::Visible
    $script:ToastTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:ToastTimer.Interval = [TimeSpan]::FromMilliseconds(2200)
    $script:ToastTimer.Add_Tick({
        $script:ToastTimer.Stop()
        $Toast.Visibility = [Windows.Visibility]::Collapsed
    })
    $script:ToastTimer.Start()
}

function Hide-DemoKey {
    if ($null -ne $script:KeyTimer) {
        $script:KeyTimer.Stop()
        $script:KeyTimer = $null
    }
    $ApiKeyPassword.Visibility = [Windows.Visibility]::Visible
    $ApiKeyPlain.Visibility = [Windows.Visibility]::Collapsed
    $ToggleKey.Content = "显示"
}

function Reset-DemoKey {
    $ApiKeyPassword.Password = "demo-key-not-real-9Q2M"
    $ApiKeyPlain.Text = "demo-key-not-real-9Q2M"
    Hide-DemoKey
}

function Set-DemoState([string]$State) {
    foreach ($name in $StateScreens.Keys) {
        $StateScreens[$name].Visibility = if ($name -eq $State) {
            [Windows.Visibility]::Visible
        }
        else {
            [Windows.Visibility]::Collapsed
        }
        Set-ButtonActive $StateButtons[$name] ($name -eq $State)
    }
    $ConfirmOverlay.Visibility = [Windows.Visibility]::Collapsed
    Hide-DemoKey
    if ($State -eq "ready") {
        Reset-DemoKey
    }
    if ($State -eq "running") {
        $script:DemoProgress = 48
        Set-DemoProgress $script:DemoProgress
    }
}

function Set-DemoMode([bool]$Advanced) {
    $AdvancedPanel.Visibility = if ($Advanced) {
        [Windows.Visibility]::Visible
    }
    else {
        [Windows.Visibility]::Collapsed
    }
    Set-ButtonActive $SimpleMode (-not $Advanced)
    Set-ButtonActive $AdvancedMode $Advanced
}

function Set-DemoScene($ActiveButton) {
    $script:SelectedScene = [string]$ActiveButton.Tag
    foreach ($button in $SceneButtons) {
        Set-SceneActive $button ($button -eq $ActiveButton)
    }
}

function Update-ProgressWidth {
    if ($ProgressTrack.ActualWidth -le 2) {
        return
    }
    $usable = [Math]::Max(0, $ProgressTrack.ActualWidth - 2)
    $ProgressFill.Width = $usable * ($script:DemoProgress / 100.0)
}

function Set-DemoProgress([int]$Percent) {
    $script:DemoProgress = [Math]::Max(0, [Math]::Min(100, $Percent))
    $ProgressLabel.Text = "$($script:DemoProgress)%"
    $CorePercent.Text = "$($script:DemoProgress)%"
    Update-ProgressWidth

    $stages = @(
        @{ min = 0; title = "正在准备任务"; detail = "正在检查影片与输出位置。" },
        @{ min = 20; title = "正在识别对白"; detail = "正在本机识别日语声音并整理字幕。" },
        @{ min = 40; title = "正在翻译字幕"; detail = "正在把日语对白整理为自然中文；音视频仍在本机。" },
        @{ min = 65; title = "正在处理画面"; detail = "正在执行本次选择的画面处理步骤。" },
        @{ min = 86; title = "正在合成并检查"; detail = "正在加入中文字幕并完成最后检查。" }
    )
    $active = 0
    for ($index = 0; $index -lt $stages.Count; $index++) {
        if ($script:DemoProgress -ge $stages[$index].min) {
            $active = $index
        }
    }
    $StageIndex.Text = "阶段 {0:D2} / 05" -f ($active + 1)
    $CurrentTitle.Text = $stages[$active].title
    $CurrentDetail.Text = $stages[$active].detail

    for ($index = 0; $index -lt 5; $index++) {
        if ($index -lt $active) {
            $StageDots[$index].Fill = Convert-ToBrush "#45D7EF"
            $StageDots[$index].Stroke = Convert-ToBrush "#8CEFFF"
            $StageDots[$index].Effect = $null
            $StageTexts[$index].Foreground = Convert-ToBrush "#8DDFF2"
        }
        elseif ($index -eq $active) {
            $StageDots[$index].Fill = Convert-ToBrush "#2385D8"
            $StageDots[$index].Stroke = Convert-ToBrush "#8CEFFF"
            $StageDots[$index].Effect = New-Object Windows.Media.Effects.DropShadowEffect -Property @{
                Color = [Windows.Media.ColorConverter]::ConvertFromString("#47DAFF")
                BlurRadius = 14
                ShadowDepth = 0
                Opacity = 0.55
            }
            $StageTexts[$index].Foreground = Convert-ToBrush "#EAFAFF"
        }
        else {
            $StageDots[$index].Fill = Convert-ToBrush "#091521"
            $StageDots[$index].Stroke = Convert-ToBrush "#35546A"
            $StageDots[$index].Effect = $null
            $StageTexts[$index].Foreground = Convert-ToBrush "#59778D"
        }
        $StageDots[$index].StrokeThickness = 2
    }
}

function Start-PrototypeAnimations {
    $waveAnimation = New-Object Windows.Media.Animation.DoubleAnimation
    $waveAnimation.From = -130
    $waveAnimation.To = -30
    $waveAnimation.Duration = [Windows.Duration]::new([TimeSpan]::FromSeconds(2.6))
    $waveAnimation.RepeatBehavior = [Windows.Media.Animation.RepeatBehavior]::Forever
    $WaveTranslate.BeginAnimation([Windows.Media.TranslateTransform]::XProperty, $waveAnimation)

    $rotateAnimation = New-Object Windows.Media.Animation.DoubleAnimation
    $rotateAnimation.From = 0
    $rotateAnimation.To = 360
    $rotateAnimation.Duration = [Windows.Duration]::new([TimeSpan]::FromSeconds(14))
    $rotateAnimation.RepeatBehavior = [Windows.Media.Animation.RepeatBehavior]::Forever
    $CoreRotate.BeginAnimation([Windows.Media.RotateTransform]::AngleProperty, $rotateAnimation)

    $pulseAnimation = New-Object Windows.Media.Animation.DoubleAnimation
    $pulseAnimation.From = 0.45
    $pulseAnimation.To = 1.0
    $pulseAnimation.Duration = [Windows.Duration]::new([TimeSpan]::FromSeconds(1.3))
    $pulseAnimation.AutoReverse = $true
    $pulseAnimation.RepeatBehavior = [Windows.Media.Animation.RepeatBehavior]::Forever
    $CorePulse.BeginAnimation([Windows.UIElement]::OpacityProperty, $pulseAnimation)
}

function Save-WindowScreenshot([string]$Path) {
    $targetDirectory = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($targetDirectory) -or -not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
        throw "截图目标文件夹不存在。"
    }
    $renderTarget = $Window.Content
    $width = [Math]::Max(1, [int][Math]::Ceiling($renderTarget.ActualWidth))
    $height = [Math]::Max(1, [int][Math]::Ceiling($renderTarget.ActualHeight))
    $bitmap = New-Object System.Windows.Media.Imaging.RenderTargetBitmap(
        $width,
        $height,
        96,
        96,
        [System.Windows.Media.PixelFormats]::Pbgra32
    )
    $bitmap.Render($renderTarget)
    $encoder = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
    $encoder.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create)
    try {
        $encoder.Save($stream)
    }
    finally {
        $stream.Dispose()
    }
}

$ShowReady.Add_Click({ Set-DemoState "ready" })
$ShowRunning.Add_Click({ Set-DemoState "running" })
$ShowSuccess.Add_Click({ Set-DemoState "success" })
$ShowFailure.Add_Click({ Set-DemoState "failure" })
$SimpleMode.Add_Click({ Set-DemoMode $false })
$AdvancedMode.Add_Click({ Set-DemoMode $true })
$AnimeScene.Add_Click({ Set-DemoScene $AnimeScene })
$FilmScene.Add_Click({ Set-DemoScene $FilmScene })
$AdultScene.Add_Click({ Set-DemoScene $AdultScene })

$ChooseDemoFile.Add_Click({
    $DemoFileName.Text = if ($DemoFileName.Text -like "星海列车*") { "演示影片_新选择.mkv" } else { "星海列车_第03话.mkv" }
    Show-Toast "视觉原型没有读取真实文件"
})
$ChooseOutput.Add_Click({ Show-Toast "视觉原型不会打开真实文件夹" })
$DropZone.Add_DragEnter({
    $DropZone.BorderBrush = Convert-ToBrush "#A053DDFF"
    $DropZone.Background = Convert-ToBrush "#E00D2F45"
})
$DropZone.Add_DragLeave({
    $DropZone.BorderBrush = Convert-ToBrush "#267ED3FF"
    $DropZone.Background = Convert-ToBrush "#DB0B1B2D"
})
$DropZone.Add_Drop({
    $DemoFileName.Text = "演示影片_已放入.mkv"
    $DropZone.BorderBrush = Convert-ToBrush "#267ED3FF"
    $DropZone.Background = Convert-ToBrush "#DB0B1B2D"
    Show-Toast "已演示拖放；没有读取真实文件"
})

$ToggleKey.Add_Click({
    if ($ApiKeyPlain.Visibility -eq [Windows.Visibility]::Visible) {
        Hide-DemoKey
        return
    }
    $ApiKeyPlain.Text = $ApiKeyPassword.Password
    $ApiKeyPassword.Visibility = [Windows.Visibility]::Collapsed
    $ApiKeyPlain.Visibility = [Windows.Visibility]::Visible
    $ToggleKey.Content = "隐藏"
    if ($null -ne $script:KeyTimer) { $script:KeyTimer.Stop() }
    $script:KeyTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:KeyTimer.Interval = [TimeSpan]::FromSeconds(8)
    $script:KeyTimer.Add_Tick({ Hide-DemoKey })
    $script:KeyTimer.Start()
})

$StartDemo.Add_Click({
    $ConfirmScene.Text = $script:SelectedScene
    $ConfirmInterpolation.Text = if ([bool]$InterpolationToggle.IsChecked) { "已开启" } else { "未开启" }
    $ConfirmOverlay.Visibility = [Windows.Visibility]::Visible
})
$CancelConfirm.Add_Click({ $ConfirmOverlay.Visibility = [Windows.Visibility]::Collapsed })
$ConfirmStart.Add_Click({
    Hide-DemoKey
    Set-DemoState "running"
})
$AdvanceDemo.Add_Click({
    Set-DemoProgress ([Math]::Min(100, $script:DemoProgress + 11))
    if ($script:DemoProgress -ge 100) {
        $timer = New-Object System.Windows.Threading.DispatcherTimer
        $timer.Interval = [TimeSpan]::FromMilliseconds(520)
        $timer.Add_Tick({
            $timer.Stop()
            Set-DemoState "success"
        }.GetNewClosure())
        $timer.Start()
    }
})
$ShowDetailToast.Add_Click({ Show-Toast "详细阶段将在正式接入后显示" })
$OpenFolderDemo.Add_Click({ Show-Toast "视觉原型不会打开真实文件夹" })
$NextDemo.Add_Click({ Set-DemoState "ready" })
$RetryDemo.Add_Click({
    Set-DemoState "running"
    Set-DemoProgress 34
})
$NewDemo.Add_Click({ Set-DemoState "ready" })
$ProgressTrack.Add_SizeChanged({ Update-ProgressWidth })

Set-DemoMode ([bool]$InitialAdvanced)
Set-DemoScene $AnimeScene
Set-DemoState $InitialState

if ($ValidateOnly) {
    Write-Output "Translation Flow WPF v2 visual prototype XAML loaded successfully."
    return
}

$Window.Add_ContentRendered({
    Start-PrototypeAnimations
    Update-ProgressWidth
    if (-not [string]::IsNullOrWhiteSpace($ScreenshotPath)) {
        [void]$Window.Dispatcher.BeginInvoke(
            [Action]{
                Save-WindowScreenshot $ScreenshotPath
                if ($CloseAfterScreenshot) {
                    $Window.Close()
                }
            },
            [System.Windows.Threading.DispatcherPriority]::ApplicationIdle
        )
    }
})

[void]$Window.ShowDialog()

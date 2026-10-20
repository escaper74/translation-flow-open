# 安装与运行手册

> 本文介绍安装、运行模式、检查点恢复和结果验证。模块结构与技术路线见 [项目说明](PROJECT_BRIEF.md)。

## 0. 阅读导航

| 现在想知道什么 | 直接看哪里 |
|---|---|
| 项目由哪些模块组成、为什么这样拆 | `PROJECT_BRIEF.md` 第 2–3 节 |
| 正常投片应该选哪个模式 | 本文第 1–3 节 |
| 中途失败后如何恢复 | 本文第 4 节 |
| “机器通过”和“最终通过”差在哪里 | 本文第 5 节 |
| 如何记录耗时、资源与验证边界 | 本文第 6 节 |
| 命令行入口与处理范围 | 本文第 7 节 |
| 并行、模型选择和开头校时如何运行 | 本文第 8–9 节 |

桌面首页为“去马、翻译、美颜”。去马、翻译先选择视频，再在准备页独立或双选；已有字幕视频换格式位于右上角设置。只选去马时不检查翻译服务；SRT 仍可通过翻译入口导入，不能用于去马。一个功能也没选时提示至少选择一项。

## 新机器安装

源码位于 `src/`，安装脚本位于 `scripts/`。桌面仍从根目录 `启动翻译流.cmd` 打开；命令行可用 `.\scripts\python.cmd -m <模块名>`，它自动加载本项目源码并优先使用 `.venv`，也可通过当前进程的 `TRANSLATION_FLOW_PYTHON` 指定 Python。使用下文的原生 `python` 或 `$Python` 命令前，在仓库根目录的同一 PowerShell 窗口设置 `$env:PYTHONPATH = (Resolve-Path .\src).Path`。

先按 README 建立 Python 3.12 x64 的 `.venv` 并运行 `-ValidateOnly`。主程序只用标准库；C 识别在单独的虚拟环境中安装。配置样例 `config/local.example.psd1` 可复制成被忽略的 `config/local.psd1`，只接受静态字符串，不写 Key、`$env` 或 PowerShell 表达式。

桌面 Python 顺序为显式参数/本地配置、项目 `.venv`、兼容已有用户的 Codex 内置 Python、PATH。没有 Codex 内置运行时不影响显式 Python 或 `.venv` 使用。桌面的工具根可由参数/本地配置指定；CLI 不读取 `local.psd1`，用 `--tool-root` 或当前进程的 `TRANSLATION_FLOW_TOOL_ROOT`。Jasna 和 Gate 的进程环境设置优先于本地配置。配置路径须指向当前机器实际安装的位置。

### 工具和模型的目录

以下 `<工具根>` 默认为源码下 `workflow/tools`，也可选短英文绝对路径并在桌面配置中填写 `ToolRoot`。先手工取得组件，源码不会自动提供它们：

| 用途 | 固定组件/来源 | 在工具根下的实际路径 |
|---|---|---|
| 视频探测、封装 | [Gyan FFmpeg 8.1.1 essentials](https://www.gyan.dev/ffmpeg/builds/) 的完整 Windows x64 包 | `ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe` 和 `ffprobe.exe` |
| A 识别 | [whisper.cpp v1.9.0](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.0) CUDA 12.4 x64 包，保留依赖 DLL | `whisper-cublas-12.4.0-bin-x64/Release/whisper-cli.exe` |
| A 默认模型 | [whisper.cpp GGML 模型库](https://huggingface.co/ggerganov/whisper.cpp/tree/main)，非量化 `ggml-large-v3-turbo.bin`，约 1.51 GiB | `LLPlayer-v0.3.0/whispermodels/ggml-large-v3-turbo.bin` |
| A 可选模型/旧任务 | 同一模型库，非量化 `ggml-large-v3.bin`，约 2.88 GiB | `LLPlayer-v0.3.0/whispermodels/ggml-large-v3.bin` |
| 开头局部校时（可选） | 同版 Whisper 的 `whisper-vad-speech-segments.exe` 与 [Silero v6.2.0 GGML](https://huggingface.co/ggml-org/whisper-vad/tree/main) | 前者放 `whisper-cublas-12.4.0-bin-x64/Release/`，后者放 `whisper-vad/ggml-silero-v6.2.0.bin` |
| C 识别环境 | faster-whisper 1.2.1 / CTranslate2 4.6.0，完整锁定列表见 `scripts/asr_fusion/requirements-faster-whisper.lock.txt` | `asr-lab/venvs/faster-whisper-1.2.1/Scripts/python.exe` |
| C 模型 | [Systran large-v3 固定快照](https://huggingface.co/Systran/faster-whisper-large-v3/tree/edaa852ec7e145841d8ffdb056a99866b5f0a478)，由下面脚本下载并核对 | `asr-lab/models/faster-whisper-large-v3/`，含 model.bin、tokenizer/config/vocabulary 等文件 |
| 画面处理 | [Jasna v0.10.0 Windows NVIDIA 包](https://github.com/Kruk2/jasna/releases/tag/v0.10.0)，完整解压到英文路径 | 默认 `jasna/jasna.exe`，或配置 `JasnaPath` |
| 美颜播放（可选） | MediaPipe Tasks Vision 0.10.21 + FaceLandmarker float16 v1；下方安装命令 | `beauty-player/` |

`LLPlayer-v0.3.0` 只是沿用的模型目录名，不要求下载 LLPlayer。也不要因下载站出现新版本就替换上述版本或改名假装一致。下载后核对上游校验值并保留对应许可；上游分发内容和链接可能变化，下载时仍需核对。第三方分发限制见 `THIRD_PARTY_NOTICES.txt`。

NVIDIA 驱动须满足所选 CUDA 和 Jasna 包要求；本项目未提供 AMD/CPU 的画面处理配置。模型文件、CUDA wheel 和视频检查点合计需要数 GiB 到数十 GiB，具体取决于输入。A 的临时路径必须是纯 ASCII；若自动选择的位置不可写，可仅在启动进程中设置 `TRANSLATION_FLOW_ASCII_WORK_ROOT` 到同盘可写英文目录。同盘可用硬链接避免反复复制模型。

### 安装 C 和美颜组件

以下命令需要联网，会下载依赖/模型；只在决定安装时执行。从源码根目录运行，`$Tools` 替换成与桌面 `ToolRoot` 相同的路径：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$Tools = Join-Path (Get-Location) 'workflow\tools'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\asr_fusion\setup-dependencies.ps1 -BootstrapPython $Python -LabRoot (Join-Path $Tools 'asr-lab')
```

脚本固定 C 模型 revision 和 7 个文件的大小/SHA256，使用仓库里的完整 pip 锁定列表，并保留安装清单；不修改系统 PATH。`-SkipModel` 只跳过模型，仍会下载/安装 CUDA 等依赖。C 环境使用 Python 3.12；锁定文件不能替代新机器上的安装验证。

美颜可单独显式安装（约 22 MiB），不运行媒体或人脸推理：

```powershell
& $Python -m beauty_player.setup --destination (Join-Path $Tools 'beauty-player')
```

### 只读检查

```powershell
& $Python -m translation_flow.diagnostics --mode video --provider codex --tool-root $Tools
& $Python -m translation_flow.diagnostics --mode video --provider deepseek --tool-root $Tools --asr-a-model large-v3
& $Python -m translation_flow.diagnostics --mode picture --tool-root $Tools --jasna 'C:\Tools\Jasna\jasna.exe' --acceptance-gate-report 'C:\TFWork\acceptance\reports\workflow-report.json'
```

API 服务替换成实际所选的 provider。`missing_paths` 列出必须补齐的文件；`optional_checks` 单列开头校时组件，缺失时警告但不升级为运行 Gate。`paths_available` 不代表文件内容正确、C 依赖可导入、登录有效、GPU 就绪或 Gate 内容通过。这里不启动任何媒体工具/模型，也不发送服务请求。

### 在新机器建立画面验收报告

这是**之后由用户显式执行的 GPU/媒体验收**，不是打开软件时的自动步骤。先按 Jasna 0.10.0 安装说明完成本机引擎编译；本项目配置为 BasicVSR++ / lada-yolo-v4 / batch 4 / fp16 / clip 90 / overlap 8 / HEVC CQ28，无 secondary restoration。版本的默认模型可能不同，必须核对 `plan` 输出。七个引擎的精确路径由 `workflow-plan` 输出并由 `workflow-preflight` 检查；引擎须来自当前 GPU/工具组合，源码不携带它们。

1. 准备一部有权使用且至少 95 分钟的本地样片。短样命令的 60 秒窗口固定在 `01:34:00`；这是现有约束，不能用不足长度的短片冒充通过。
2. 用下面只读计划检查源路径与工具。之后依次 `prepare-short`、`process-short`、`verify-short`，仅 GPU 步骤带确认开关。完成后人工观看 60 秒样片。

   ```powershell
   $Source = 'C:\Media\acceptance-input.mp4'   # 替换成你的样片
   $Short = 'C:\TFWork\short-check'           # 新目录
   $Jasna = 'C:\Tools\Jasna\jasna.exe'
   $env:TRANSLATION_FLOW_TOOL_ROOT = $Tools
   & $Python -m jasna_picture.cli plan --source $Source --work-dir $Short --jasna $Jasna
   & $Python -m jasna_picture.cli run --source $Source --work-dir $Short --jasna $Jasna --step prepare-short
   # 确认没有别的 GPU 工作、引擎已经编译好后才运行：
   & $Python -m jasna_picture.cli run --source $Source --work-dir $Short --jasna $Jasna --step process-short --confirm-gpu-ready --confirm-engines-ready
   & $Python -m jasna_picture.cli run --source $Source --work-dir $Short --jasna $Jasna --step verify-short
   ```

3. 短样技术通过且观看认可后，以 `logs/verify-short.json` 放行 30 分钟 acceptance。起点可选，但样片必须覆盖完整窗口；例子用开头 30 分钟。先执行相同参数的 `workflow-plan`（只打印）和 `workflow-preflight`（运行版本/媒体探测，无模型推理）。确认 GPU 空闲和短样观感后，才把命令换成 `workflow-run` 并增加下面两个开关：

   ```powershell
   & $Python -m jasna_picture.cli workflow-plan --scope acceptance --source $Source --start '00:00:00' --work-dir 'C:\TFWork\acceptance' --output 'C:\TFWork\acceptance.video-only.mkv' --gate-report (Join-Path $Short 'logs\verify-short.json') --jasna $Jasna
   # workflow-preflight 使用上述同一组参数。
   # workflow-run 使用上述参数，并加 --confirm-gpu-ready --confirm-gate-viewed。
   ```

4. 30 分钟技术通过并观看认可后，把实际生成的 `C:\TFWork\acceptance\reports\workflow-report.json` 填入本机 `AcceptanceGateReport`，再通过正式桌面处理新文件。完整模式还会核对报告 scope 与工具/路线。换硬件/工具后应重新核实相应证据，不复制个人旧报告、不手写通过状态。

本项目没有“一键安装全部工具并为新 GPU 自动验收”的功能。完整下载、驱动兼容、引擎编译和本机人工验收需分别完成，不能用路径检查或离线测试代替。

## 1. 桌面入口与运行模式

根目录 `启动翻译流.cmd` 是唯一用户入口；`scripts/预览新版界面.cmd` 只转发到同一入口。其他命令行入口也集中在 `scripts/`。

1. 点首页去马或翻译的“选择视频”，可多选；翻译入口兼容 SRT。美颜卡直接打开播放窗。
2. 独立勾选所需功能，选择保存位置与 MKV/MP4。
3. 点“开始处理”。需要字幕外发时沿用本次授权与所选服务检查；仅去马只检查本地去马与封装工具。
4. 自动运行，完成后点“打开结果”。中途停止会保留已完成检查点，从“任务”页按原请求继续。

### 字幕导入检查

- 导入视频后异步检查：先读取同名或 `片名.语言` 的 SRT/ASS/SSA/VTT 和内嵌字幕轨；中文标签明确的轨道直接视为已有字幕，未标语言的文本轨抽取短窗口判断中文对白。
- 没有找到时，最多在全片 12 个分散时间点抽取画面下部，用本机 Windows 中文 OCR 每批检查 4 帧；找到一句明确的中文对白就返回证据，不再检查后续批次，不检查整片覆盖率。字幕组标记、广告、仅含汉字的标题与明确日文文本不会单凭“有文字”认定为中文字幕。
- 检测到的文件自动跳过翻译；全部已有字幕时取消翻译勾选。可重新勾选翻译，或选“已有中文字幕也重新翻译”覆盖判断。批量分别分流，双选任务中已有字幕的文件只去马；全部无需处理时不创建任务、不检查翻译服务。
- 去马由用户勾选决定。仅去马时复制匹配的外挂字幕到成品旁，并沿用成品文件名，使播放器仍能找到；不覆盖不同内容的已有外挂字幕。
- “抽样未检测到”不能证明没有；工具缺失、中文 OCR 未安装、探测失败会显示“检查未完成”，保留原选择。字幕只出现在画面上部、对白稀少或 OCR 误识别时可能漏检，用户可以直接取消翻译。
- 原任务恢复沿用原请求和检查点，不按新的导入结果改变任务。检查结果只在当前窗口内复用，源文件、关联字幕或工具路径改变后重新检查。输入与抽帧/OCR 始终留在本机。

| 选择 | 正式执行路径 | 用户成品 |
|---|---|---|
| 仅精修字幕，视频输入 | `translation_flow.cli`：本地 A/C/fusion → 所选服务翻译润色 | 中文字幕、原画/原声音 MKV 或 MP4 |
| 仅精修字幕，SRT 输入 | `translation_flow.srt_desktop` | 中文字幕及检查报告 |
| 仅去马，视频输入 | `full_workflow.picture_cli` → Jasna → final_mux | 去马后 MKV/MP4，保留原声音及已有字幕 |
| 双选，视频输入 | `full_workflow.cli`：A/C/fusion 后字幕与去马并行 → final_mux | 去马后视频、原声音、精修中文字幕 |

字幕与双选可沿用七种已有供应商；双选不自动切换 Codex/模型。MKV 是字幕主线的正式检查点；选择 MP4 时自动追加 MP4，已完成 MKV 保留。仅去马不构造 provider 或字幕授权，也不要求输入 Key。MP4 不支持源片图形软字幕时提示改用 MKV；不能丢轨冒充成功。仅去马按当前段完成后停止，封装开始后完成验证和发布；双选用户停止同时通知两支，普通单支故障仍让另一支保留成果。

独立诊断入口：

- `scripts/run-jasna-picture.cmd`：画面计划、预检和正式处理。
- `scripts/run-final-mux.cmd`：只做最终薄封装。

这些 CLI 用于短样、排错和模块验收；普通生产由桌面入口调用。

### 1.1 后端工具配置与环境清单

后端默认从当前项目的 `workflow/tools` 定位既有固定工具布局。换机器或在另一份源码目录复用现有工具时，使用 `--tool-root <工具根目录>`，或在本次终端设置 `TRANSLATION_FLOW_TOOL_ROOT`。多份源码可以共享同一工具目录，无需复制大型模型。

桌面启动时也会读取 Git 已忽略的 `config/local.psd1`，仅消费 `PythonPath`、`ToolRoot`、`JasnaPath`、`AcceptanceGateReport` 四个本机路径。Python/工具根的显式启动参数优先；Jasna/验收报告的既有进程环境优先。本地配置不放 Key，不进入源码留档；命令行后端仍使用自己的参数/环境配置。升级代码时保留本地配置，避免改变工具、模型或验收报告。

完整流程另支持 `--jasna <jasna.exe>`、`--acceptance-gate-report <既有通过报告.json>`，对应环境变量为 `TRANSLATION_FLOW_JASNA`、`TRANSLATION_FLOW_ACCEPTANCE_REPORT`。未配置时分别定位工具根下的 `jasna/jasna.exe` 与 `acceptance/workflow-report.json`。原有工具版本、模型和验收检查保持；诊断不会生成验收报告，也不确认报告内容已通过。

只检查环境、不运行翻译或媒体：

```powershell
python -m translation_flow.diagnostics --mode srt --provider codex
python -m translation_flow.diagnostics --mode video --provider qwen --tool-root "已有工具根目录"
python -m translation_flow.diagnostics --mode full --provider codex --tool-root "已有工具根目录" --jasna "已有jasna.exe" --acceptance-gate-report "既有通过报告.json"
```

`paths_available` 只证明列出的路径存在；`missing_paths` 会列出缺失项并退出 1。不会验证登录、API 连通、GPU 或模型完整性，不读取 Key 值、不下载或安装依赖。独立 SRT 的 API 模式只需要项目 Python；Codex 模式还需要官方 CLI 及用户另行完成的本机登录。

### 1.2 字幕翻译小样

1. 在源码目录双击 `启动翻译流.cmd`，默认选择“精修中文字幕”。先使用 `tests/fixtures/api-acceptance.ja.srt` 的普通人工小样，拖入后确认提示“已识别字幕文件”，选一个独立输出目录；改放视频时自动改为识别对白，无需选择另一种任务类型。
2. 选择翻译方式。Codex 使用本机已有 ChatGPT 登录；其余方式填本次 API Key。新增五家需填写服务提供的准确模型 ID，Qwen/Kimi 的地域须与 Key 一致，输出 token 上限须受所选模型支持。界面不会代选新模型，切换服务或地域会清空旧 Key。
3. 模型、地域、输出上限、翻译风格、视频格式均在“更多设置”。需要指定 Python 时继续展开“本地工具”。独立 SRT 不需要 FFmpeg、Whisper 或 GPU；测试视频时才填写既有工具根目录。设置只在当前窗口及本次子进程生效，不修改系统或 Codex 全局配置。
4. 点“开始处理”，核对弹窗中的服务、模型、地域和字幕范围后确认。成功页核对真实 SRT 和报告；报告有疑点不代表已经人工确认译文。请求/事件/缓存保存在输出目录的 `.srt-translation/<job-id>`，不会保存 Key。
5. 用“停止并保留进度”检查停止边界；HTTP 当前请求允许返回并缓存，Codex 只停止自己创建的 CLI。任务中心选中该任务后“按原请求继续”，核对恢复的模型/地域/上限，再确认本次授权。已完成且校验匹配的 SRT 可无 Key、无新调用复用；未完成 API 任务可复用同一后台仍保留的本次 Key，后台重启后需要再次填写。
6. SRT 小样由用户确认后，再独立测试原画字幕的 MKV/可选 MP4。界面的正式视频模式仍走原后端和检查点，不以 SRT 小样代替视频处理验收。旧任务按其正式请求恢复。

### 1.3 通用队列与后台接口

桌面可多选或拖入多个文件，确认本批授权后顺序处理。在进度页停止只影响当前文件；“任务”页可以暂停后续项或停止并保留全部进度。任务交给后台并确认后可关闭窗口，重新打开同一源码目录的入口会接回队列；没有设置开机自启。恢复使用原请求和设置，同一后台仍保留本次 Key 时可以直接继续，后台重启后再填写；若要更换设置，应新建任务。

更新桌面脚本后，关闭旧窗口并从正式入口重开以加载新代码；已交给后台的任务继续运行。仅重开窗口不会热加载后台 Python，更新后台应等任务安全结束后使用正式 shutdown 接口，不为升级打断任务。不要直接改 queue.json 或请求文件。历史列表的“按原请求继续”还原设置并显示本次确认页；只有请求记录、尚未入队的任务也走这条路径。

长任务建议从 Windows 桌面双击入口启动。从受进程作业对象约束的终端启动时，子进程可能继承 `KILL_ON_JOB_CLOSE`，随外层应用退出而被结束。需要更换启动方式时，先通过界面安全停止并保留检查点，退出空闲队列，再重新打开入口并按原请求恢复。

队列默认位于 `%LocalAppData%\TranslationFlow\<项目路径摘要>`，不同源码目录分别保存。测试可通过桌面参数 `-QueueStateDirectory` 指定隔离目录。同步 RPC 和事件读取在独立 PowerShell runspace 执行；UI 每两秒查询一次，不占用绘制线程。Windows 环境同时包含 Path/PATH 时，QueueBridge 合并大小写重复的环境键，避免 .NET Framework 无法启动客户端。

正式 `.cmd` 使用 UTF-8 控制台；队列客户端同时接受有 BOM 和无 BOM 的 JSON。只有 request、没有事件/manifest/result 时，历史列表显示“请求已保存 / 无运行记录”，不将请求保存冒充为处理阶段已完成；后台错误在界面详情中保留。

进度页显示当前步骤、已用时间与“当前阶段预计还需”，详情中的总耗时与当前阶段耗时每秒更新。Whisper A/C 目前只报告阶段起止，页面明确显示对应阶段和“识别中 / 暂无可靠估算”，隐藏阶段权重百分比，避免起点的“2%”被误认为实时识别进度。进入后续阶段恢复总进度。估算仅在语境、初译和润色有足够的完成数量/时间观测后给出范围；阶段变化会重新估算，其余缺少数据时显示“正在估算”，超过两分钟无新观测显示“等待新的进度”。不承诺 ASR、封装或整个任务的倒计时。

新入口 `python -m translation_flow.queue_cli --state-dir <固定本机队列目录>` 从 stdin 读取一个 UTF-8 JSON 对象，stdout 返回单个 `{ok, queue}` 或 `{ok:false, error}`。无 stdin 内容等价于查询状态。客户端按需启动独立后台进程；客户端退出不会停止已经接收的任务。目录应固定在用户可写的本机应用数据位置，窗口重开后复用；不要把其中的 `worker.json` 提交或分享。

桌面调用 `Invoke-TranslationFlowQueue -PythonPath ... -WorkingDirectory ... -StateDirectory ... -Payload <hashtable>`，位于 `src/desktop/TranslationFlowQueueBridge.ps1`。调用返回 queue 对象，失败抛出可显示的中文错误；同步 RPC 由 `TranslationFlowQueueUi.ps1` 在后台 runspace 调用。原 `TranslationFlowProcessBridge.ps1` 与直接请求入口不变。关闭窗口只释放 UI 客户端，不发 `stop` 或 `shutdown`。

| action | 输入 | 行为 |
|---|---|---|
| `status` | 无其他必需字段 | 返回队列、后台 PID、代码目录与 Python 路径；不启动媒体任务 |
| `enqueue` | `items:[{request_path, tool_root?, api_key?}]`；含字幕任务必须 `authorized:true` | 无法读取或解析的请求单独跳过，有效请求顺序入队并返回 `submission`；同一正式请求重复提交不重启；不会解除已暂停的队列，暂停期间新增项标记 `paused`。桌面开始按钮另用 `resume` 启动本次 `submission.added` |
| `stop` | 可选 `id` | 有 ID 只停所选项；无 ID 停当前项并暂停等待项；当前项显示 `stopping`，等待后端安全结束 |
| `pause` | 可选 `id` | 暂停等待项；不打断已经开始的任务 |
| `resume` | `ids:[...]`；字幕任务必须 `authorized:true`，API Key 通过可选 `credentials:{队列id:本次key}` | 核对原请求未变，重排所选失败/停止/暂停项；同一后台进程内省略或留空 Key 会复用本次 Key，提供新 Key 则替换；未选中的暂停项不恢复；已完成项不重跑 |
| `shutdown` | 无 | 仅无运行中任务时退出后台，保留队列；不是关闭窗口操作 |

队列 ID 是稳定的请求路径标识，不代替正式 `job_id`。每项包含 `request_path`、`job_id`、`mode`、`source`、`provider`、`result_path`、`status`、`message`、`stage`、`progress` 及完成后的 `outputs`。状态为 `queued / starting / running / stopping / paused / stopped / failed / interrupted / completed`；`progress` 来自正式事件，不代表估计剩余时间。`has_session_key` 表示后台内存中是否还有该项的本次 Key，只返回布尔值。

后台将正式终态事件中的失败详情带入 `detail`、`error_code`、`recovery_hint`；启动或完成报告异常也提供对应原因与恢复提示，完整记录仍在该项的 `events.jsonl` 和报告中。`warning_count`、`last_warning` 供界面展示提醒，提示本身不暂停队列或将通过的产物改成失败。启动前任务记录、事件或完成报告短暂被占用时自动重试；恢复后清除队列 `error` 提示，不要求用户手动解除暂停。工具无法启动等真实失败保留该项并继续后续项。

最近一次入队反馈位于 `submission`：`added` 为新加入的队列 ID，`existing` 为已存在的 ID，`skipped` 为 `{request_path,message}` 列表，`message` 为本次摘要。界面应在提交后展示被跳过的输入，避免误以为全部已加入；全部请求无效时返回错误。字幕授权缺失、Key/工具目录参数类型错误或同一任务的请求设置改变仍使整次提交报错，不会悄悄改变任务设置。

队列接收五个正式入口：`translation_flow.cli`、`translation_flow.srt_desktop`、`full_workflow.cli`、`full_workflow.picture_cli`、`media_planning.cli`。UI 仍按原规则生成各任务 request，保持源文件、供应商、模型、地域、上限与恢复身份；新队列不替 UI 生成或修改请求。出于队列等待期间的配置一致性，原 request 若改变会拒绝启动/重试，应另建正式任务。仅去马请求位于 `.picture-workflow/<job-id>`，使用 `job.result.json`；双选继续使用 `.full-workflow/<job-id>` 的 `workflow.result.json`。

Key 通过 QueueBridge 的 UTF-8 stdin 与仅监听 `127.0.0.1` 的随机鉴权本地 RPC 进入后台内存，不放命令行、queue.json、request 或日志；RPC 明确绕过代理并禁止重定向。`worker.json` 中仅有本地控制连接信息及随机控制令牌。暂停、停止或失败保留同一后台进程内的 Key，继续时无需重复填写；成功完成后释放该项 Key。后台重启丢失内存中的 Key/本次授权，因此等待项暂停；已启动子进程可继续并由新后台按进程创建身份跟踪。继续任务仍需确认本次字幕外发范围。

停止按阶段采用不同边界：Whisper A/C 运行期间约每 0.2 秒检查取消，停止本任务拥有的转录进程树，目标是在几秒内退出并释放负载，不等待整个识别阶段；界面确认后台退出前仍显示“正在停止”。A 的成功分块保留，被打断分块的 `.incomplete` 标记阻止残缺 SRT/TXT 被当作成功，恢复时仅重算未完成分块。HTTP 翻译仍保留当前已发出批次的成功响应再停；重封装可以在媒体探测或短预检后停止，完整 mux 开始后完成验证和发布，再停后续任务。停止请求到达时若该项已经成功，显示完成并保留结果，不把它改成失败。不承诺断电后从任意帧继续，也没有增加开机自启、系统服务或自动登录功能。

## 2. 运行前检查

翻译或完整工作流每次必须明确：

1. 源片和输出目录；
2. 翻译风格；
3. 本次供应商和模型；
4. 本次只外发字幕文本的授权；
5. 完整工作流使用的Jasna 配置是否已有一次通过技术检查和人工观看的 30 分钟全局路线 Gate；
6. Whisper A/C 完成并退出后，画面才能使用 GPU。

Codex 不填 Key，使用官方 CLI 的本机 ChatGPT 登录与账户额度。新版提供“登录 ChatGPT”“重新检测”，找不到程序时提供官方安装说明；登录按钮启动官方浏览器授权，完成后自动重查，不索取、读取或保存账号密码。CLI 优先从 PATH 定位，Windows 桌面应用的标准版本目录作为后备，不固定版本哈希。在隔离工具沙箱中查不到登录，并不等于普通桌面身份未登录，应以正常本机身份检查。只读诊断仍为 `python -m translation_flow.cli --codex-status` 或 `codex login status`；“已登录”只证明认证状态，不代表已获得服务响应。

新任务在确认本次授权后、交给队列前先检查所需本地工具，并向所选服务发送一次固定公开测试词；不含影片或字幕内容，会消耗少量该服务额度。检查通过才启动，失败在界面说明原因。恢复任务仍交由原后端判断可复用成果，不强制重复连接测试；仅换格式不检查翻译登录、不调用服务。检查不保证之后的网络、额度、媒体解码或译文质量永远正常。若没有显式代理环境变量，CLI 子进程沿用当前 Windows 用户已有系统代理，不修改系统代理、全局 PATH 或 Codex 配置。

六家 API 的 Key 只通过子进程环境传入，后端读取后立即移除。密钥不进入任务文件、日志、报告、缓存、Git 或聊天。

重新封装模式只需明确源媒体、输出目录和 MKV/MP4 目标。它不需要 Key 或字幕外发授权，也不得在请求中伪造 provider、profile 或 consent。

切换供应商或模型时新建任务，以免混用缓存。下载工具、调用服务和运行 GPU 任务前，确认所需磁盘空间、账户额度和设备资源。

## 3. 完整处理顺序

顶层调度不是等整条翻译结束后才开画面。正式顺序是：

```text
Whisper A → Whisper C → A+C 融合 → 融合 SRT 正式结构检查
                                      ├→ 外部翻译、字幕验证、原画封装
                                      └→ Jasna 画面处理
两支都成功 → 最终薄封装
正式 MKV 通过 → 按用户选择可选追加 MP4
```

其中任一分支失败，另一分支继续完成并保留检查点；本次运行不进入最终汇合。下次从同一正式入口恢复时，只补未完成分支。画面 runner 在桌面模式不向 stdout 镜像媒体日志，避免与并发 JSONL 事件流互相污染；完整日志仍写入画面工作目录。

追加 MP4 不属于两条核心分支，也不进入核心 manifest 谱系。它只在最终 MKV 技术通过后调用现有 `media_planning`：视频和全部原音轨 stream-copy，中文字幕转为 mov_text。追加失败时正式 MKV 仍是技术候选，顶层结果和任务中心会单独显示 MP4 失败；排除兼容或输出冲突后可按原请求重试。

### 3.1 翻译

1. 本地工具、源媒体和请求预检；
2. 极短供应商连接检查；
3. Whisper A；
4. A 退出并记录 GPU released；
5. Whisper C；
6. C 退出并记录 GPU released；
7. A+C 保守融合，并对新任务执行有证据支持的开头局部校时（不改文字与后续时间）；
8. 在任何外部翻译前重新验证融合 SRT，并通知顶层启动画面分支；
9. 全片语境；
10. 按预计文字量把批次固定分成连续的前半片和后半片，两路分别按时间顺序初译，不互相领取批次；
11. 等待两路初译全部完成，按源 ID 合并并验证完整性；
12. 使用同样的固定前后两路润色，完成后再次按源 ID 合并；
13. 必要时一次假名残留修复；
14. 写入并重新解析正式中文 SRT；
15. 生成本地 Markdown 字幕检查表，按时间并列日文和最终中文；
16. 原画、全部原音轨和唯一默认 `zho` SubRip 封装；
17. FFprobe 和开头/中部/结尾短解码。

默认翻译批次不超过 60 条、5000 字符，并发固定为 2。初译使用全片语境和前后少量日文；润色必须等待完整初译，并可参考前后少量初译。相邻参考只帮助理解，不允许 API 返回参考 ID。可重试当前失败批次，不重复调用已经成功并落盘的批次；本地缓存写入失败直接停止，不能拆批制造重复 API 调用。无效 Key、明确余额/权限错误和用户取消直接失败。

Codex 在 ASR 前只做登录状态检查，不额外消耗一条连接测试字幕。A+C 后增加独立源文审查（详见 `PROJECT_BRIEF.md` 3.1.1），然后复用上面的全片语境、双路初译、逐条对照润色和结构验证。翻译检查表附源文/译文疑点，MKV 字幕轨标题标记 Codex，任务中心显示原供应商。Codex 与其他既有服务均可在桌面双选；切换功能保留所选供应商与模型。

Codex 每次 CLI 最长 300 秒，翻译调用最多 4 次尝试，源文审查最多 2 次；登录、额度、取消或本地落盘失败不按网络故障重试。默认使用 CLI 模型策略 `cli-default`，不承诺具体模型名称。Windows 输出目录需保持较短，避免批次缓存路径达到 260 字符；后端在任何 Codex/ASR 调用前检测并提示，用户无需修改注册表或全局设置。

Codex CLI 更新后，同任务仍复用已接受的源文审查、语境、初译和润色缓存。任务在 `codex-calls/.cache-identity-codex-subtitles-v1` 固定缓存身份；旧任务由正式适配器根据最早成功调用的版本记录自动沿用原身份，不移动或手改缓存。新任务的缓存身份不含 CLI 版本，当前实际版本仍逐次写入调用报告。风格、源文摘要、适配器版本、模型策略和处理选项继续参与缓存隔离；无效身份记录保留现场并停止。

若桌面应用在 ASR 等待期间更新并移除旧 CLI，首次创建翻译子进程遇到文件不存在时，自动定位模式会重新查找已安装 CLI、核对本地 ChatGPT 登录，再尝试启动一次；显式指定的 CLI 路径不被替换。只有尚未成功创建进程时可做此恢复，进程启动后的通信、结果读取、临时目录清理或审计写入失败不因此重发模型请求。错误提示保留异常类型与系统错误码，不记录原始异常内容、字幕或凭据。

### 3.1.1 独立 SRT：六家 API 与 Codex

已有日文 SRT 时，使用项目 Python 执行 `-m translation_flow.translation.cli`。这条入口只产生中文 SRT、可选 JSON 报告和翻译缓存，不启动 Whisper、媒体处理或画面分支。

| `--provider` | 默认目的地 / 可选 `--endpoint-profile` | 模型 |
|---|---|---|
| `deepseek` | `api.deepseek.com` / `deepseek` | 保留既有 V4 默认与模型范围 |
| `qwen` | 北京 `qwen-cn`；新加坡 `qwen-intl` | 必须 `--model` |
| `kimi` | 中国 `kimi-cn`；国际 `kimi-intl` | 必须 `--model` |
| `openai` | `api.openai.com/v1/responses` / `openai` | 必须 `--model` |
| `anthropic` | `api.anthropic.com/v1/messages` / `anthropic` | 必须 `--model` |
| `gemini` | Google `v1beta/openai/chat/completions` / `gemini` | 必须 `--model` |
| `codex` | 官方 Codex CLI + 本机 ChatGPT 登录；不填 API endpoint | 省略或 `cli-default` |

准备 `--input`、新的 `--output`、建议显式设置的 `--report` 和 `--cache-dir`，再选择 `--provider`、`--model`、`--profile normal` 或既有其他风格。六家 API 的 Key 只通过本次子进程的 `SUBTITLE_API_KEY` 传入，后端读取后移除；不提供命令行 Key 参数。必须带 `--consent-subtitle-text-to-provider`，值与所选 provider 一致；该授权包括语境、初译、润色和可能的假名修复。Qwen/Kimi 的 Key 与地域必须对应，程序不跨地域尝试。

Codex 不需要 Key，也不继承 `SUBTITLE_MODEL`；仅使用既有官方 CLI 适配器和本机 ChatGPT 登录。`--endpoint-profile`、`--max-output-tokens` 或非 `cli-default` 模型会在创建适配器前报错，不能拿 API 配置或 DeepSeek 授权放行 Codex。默认每次调用最长 300 秒，可用 `--timeout` 调整；API 原默认仍为 240 秒。独立 SRT 直接使用所提供的字幕文本，因没有 A/C 识别证据，不执行视频流程的 A+C 源文审查；译文疑点进入 JSON 报告的 `uncertainties`，不会混进中文 SRT。Codex 调用元数据在缓存目录下按输出路径隔离保存。

用户可在项目根目录，用已有项目 Python 执行以下小样命令（`python` 替换为本机 Python 路径即可）。执行时会把这 8 条人工编写的普通字幕发给 Codex，并使用当前账户额度：

```powershell
python -m translation_flow.translation.cli `
  --provider codex --consent-subtitle-text-to-provider codex `
  --input tests/fixtures/api-acceptance.ja.srt `
  --output .local/srt-codex/zh.srt --report .local/srt-codex/report.json `
  --cache-dir .local/srt-codex/cache --profile normal --batch-size 4
```

运行中按 Ctrl+C，会通知两路停止各自的 Codex 子进程，保留已完成批次并以退出码 2 结束；重新执行同一组参数补缺失批。已完成后验证缓存复用时，把输出和报告改成新的路径，保留输入和其他参数，正常情况下不新增翻译调用。已有结果即使带 `--force` 也不覆盖。登录、额度限制直接停止，不回退到付费 API。

旧 DeepSeek 命令及 `--consent-subtitle-text-to-deepseek` 保持兼容，但旧授权不能放行另一家。Qwen、Kimi、OpenAI、Anthropic、Gemini 默认使用 `--max-output-tokens 8192`；可按明确模型能力调整，不代表每个模型均有该上限。它们省略 temperature/thinking 参数，沿用模型默认值；`--temperature` 仍是旧 DeepSeek 的参数，不自动调整所选模型的参数。模型不支持当前参数时直接报错，不隐式删参数重调或改模型。

初译和润色仍是阶段内固定两路，润色等待全部初译成功。失败后重新执行同一组设置，复用成功缓存；输出上限、模型、服务商或端点改变会形成新缓存。已有最终 SRT/报告不覆盖；如需对照或演示缓存复用，选择新的输出/报告路径，沿用缓存目录。

Qwen、Kimi、OpenAI、Anthropic、Gemini 的错误处理：余额、权限、明确拒绝、截断及异常结束保留缓存后停止；连接/超时/暂时 HTTP 错误使用现有有限重试，不另套 SDK 重试层，不记录原始错误体。Qwen 的 `insufficient_quota` 按临时吞吐限流处理，不套用 OpenAI 的余额语义。截断时按明确模型上限增大 token cap，或减小批次后重跑。DeepSeek 仍沿用既有适配器及错误处理；其结束状态与错误处理能力以该适配器为准。各服务的连通、限流、质量及具体模型参数需有授权的小样验收；离线 HTTP 夹具测试不等于六家真实可用。

协议依据：[OpenAI Docs](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[Gemini 兼容 API](https://ai.google.dev/gemini-api/docs/openai)、[Qwen 端点](https://help.aliyun.com/zh/model-studio/base-url)、[Kimi 参数](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart)、[DeepSeek API](https://api-docs.deepseek.com/)。只用于实现依据，不替代真实测试。

数字秒格式的 `Retry-After` 在 0–300 秒范围内由唯一共享重试层尊重，超过 300 秒则保留缓存并提示稍后继续，不额外叠加适配器重试。

### 3.1.2 视频 API 后端

使用项目 Python 执行 `-m translation_flow.cli --request <本次请求.json>`，视频在本地走 A → C → 融合，随后由所选 API 走既有语境/固定两路初译/润色，输出中文 SRT、日中检查表和原画 MKV。各服务共用这条流水线。`full_workflow.cli --request` 同样接收这一翻译配置；画面模块与既有调度保持一致。

请求保留 schema 1、job_id、input_video、output_dir、profile_id 等既有字段。Qwen 的 provider 配置示例：

```json
{
  "id": "qwen",
  "model": "qwen-plus",
  "endpoint_profile": "qwen-cn",
  "max_output_tokens": 8192
}
```

`host` 可省略，由命名端点确定；填写时须与所选地域一致。`consent` 保留 `current_run`、`subtitle_text_only`、`audio_video_local` 三个 true，并为Qwen、Kimi、OpenAI、Anthropic、Gemini 带上匹配的 `provider`，例如 `"provider":"qwen"`。这些 true 必须来自本次明确同意，不能把保存过的授权当永久授权。Key 仍只由本次子进程环境 `SUBTITLE_API_KEY` 提供，不能放进 JSON。默认输出上限不代表模型通用上限，应以所选具体模型为准。

旧 DeepSeek 请求、模型/参数、三字段身份和恢复约束不变；不能为它注入新的通用 8192 上限。DeepSeek 原画任务支持 `delivery.additional_mp4`；正式 MKV 先保存，MP4 交付失败独立报告。Codex 的原画与完整流程均使用既有本机登录。现行桌面与 bridge 已接入七种 SRT/原画字幕选择，见 1.2；恢复时使用同一版本的入口。

API 任务的恢复继续使用同一 job_id、源片和公开配置：

- API 失败：保留 ASR 和成功批次；重新授权并提供 Key，只补缺失批，可能重新做一次连接小探测。
- 字幕已完成、封装失败：验证正式 ASR、翻译报告、字幕内容与配置后，无 Key 直接补封装，不重新识别/翻译。
- MKV 与报告已经完整：无 Key 验证后直接复用；原画请求可用既有 `delivery.additional_mp4` 追加本地 MP4。
- 完整工作流先复用已完成翻译结果；若只有完成字幕而封装失败，也可由翻译后端无 Key 补完再交回原编排。画面/最终交付仍按各自既有检查点恢复。
- 改服务商、模型、地域或输出上限时，另建任务和结果路径，不能混用已有终态。旧 DeepSeek/Codex 没有新字段的合法检查点继续兼容。

### 3.1.3 真实 API 小样验证方法

先选择已有账户的一家服务，明确模型、地域、外发字幕范围和调用预算。可使用 `tests/fixtures/api-acceptance.ja.srt` 的普通人工字幕；如果改用自己的字幕，先确认片段范围与使用权限。

保持正式语境、固定两路初译、全部初译完成后再润色的流程。用新的输出目录保存中文 SRT、报告和成功缓存，检查 ID、数量、时间码、空文本、阶段屏障及译文疑点。随后用同缓存、新输出路径重放，核对没有重复外部调用；失败时只补缺失批次，不手改缓存。

用户审阅译文后，再独立验证视频短样。单家服务或短样通过不能代表其他服务、所有模型或整片质量已通过。Key 只进入本次进程环境，不写进命令、文档或报告。

### 3.1.4 Codex 总流程、媒体输入与停止恢复

验证使用独立输出，不接管正在运行的任务。

**Codex 总流程。** 沿用第 3.1.2 节的视频请求，把 provider 设为 `{"id":"codex","host":"chatgpt.com","model":"cli-default"}`，`workflow_mode` 设为 `translation_picture_final`，并按本次选择填写已有 `consent` 和 `gates.picture_and_mux_30m_human_accepted`。运行命令为：

```powershell
python -m full_workflow.cli --request "本次请求.json" --tool-root "已有工具根目录" --jasna "已有jasna.exe" --acceptance-gate-report "既有通过报告.json"
```

Codex 不填 Key；A/C 退出、融合结构验证通过后，原有总流程开始另一分支，Codex 则进行源文审查/翻译。恢复验证 A/C/fusion、审查报告、独立日文参考及中文字幕的对应关系。只有封装未完成时不需要重新登录或识别；总流程的可选 MP4 只在最终 MKV 后追加，原画支线不再另做一次 MP4。

**媒体输入。** 新 ASR 任务先做本地实际轨道探测、首条音轨 1 秒解码和原画/全部音轨 1 秒 MKV 直封检查，报告写入 `asr/media-input.report.json`。文件扩展名不作为放行依据；MP4/MKV 优先，MOV/AVI/WMV 等同样由安装的 FFmpeg 验证。预检失败不进入 ASR 或翻译。它证明短样可处理，不代表全片或画面引擎支持所有编码；完整流程仍需通过画面工具和模型的预检。原画成片选择首条非封面视频轨并保留全部原音轨；额外视频轨和多音轨选择会有提示。

新任务的 `asr.input.json` 记录首条音轨策略，A、C 都识别音轨序号 0。旧部分任务没有此记录时保持原 A 自动选轨策略，不把新策略混进已有 A 分块；旧完整 ASR 检查点直接复用。中文字幕 SRT 和 MKV 始终保留，MP4 仍使用既有本地直封路线，遇到不兼容编码提示转码或使用 MKV，不自动转整片。

**停止与恢复。** 三个翻译 CLI 都可用 Ctrl+C 请求停止。视频/完整任务也识别 `.translation-flow/<job-id>/cancel.requested`；现行桌面通过队列使用原有取消机制。Codex 停止自己创建的 CLI 子进程，HTTP API 允许已发出的请求返回并保存成功批次，后续调用停止；限流退避期间也会响应停止。Whisper 等当前阶段安全退出。完整流程已启动的画面分支按原方式结束并保留成果，随后跳过最终汇合；不是立即杀死所有媒体进程。退出 2 和 `job_cancelled` 表示已停止；同一请求重新运行会清除旧停止标记并复用成功检查点。旧 DeepSeek 恢复仍需遵守原 Key 和终态约束。

**用户测试顺序。** 先执行 1.1 环境清单和 3.1.1 的普通 8 条 SRT 小样，确认译文/疑点并用新输出路径重放缓存；再选短视频验证原画 SRT/MKV/MP4、停止与恢复；最后再用已验收画面路线验证 Codex 总流程。六家服务的实际可用性、额度、速度、翻译质量和各容器播放体验分别由对应真实小样确认。离线夹具只覆盖程序行为，不能替代上述真实验证。

### 3.2 画面

1. 收到已验证的 A+C 正式交接后，校验 30 分钟全局路线 Gate 已通过、`scope=acceptance`，且 Jasna 工具/模型/编码参数与配置的处理参数一致；Gate 与当前源片是否同源只记录为信息性证据；
2. 读取源片首批视频 packet，跳过 discard 预卷，记录首个可呈现 PTS；
3. 将源视频 stream-copy 为约 15 分钟单视频段；
4. Jasna 顺序处理，每段直接输出最终 HEVC；
5. 每段检查单视频轨、编码、尺寸、帧率、时长、硬解码证据；
6. 完成段和段报告同时成立后才可成为检查点；
7. stream-copy 拼接完成段；
8. 检查总时长、统一流参数和开头/全部接缝/结尾解码；
9. 写入源起点、逐段映射和真实输出接缝。

命令格式：

```powershell
.\scripts\run-jasna-picture.cmd workflow-plan <参数>
.\scripts\run-jasna-picture.cmd workflow-preflight <同一组参数>
.\scripts\run-jasna-picture.cmd workflow-run <同一组参数> --confirm-gpu-ready --confirm-gate-viewed
```

整片 `scope=full` 必须从源片开头处理，并读取一份与处理参数匹配的 `scope=acceptance` 全局 Gate 报告。每部新源片的 30 分钟样片是可选质量检查，不是整片前置硬门；报告必须明确区分“路线已验收”与“当前源片已逐片验收”。具体参数以 `--help` 为准，不在文档复制机器路径。

### 3.3 最终汇合

输入：

- 画面报告指定的处理后纯视频轨；
- 同一只读源片的全部原音轨；
- 翻译主线正式中文 SRT；
- 画面报告中的真实源起点和接缝。

处理：

- 中文字幕只做窗口裁切、整体平移和连续编号，文本不修改；
- 视频与音频 stream-copy；
- 字幕封装为唯一默认 `zho` SubRip；
- 输出必须是新的 MKV，不覆盖任何输入或已有结果。

独立命令先 `plan`，确认后再 `run`：

```powershell
.\scripts\run-final-mux.cmd plan <参数>
.\scripts\run-final-mux.cmd run <同一组参数>
```

完整生产由顶层自动读取画面正式报告，不允许调用者手猜时间映射。

如果源画面已有硬字幕，任务又明确只做画面处理，独立薄汇合可以用 `--no-subtitle`：只将处理后视频和同一源片的全部原音轨 stream-copy 到新 MKV，技术报告必须确认输出没有额外软字幕轨。该模式不修改完整工作流必须使用正式中文 SRT 的契约。

### 3.4 已有中文字幕影片重新封装

从 `启动翻译流.cmd` 选择“重新封装已有中文字幕”，再选择输出容器。后端顺序是：

1. FFprobe 读取实际 format/streams，不用文件后缀作结论；
2. attached picture 不算主视频；可用视频不是恰好一条时停止；
3. 只认 FFprobe 语言标签明确标记的中文文本字幕；没有候选、候选多于一条或仅有图形字幕时停止；
4. 显式映射唯一视频、全部原音轨和唯一中文字幕；不需要选某条音轨，因为全部保留；
5. 静态规划已知需转码时，不调用 FFmpeg 封装；规划允许时，再用当前 FFmpeg 做不超过 1 秒的同参数候选预检；
6. 预检通过后才生成整片候选，其中视频/音频 `copy`，MKV 字幕为 SubRip，MP4 字幕为 mov_text；
7. 通过容器、轨道、codec、时长和开头/中部/结尾短解码后，才独占发布正式输出和技术报告。

MP4 静态规划或 FFmpeg 预检失败时，界面必须明确显示“需要转码或改用 MKV”，且不得发布正式输出。当前不提供烧录字幕、视频/音频转码、图形字幕 OCR 或多轨选择 UI。稳定任务记录位于输出目录的 `.media-repackage/<job-id>`；源媒体和已有结果始终不覆盖。

放大到真实长样前，先用合成短样验证默认 MKV，再用已知不兼容短样验证 MP4 早停。长样 MP4 验收应从当前安装版本的 `启动翻译流.cmd` 运行；机器检查通过后，仍由用户播放开头/中段/结尾、拖动、判断同步并开关字幕。合成结果不能写成人工验收已通过。

### 3.5 本地任务中心

任务中心默认读取项目 `deliverables`，选择其他输出目录后只读取该目录下的：

- `.translation-flow/<job-id>/request.json`、`events.jsonl`、`job.result.json`；
- `.full-workflow/<job-id>/request.json`、`events.jsonl`、`workflow.json`、`workflow.result.json`；
- `.media-repackage/<job-id>/request.json`、`events.jsonl`、`technical-validation.report.json`。

它不建立新数据库，不读取大媒体内容，也不把没有终态证据的任务猜成“仍在运行”；没有成功 result 或明确失败终态时显示“未完成 / 可恢复”。刷新时保留当前选择；没有选择时自动选中最新可恢复任务，并在操作前再次收敛已经退出但界面尚未终结的后端状态，避免“任务可恢复但按钮仍为灰色”。“按原请求继续”还原原供应商/模型等正式设置：媒体任务直接本地运行；Codex 任务无 Key 并走当次字幕授权；DeepSeek 仅翻译没有当次 Key 时只载入、不启动；完整任务仍显示当次授权页，后端决定是复用已验证翻译还是在外部调用前停止。任何含 API Key 字段的异常 request 都拒绝载入。

## 4. 检查点与恢复

### 翻译

- A/C、融合、翻译批次和完整翻译结果各自有独立检查点。
- 不同源身份、翻译风格、供应商或模型不能混用缓存。
- 并发数只改变调度，不改变 prompt 或缓存身份；同一批次可在串行/两路之间复用。
- 完整翻译报告、SRT、原画 MKV 和技术报告全部匹配时，后续恢复可无 Key 复用。
- 不完整翻译或需要新外部调用时，仍须重新取得当次字幕授权；Codex 需有效 ChatGPT 登录，DeepSeek 需 Key。
- 桌面的“停止并保留进度”写入该任务的 `cancel.requested`。Codex、六家 API 及完整流程均支持该标记和 Ctrl+C；具体安全停止边界见 3.1.4。CLI 发出 `job_cancelled` 并退出 2，同任务恢复清除旧信号并复用成功批次。Codex 登录失效或额度限制保留批次，绝不回退付费 API。
- Codex 的 `source-review.pointer.json` 指向独立日文参考与源文审查，`translation.report.json` 包含译文摘要和逐批疑点。完成翻译恢复校验源身份、SRT 摘要、对应报告及 MKV 文件状态；仅追加 MP4 不改变核心任务身份。不要手工修改这些文件。
- 画面在 A+C 后已经完成、但翻译分支失败时，下一次恢复复用画面正式报告，不重跑已完成画面。

### 画面

- 只有完成段与 `technical_checks_passed` 段报告成对存在时才复用。
- prepare 媒体已经成功但报告未落盘时，只有正式日志返回 0、分段连续、参数一致且总时长通过，workflow 才能补建 checkpoint。
- 媒体已完成但报告未发布时，workflow 会重新核对成功日志、硬解码证据、轨道、编码、时长与抽点解码；成立的 `.working.mkv` 或无报告成品自动晋升并补建正式报告。
- 不能证明完整的 `.working.mkv` 不删除、不冒充完成；自动改名为 `.interrupted-*` 保留后，只重跑当前段或最终拼接。损坏容器和不连续分段仍拒绝复用。
- 旧任务如果仅因 Gate 与新源片不同源而在 picture/final 产物产生前失败，修正正式 Gate 政策后从同一入口恢复；必须重新验证已完成翻译谱系，不手改 manifest，不重跑 API/Whisper。
- 完整工作流恢复时，桌面入口可不填 API Key；后端只有在正式翻译结果通过谱系验证后才复用并继续画面/汇合，否则在 Whisper、画面和外部调用前明确停止。DeepSeek 仅翻译新任务仍必须提供当次 API Key。
- 旧完整工作流 request 没有 `delivery` 时按“只交付 MKV”处理；以后选择追加 MP4 不改变 stable job id 或核心 manifest，可复用已完成的翻译、画面和 FinalMux，只运行或复用交付阶段。

### 最终汇合

- 只有输入谱系、源起点、接缝、输出媒体和技术报告全部匹配时才复用完成结果。
- 最终候选只在所有技术检查通过后发布。

### 失败处理

```text
保留现场
→ 确认失败阶段与最近检查点
→ 定位通用根因
→ 修正式代码
→ 补相关离线/合成测试
→ 从正式入口恢复
→ 核对没有重复 API/GPU/媒体工作
```

禁止手工修改字幕、缓存、manifest、报告或中间媒体来冒充正式恢复。

### 自动化优先的异常分级

目标是尽量稳定地产出可观看候选，而不是让轻微对齐和展示偏差把整条流卡死：

| 级别 | 处理 | 当前范围 |
|---|---|---|
| 硬停止 | 保留现场，不发布候选 | 源片/manifest 谱系冲突；工具真实失败；媒体或字幕不可解析；关键轨道缺失；覆盖源片或已有结果；外部调用没有当次授权/Key；翻译 ID、数量、时间码不一致；ASR 严重重复坍塌 |
| 自动修复 | 写明动作后继续 | 清理空文本、零/负时长 cue；验证并晋升已完成工作文件；不能证明完整时改名保留并重跑当前媒体步骤；负的可呈现起点按输出零轴夹到 0 并保留原始证据 |
| 警告后继续 | 写入事件、阶段报告和顶层结果 | cue 轻微重叠、行数/行长/单条时长、少量超出源时长、可播放的容器时长偏差、报告数值的小舍入差 |

容器时长约从 0.5–0.75 秒差异开始报警，2 秒以内继续，超过 2 秒停止；画面报告的 origin、接缝和段映射允许 50 ms 内舍入差。翻译阶段逐条 ID 与时间码仍精确匹配，因为它们决定字幕归属，不能用“观感差不多”代替。

## 5. 验收层级

### 如何理解完成状态

`technical_checks_passed` 表示对应机器检查通过。成片此时仍为 `candidate_for_human_review`，需要观看判断翻译、画面、接缝和同步；人工结论单独记录在 `human-review.json`，不覆盖技术报告。

### 翻译机器检查

- A、C、融合和中文 SRT 的结构可解析；
- 正式 A 作为时间轴骨架，融合不删除 A；新任务的原始识别 A 另存，局部校时只改变明确异常的开头时间，融合/复核/中文保持同一正式时间轴；
- ID、数量和毫秒时间码符合阶段契约；
- 清理报告明确记录被自动剔除的空文本和非正时长 cue；进入正式 A、C、融合和中文交接的文本必须非空，且每条 `end_ms > start_ms`；
- 报告记录两路固定编号范围、批次开始/完成事件和阶段墙钟；润色开始前全部初译必须完成；
- 不同 profile/provider/model/cache 没有混用；
- 原主视频和全部原音轨保留，只新增唯一默认中文字幕；
- 失败不发布残缺 SRT/MKV。

### 画面机器检查

- 工具、引擎和硬解码路线通过预检；
- 每段有匹配的输入/输出时长和统一流参数；
- 最终视频恰好一条主视频轨；
- 段总量和整片时长无明显缺失；
- 开头、真实接缝附近和结尾可解码。

GPU 占用高、模型日志出现 fps、引擎存在或进程退出 0 都不能单独证明画面成功。

### 最终封装机器检查

- Matroska 可读；
- 恰好一条主要视频轨且编码/尺寸与画面输入一致；
- 原音轨数量和 codec 保留；
- 恰好一条默认 `zho` SubRip；
- 总时长无明显异常；
- 开头、全部真实接缝和结尾可解码。

### 重新封装机器检查

- 容器与所选 MKV/MP4 一致；
- 恰好一条视频，codec 和尺寸与所选源视频一致；
- 原音轨数量、顺序和 codec 一致，命令证据为 `-c:v copy -c:a copy`；
- 恰好一条默认 `zho` 软字幕：MKV 为 SubRip，MP4 为 mov_text；
- 总时长在允许的容器偏差内，源文件大小/修改时间未变；
- 开头、中部和结尾的视频与全部音轨可解码。

### 人工验收

用户观看代表性开头、中段、结尾和全部接缝，判断：

- 翻译是否自然、符合既定风格；
- 是否有明显幻觉、漏译或字幕时机问题；
- 去马是否有效，是否出现涂抹、伪影或接缝突变；
- 音画与字幕同步是否可接受；
- MP4/MKV 重新封装结果能否正常播放和拖动，中文软字幕能否开关；
- 播放器中的整体体验。

技术报告生成时只能写 `candidate_for_human_review / pending`。用户之后的通过/不通过应写入独立验收记录，不覆盖原技术结果。

## 6. 耗时、资源与证据记录

阶段耗时来自正式 events/result，区分本次执行、检查点复用与暂停等待。并行阶段的耗时不能相加当作总墙钟；整体用时应按正式起止事件计算，取消后已发生的工作与暂停等待分别说明。

GPU 利用率和显存只作诊断。整卡采样包含桌面及其他进程，有限采样不能证明全程峰值或某个模型的独占占用。模型日志的 fps 也不等于整条流程的速度。

反馈问题时，提供软件版本、处理模式、错误码和复现步骤。优先使用中性素材；分享日志或报告前，删除媒体文件名、本地路径、字幕文本和凭据。

## 7. 入口与路线边界

- 视频字幕使用 A+C 主线；独立 SRT 翻译不运行 ASR。
- 画面处理使用 `jasna_picture`；`video_pipeline` 中的实验模块不属于桌面处理路径。
- Jasna 直接输出 HEVC，分段完成后 stream-copy 拼接。
- 分段用于检查点、恢复和接缝验证，不天然减少模型算量。
- 更换工具或模型后，先检查短样和路线验收，再扩大处理范围。
- 同一媒体阶段只保留一个运行者；监控使用低频、只读检查，不重复发起任务。

## 8. 并行与交付机制

### A+C 后双分支并行

- 分叉点位于融合 SRT 的正式结构检查之后；
- 普通翻译失败后画面继续完成，画面失败后翻译继续完成；
- 两边均成功才进入最终薄封装；
- 已完成翻译的恢复任务可直接启动或复用画面；
- 旧串行 manifest 可兼容恢复；
- 桌面 JSONL 事件保持有序，Jasna/FFmpeg 原始输出只进入持久日志。

### 翻译 API 固定双路

- 全片语境只生成一次；
- 初译固定拆成连续前后两路，每路内部保持时间顺序；
- 全部初译合并并验证后才启动润色；
- 成功批次原子落盘，并发数不进入缓存身份；
- 一路失败会阻止继续领取新批次，已发出的请求可完成并保留有效缓存；
- 本地缓存写失败立即停止，不能当成模型格式错误拆批重调。

`concurrency=3` 不在当前支持范围。术语一致性、措辞和具体译文质量由人工审阅，不以结构检查通过代替。

### 任务中心与可选 MP4

任务中心从正式任务目录生成列表，不另建状态库。恢复不绕过供应商、源片谱系、授权或检查点规则。MP4 是最终 MKV 后的独立交付层，成功、复用与失败分别记录；MP4 失败不会把已通过的 MKV 改为失败。

## 9. 阶段计时、模型选择与开头校时

### 阶段计时与模型选择

桌面按正式阶段事件显示总耗时和已完成阶段耗时，标明检查点复用。识别阶段没有可靠实时进度时显示等待，不制造倒计时。`timing.summary.json` 为事件派生的汇总，不能代替原始事件或技术报告。

视频准备页提供 Turbo（默认）和 large-v3。选择保存为 `asr_a_model` 并参与 ASR 检查点身份；缺字段的旧请求仍按 large-v3 恢复。不同模型的速度和识别质量需要同源、同参数的独立对照，字幕条数或字符数差异本身不是质量结论。

### 开头局部校时

新视频字幕任务在 A/C 退出后、融合交接和外部翻译前应用 `leading_silence_v1`。它只检查明确处于开头非语音区域的连续字幕前缀；C 文本匹配、顺序、偏移和语音重叠证据均充分时才修正时间。证据不足保持原样，不猜测、不阻断生产。

该规则不启用全片 VAD 重新识别、不整体平移字幕、不修改文字。常规融合先按原时间选文本，再转移符合条件的开头修正时间。`A.recognized.ja.srt` 保留清理后的原识别，`A.ja.srt` 是后续 fusion、复核和中文使用的正式骨架；`opening-timing.report.json` 与 ASR 报告记录 applied/no_candidate/not_applicable/skipped 及实际修改。

校时使用与 whisper.cpp 配套的 Silero v6.2.0 CPU VAD，仅复用 A 首段 WAV 的前 60 秒。缺组件或检测失败时显示 `OPENING_TIMING_SKIPPED`，保留原时间继续；取消必须正常传递。新 ASR marker 保存 `opening_timing_policy=leading_silence_v1`；没有该字段的旧部分任务不改变时间，完成 ASR 直接复用，避免污染已有翻译缓存。SRT 输入不经过这一步。

独立入口 `python -m translation_flow.opening_timing` 可生成另存的试听候选，不修改既有正式任务。候选需要在相应素材上人工听看，不能把规则测试通过当作整片同步验收。

## 10. 产物保留与清理

### 实时美颜播放器

重新打开 `启动翻译流.cmd`，点击 **首页 → 美颜 → 打开视频** 选任意本地视频；或打开 **成品 → 选中版本 → 实时美颜**。播放器独立打开，不入生产队列、不要求 Key。点击播放窗右上角“美颜”打开独立小工具栏，可分别移动/缩放视频窗与工具窗。关闭工具栏不停止播放、不丢强度；重新点“美颜”继续调节，关闭视频窗会收起工具栏。若浏览器拦截弹窗，会明确提示允许本地播放页弹窗后重试，不静默改成侧栏。

磨皮、美白、瘦脸、大眼滑块立即影响观看；“自然”“全部归零”和“启用美颜”支持快速调整，暂停时也能调节。正常只显示“实时生效”，真实美颜错误仍提示并播放原画。底部 − / 百分比 / + 只缩放视频画面（50%–200%，百分比按钮还原 100%），字幕与控件不跟着放大；视频还可通过独立窗口边框缩放或全屏观看，全屏保留播放控制。没有美颜成片导出，不额外生成整片中间文件。工具窗不承诺系统置顶；原有音轨/字幕支持范围不变。

常见 H.264 MP4 直接播放，MKV 通过本地 FFmpeg 边读边直封。MKV 跳转会落在邻近关键帧，进度和字幕使用实际落点而不是虚构精确跳转。浏览器无法解码 HEVC 时，播放器会检测并切换为 CPU H.264 观看流（最高 1080p）；准备期间短暂提示，成功后不保留技术介绍，不改源视频/音频、不写整片副本。无法解码的其他组合会显示原因，不自动改正式工作流。

只显示首选内嵌中文文本字幕，播放第一条音轨。HDR、复杂运动人脸、遮挡、多人镜头的稳定性及实际观看效果需要分别试用；不提供专业美颜 SDK 的完整效果集合。关闭播放窗会断开视频流，播放器自己的本地服务在无访问约 3 分钟后退出，不停止既有翻译或画面任务。

新机器首次显式运行项目 Python `-m beauty_player.setup` 下载固定版本的公开运行库/模型；播放时不下载、不外发媒体。开发排错入口为 `-m beauty_player.server --media <本地视频> --no-browser`。`tests.beauty_browser_smoke --media <中性短片> --output <新的证据目录> --tool-root <现有工具根>` 是需显式运行的隔离 Edge 集成测试；多份源码可复用既有工具，不重新下载。窗口验证使用本机 Node 22+ 自带 WebSocket，可用 `--node <node.exe>` 指定路径，无额外 npm 依赖；这个驱动只连接本次全新 Edge 配置的随机调试端口。正式播放器不开放调试端口，也不提供测试上传接口。

### 成品中心与人工修订

打开桌面入口，点击顶部“成品”。默认读取当前保存目录，可切换目录；列表只收录有正式技术完成报告且实际产物存在的任务。完整工作流不重复列其翻译子任务，版本下拉框可打开已保留的 MP4、MKV 或原画版本。目录中的历史散落视频不自动猜测成品身份。

1. **清理：** 直接点击视频成品卡播放按钮下方的“清理中间文件” → 检查精确文件清单和逻辑大小 → 已看过成片后确认永久删除。不必先选中卡片；按钮始终针对所点击的成品，取消不会删除，执行期间暂停成品卡操作。只处理已完成任务内输入/输出分段、纯视频轨及 job-local WAV；不递归删除目录、不追踪链接、不清模型或外部 ASR 临时目录。保留所有完整成片版本、源片、字幕、缓存和报告。文件清单或成片发生变化会要求重查；文件占用或删除失败立即停止，独立 `retention-cleanup.json` 记录已删部分。已删检查点不可恢复，任务中心不再提供该任务原地继续，成片仍可播放。硬链接及并发磁盘活动可能使实际释放量不同于逻辑字节数。
2. **校对：** 选成品 → 字幕校对。时间和日文只读、中文可编辑；日文缺失或时间不匹配时明确显示无匹配参考。点击行定位画面，内置播放器不支持该编码时不影响文字编辑。修改另存到 `.subtitle-edits/<修订目录>`，不改变自动原稿或正式缓存；全片源字幕按最终汇合报告的实际窗口换算到成片时间。
3. **修订成片：** “导出修订成片（MKV）”先保存修订 SRT，再复制原成片视频和全部音轨、替换中文字幕，另存新的 MKV 与技术报告；无需登录、Key 或重新调用模型。第一版只导出 MKV；多字幕轨/非中文软字幕轨不自动替换，避免静默丢轨。导出技术通过仍需人工观看，不改原任务的验收状态。
4. **通知：** 队列在任务终态请求 Windows 通知，关闭主窗口不影响后台通知。内容不含影片名或路径；系统勿扰可能隐藏通知。可通过进程环境变量 `TRANSLATION_FLOW_NOTIFICATIONS=0` 关闭通知；通知失败不阻断处理。正在运行的旧队列进程不会热加载新代码，不为升级通知而打断它。

默认保留：

- 正式 SRT、原画字幕版、纯视频轨和最终成片；
- 翻译与画面阶段报告；
- 完成分段与恢复现场；
- 小型日志和验收记录。

人工验收后可以生成“可清理对象清单”，区分媒体检查点与长期证据。删除前检查清单中的完整路径、类型和数量，确认不含源片、成片、工具或符号链接，再确认执行。

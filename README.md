# 翻译流

Windows 桌面上的日语视频字幕与本地画面处理工具。选文件、选功能，完成后从“成品”页播放、校对或导出。

目前是源码发布准备版，功能基线为 `v1-desktop-accepted-20261004`，项目采用 [Apache-2.0](LICENSE) 许可证。对外源码包采用当前源码快照，不含开发 Git 历史；工具、模型、媒体和本机配置需另行准备。

## 能做什么

| 输入与选择 | 得到什么 |
|---|---|
| 日语视频 → 翻译 | 中文字幕 SRT，保留原画和原声音的 MKV，可追加 MP4 |
| 日文 SRT → 翻译 | 中文 SRT 和检查报告，无需显卡识别 |
| 视频 → 去马 | Jasna 本地处理后的成片，保留原声音和已有字幕 |
| 视频 → 翻译 + 去马 | 识别完成后两路工作，最后合成带中文字幕的成片 |
| 已有字幕的视频 → 换格式 | 在支持的容器间复制轨道，避免重跑翻译 |
| 本地视频 → 美颜播放 | 播放时调磨皮、美白、瘦脸、大眼，不生成整片副本 |

支持顺序批量、停止后继续、关窗后台运行、字幕校对、另存修订 MKV，以及列出精确文件后清理中间产物。处理后的图像是模型推断，不能还原或证明原始细节。

## 需要准备什么

- **Windows 11 x64、Windows PowerShell 5.1、Python 3.12 x64**：这是当前验证环境。WPF 桌面不支持 macOS/Linux；其他 Windows/Python 版本未完整验证。
- **翻译服务任选一种**：Codex 的本机 ChatGPT 登录，或 DeepSeek、Qwen、Kimi、OpenAI、Anthropic、Gemini API。账户、地域、模型和额度由你准备；不同服务的真实可用性分别验证。
- **视频字幕**：FFmpeg、Whisper A 模型和 faster-whisper C 模型；现有 GPU 路线使用 NVIDIA CUDA。A 默认 Turbo，也可选 large-v3；C 始终是 large-v3。
- **去马**：额外需要 Jasna 0.10.0、相应模型/本机 TensorRT 引擎，以及本机完成的短样和 30 分钟验收。不能只下载源码就直接开始整片。
- **美颜播放**：Edge、FFmpeg 和显式安装的本地美颜组件。

视频、音频和人脸处理在本机；翻译会把字幕文本发给你本次同意的服务，并消耗相应账户额度。不要把 Key 写进源码或配置样例。

## 第一次打开

1. 把源码放到较短、可写的目录，例如 `C:\TranslationFlow`。工具和画面工作目录优先使用纯英文路径，预留模型、音频、视频分段和成片空间。
2. 安装 [Python 3.12 x64](https://www.python.org/downloads/windows/)，在源码目录打开 PowerShell：

   ```powershell
   py -3.12 -m venv .venv
   Copy-Item .\config\local.example.psd1 .\config\local.psd1
   ```

   主程序使用 Python 标准库，无需给它安装整套模型依赖。`.venv` 会被桌面入口自动找到。若使用其他 Python，按样例在 `config/local.psd1` 填写 `PythonPath` 的绝对路径。
3. 先校验界面和最小字幕环境，不调用翻译或显卡：

   ```powershell
   powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\desktop\Start-TranslationFlow.ps1 -ValidateOnly
   .\.venv\Scripts\python.exe -m translation_flow.diagnostics --mode srt --provider deepseek
   ```

   `paths_available` 只代表路径存在，未验证登录、网络、模型或 GPU。
4. 准备要用的服务。Codex 按[官方 CLI 安装说明](https://learn.chatgpt.com/docs/codex/cli)选择 Windows 安装方式，再 `codex login`，选择 ChatGPT 登录；`codex login status` 可查状态，见[官方认证说明](https://learn.chatgpt.com/docs/auth)。其他 API 在界面选择服务、模型/地域后填写本次 Key。
5. 双击 **`启动翻译流.cmd`**。可先用自己的短 SRT 验证翻译，再安装视频组件。开始处理前的服务检查会使用少量服务额度。

## 启用视频、去马和美颜

按 [WORKFLOW 的新机器安装步骤](WORKFLOW.md#新机器安装)取得固定版本组件、摆放目录、安装 C 依赖并执行只读诊断。下载与首次引擎编译可能较大，需要你显式执行；打开程序不会自动安装这些组件。

字幕视频环境检查：

```powershell
.\.venv\Scripts\python.exe -m translation_flow.diagnostics --mode video --provider codex
# 如果在界面选择了 large-v3：
.\.venv\Scripts\python.exe -m translation_flow.diagnostics --mode video --provider codex --asr-a-model large-v3
```

缺少 Silero VAD 时会警告并保留原识别时间。它只用于有证据支持的开头局部校时，不会整体平移或全片重识别。去马安装和建立本机验收报告的完整命令也在安装步骤中。

## 遇到问题

| 现象 | 处理 |
|---|---|
| 找不到 Python / 模型 / FFmpeg | 先跑对应模式的 `diagnostics`，按缺失路径补组件；桌面路径可填 `config/local.psd1` |
| 翻译登录、网络或额度失败 | 修好对应服务后，在“任务”页按原请求继续；不要改字幕缓存或换模型冒充同一任务 |
| 关闭窗口后还在运行 | 后台队列会继续；需要停时先点“停止”，等待阶段安全退出 |
| 重开后任务暂停 | 重新确认本次授权；必要时补 Key，然后继续 |
| 去马提示缺验收报告或引擎 | 完成本机安装和样片验收；不要复制别人的报告或引擎假装通过 |
| 成片占用很多空间 | 先观看确认，再在成品卡点“清理中间文件”；清理后不能再从已删分段恢复 |
| 字幕、画面或同步看着不对 | 技术检查不能代替观看。保留日志和原始检查点，先定位失败阶段 |

机器检查涵盖结构、轨道、时长、解码与恢复；翻译自然度、画面质量和同步仍需要你看。现有测试与历史样本的范围见 [WORKFLOW](WORKFLOW.md)，不代表所有 GPU、服务或输入都已验收。

## 开发与许可

工程入口是 [AGENTS](AGENTS.md)、[PROJECT_BRIEF](PROJECT_BRIEF.md)、[WORKFLOW](WORKFLOW.md)。离线回归：`python -m unittest discover -s tests -q`，画面回归：`python -m unittest discover -s tests_jasna_picture -q`；部分集成测试按真实工具环境跳过。

除另有标注外，本项目代码、文档及项目自行生成的首页插图采用 [Apache License 2.0](LICENSE)。第三方来源、版本、已知许可证及尚未核实项见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)，美颜播放器保留其 [第三方声明](beauty_player/THIRD_PARTY.txt)。外部二进制和模型仍适用各自的许可条件。

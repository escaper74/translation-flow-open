<h1 align="center">翻译流</h1>

<p align="center">给日语视频加上中文字幕，也能去马、边看边调美颜。</p>

<p align="center">
  <a href="#开始使用">开始使用</a> ·
  <a href="WORKFLOW.md#新机器安装">安装指南</a> ·
  <a href="#使用说明">使用说明</a>
</p>

![翻译流桌面首页：去马、翻译、美颜，以及任务和成品入口](docs/images/home.png)

## 能做什么

- **翻译** — 导入视频或日文 SRT，生成中文字幕；视频可保存为 MKV 或 MP4，保留原声音。
- **去马** — 单独处理画面，也能和翻译一起做。画面由模型推断，效果需要试看。
- **美颜** — 边看边调磨皮、美白、瘦脸和大眼，只改变观看效果。

支持批量添加和中断后继续。做完后，在「成品」里播放、校对字幕、另存修订版或清理中间文件。

## 开始使用

目前提供源码，适用于 **Windows 11 x64 / Python 3.12 x64**。视频识别和去马需要 NVIDIA 显卡，工具与模型另行安装。

1. 下载并解压源码，按下方「首次安装」准备 Python；视频组件见[安装指南](WORKFLOW.md#新机器安装)。
2. 双击 **`启动翻译流.cmd`**。需要翻译时，选择服务并登录或填写 Key。
3. 选择视频或 SRT，确认功能和保存位置，点击「开始处理」。

音视频留在本机；翻译会把字幕文本发送给所选服务，使用你自己的账号和额度。

<details>
<summary>首次安装：Python 与翻译服务</summary>

安装 [Python 3.12 x64](https://www.python.org/downloads/windows/)，把源码解压到可写目录，例如 `C:\TranslationFlow`。在这个目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
Copy-Item .\config\local.example.psd1 .\config\local.psd1
```

桌面入口会自动找到 `.venv`。使用其他位置的 Python 时，在 `config/local.psd1` 填写 `PythonPath`。

**选择翻译服务**

可选 Codex、DeepSeek、Qwen、Kimi、OpenAI、Anthropic 或 Gemini。

使用 Codex 时，先按[官方说明](https://learn.chatgpt.com/docs/codex/cli)安装 CLI，再运行 `codex login`；API 方式在界面填写所选服务的 Key。已有日文字幕的话，可以先用一小段 SRT 试一下。开始前的服务连接检查会使用少量账户额度。

**安装视频组件**

视频需要 FFmpeg 和 Whisper 识别组件；去马还需 Jasna、本机生成的 TensorRT 引擎，以及短样和 30 分钟样片检查。版本、下载来源和步骤见[安装指南](WORKFLOW.md#新机器安装)，美颜组件也可按其中的说明单独安装。

</details>

## 使用说明

<details>
<summary>检查本地环境</summary>

在源码目录运行，`provider` 换成你使用的服务：

```powershell
# 检查桌面能否加载
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\src\desktop\Start-TranslationFlow.ps1 -ValidateOnly

# 检查字幕翻译所需的本地路径
.\scripts\python.cmd -m translation_flow.diagnostics --mode srt --provider deepseek

# 检查视频组件
.\scripts\python.cmd -m translation_flow.diagnostics --mode video --provider codex
```

选择 large-v3 时，在视频检查命令后加上 `--asr-a-model large-v3`。`missing_paths` 会列出缺少的文件。这些命令不运行模型或翻译；路径检查通过后，仍需在开始任务时确认服务和运行环境可用。

</details>

<details>
<summary>常见问题</summary>

**提示找不到 Python、FFmpeg 或模型**

展开上面的「检查本地环境」，按缺失路径补齐组件。如果工具放在其他盘，在 `config/local.psd1` 配置对应路径；字段说明见[配置样例](config/local.example.psd1)。

**翻译中断了，或者重开后任务暂停**

检查所选服务的登录、网络和额度，再到「任务」页点继续。重开程序后，可能需要重新填写 Key 并确认继续翻译；已完成的步骤会尽量复用。需要更换模型时，新建任务即可。

**为什么关闭窗口后还在处理？**

窗口关闭后，后台队列会继续工作。想暂停处理时，先在任务里点「停止」，等状态更新后再关窗口。

**去马提示缺少引擎或验收报告**

Jasna 首次使用需要为本机显卡生成引擎，并完成样片检查。按[安装说明](WORKFLOW.md#在新机器建立画面验收报告)走完后，将生成的报告路径填入 `AcceptanceGateReport`。

**处理完以后，怎么腾出硬盘空间？**

先确认成片能正常观看，再点成品卡上的「清理中间文件」。程序会列出待删除的文件，完整成片、字幕和日志会保留。清掉的中间分段无法再用于恢复任务。

**字幕有错，或者声音和字幕对不上**

文字可以在「成品 → 字幕校对」中修改，保存后另存修订成片。遇到时间偏移或处理错误时，保留任务日志，反馈输入类型、处理选项和出现问题的位置，便于排查。

</details>

<details>
<summary>开发说明</summary>

[项目结构](PROJECT_BRIEF.md) · [运行手册](WORKFLOW.md) · [开发约定](AGENTS.md)

在源码目录运行测试：

```powershell
.\scripts\python.cmd -m unittest discover -s tests -t . -q
```

部分集成测试需要相应的本地工具。依赖要求与验证方法见运行手册。

</details>

---

[Apache-2.0](LICENSE) · 第三方工具、模型及引用代码的许可见[第三方声明](THIRD_PARTY_NOTICES.txt)。

"""Explicit desktop login and pre-start checks; never reads media or auth files."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from .codex_runtime import resolve_codex_executable
from .diagnostics import inspect_environment
from .translation.api_providers import create_api_provider
from .translation.codex_provider import CodexProvider, cli_environment, codex_status, NO_WINDOW


def login_codex():
    executable = resolve_codex_executable()
    if not executable:
        return codex_status()
    try:
        # Official browser OAuth. Never copy its output (URLs/tokens) into UI logs.
        result = subprocess.run([executable, "login"], env=cli_environment(),
                                capture_output=True, timeout=180, creationflags=NO_WINDOW)
        if result.returncode:
            return {"status": "unavailable", "message": "登录未完成。请重新点击登录，在官方浏览器页面完成授权。"}
        return codex_status(executable=executable)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "message": "等待登录超时。若已在浏览器完成，请点击重新检测。"}
    except OSError:
        return {"status": "unavailable", "message": "无法启动登录程序。请先重新检测本机 Codex。"}


def check_readiness(*, mode, provider, tools=None, model=None, endpoint=None,
                    max_output_tokens=None, api_key="", probe=False):
    report = inspect_environment(mode=mode, provider=provider, tools=tools)
    report["service_tested"] = False
    missing = [item["name"] for item in report["checks"] if not item["present"]]
    if missing:
        report.update(status="blocked", message="缺少本地工具：" + "、".join(missing) + "。请在更多设置中检查工具目录，再重试。")
        return report
    if mode in ("repackage", "picture"):
        report.update(status="ready", message="本地封装工具已找到；不需要登录或翻译服务。")
        return report
    if provider == "codex":
        status = codex_status()
        if status["status"] != "available":
            report.update(status="blocked", message=status["message"])
            return report
    elif not api_key.strip():
        report.update(status="blocked", message="请填写所选翻译服务的 API Key。")
        return report
    if not probe:
        report.update(status="ready", message="本地条件检查通过；尚未测试服务响应。")
        return report
    try:
        if provider == "codex":
            with tempfile.TemporaryDirectory(prefix="translation-access-") as audit:
                client = CodexProvider(audit_dir=audit, timeout=60)
                response = client.chat([{"role": "system", "content": "Connection test only. Reply with OK."}],
                                       model="cli-default", temperature=0)
                if not response.strip():
                    raise ValueError("服务返回空响应")
        else:
            client = create_api_provider(provider, api_key, model=model, endpoint=endpoint,
                                         max_output_tokens=max_output_tokens, timeout=45)
            client.check_connection()
    except Exception as exc:
        detail = str(exc)
        if api_key:
            detail = detail.replace(api_key, "[已隐藏]")
        if '超时' in detail or 'timeout' in detail.lower():
            detail = '连接检查超时。请检查网络或稍后重试；这不代表未登录。'
        report.update(status="blocked", message="服务检查未通过，尚未开始处理文件：" + detail[:400])
        return report
    report.update(status="ready", service_tested=True,
                  scope="已检查本地路径、所需登录及一次固定测试词请求；未验证真实媒体、字幕质量或后续服务稳定性。",
                  message="本地工具和本次服务请求检查通过，准备开始。检查不保证后续网络或额度始终可用。")
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="桌面登录和开始前检查")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--login", action="store_true")
    actions.add_argument("--check", action="store_true")
    parser.add_argument("--mode", choices=("video", "srt", "full", "picture", "repackage"), default="srt")
    parser.add_argument("--provider", default="codex")
    parser.add_argument("--tool-root")
    parser.add_argument("--model")
    parser.add_argument("--endpoint")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--probe", action="store_true", help="用户确认后才发送固定测试词，消耗少量服务额度")
    args = parser.parse_args(argv)
    api_key = os.environ.pop("SUBTITLE_API_KEY", "")
    result = login_codex() if args.login else check_readiness(
        mode=args.mode, provider=args.provider, tools=args.tool_root, model=args.model,
        endpoint=args.endpoint, max_output_tokens=args.max_output_tokens, api_key=api_key, probe=args.probe)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

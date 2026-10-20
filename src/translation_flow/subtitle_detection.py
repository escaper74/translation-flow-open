"""Local, bounded import check. Presence is evidence; a miss is not absence.

This never runs ASR, a translator, or picture restoration. OCR uses the Chinese
recognizer already installed in Windows; missing tools are reported as unknown.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from media_planning.output import TEXT_SUBTITLE_CODECS, _is_chinese_language
from translation_flow.runtime import PROJECT_ROOT, tool_root


SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt'}
CHINESE_PHRASES = re.compile(r'什么|什麼|怎么|怎麼|为什么|為什麼|没有|沒有|不要|不是|我们|我們|你们|你們|这个|這個|那个|那個|可以|知道|谢谢|謝謝|对不起|對不起|没事|沒事|不行|喜欢|喜歡|真的|好了')
CHINESE_CHARACTERS = set('这们吗么没说话让给别为还样里会过开吗這們嗎麼沒說話讓給別為還樣裡會過開')


def chinese_dialogue(text: str) -> bool:
    text = re.sub(r'<[^>]*>|\{[^}]*\}', '', text)
    text = re.sub(r'\s+', '', text)
    # Han characters alone also occur in Japanese names, signs, and titles.
    if re.search(r'[\u3040-\u30ff]', text) or len(re.findall(r'[\u4e00-\u9fff]', text)) < 4:
        return False
    if re.search(r'https?://|www\.|字幕组|字幕組|广告|廣告|翻译制作|翻譯製作', text, re.I):
        return False
    return bool(CHINESE_PHRASES.search(text)) or len(set(text) & CHINESE_CHARACTERS) >= 2


def subtitle_lines(text: str, suffix: str) -> list[str]:
    if suffix.lower() in {'.ass', '.ssa'}:
        return [line.split(',', 9)[-1].replace(r'\N', ' ') for line in text.splitlines()
                if line.lstrip().startswith('Dialogue:') and line.count(',') >= 9]
    result, in_cue = [], False
    for line in text.splitlines():
        if '-->' in line:
            in_cue = True
        elif not line.strip():
            in_cue = False
        elif in_cue:
            result.append(line.strip())
    return result


def read_subtitle(path: Path) -> str:
    raw = path.read_bytes()
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030', 'big5')
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    return ''


def sidecars(source: Path) -> list[Path]:
    # movie.srt and movie.zh-CN.srt, never movie2.srt or unrelated neighbours.
    stem = source.stem.casefold()
    return sorted(p for p in source.parent.iterdir() if p.is_file()
                  and p.suffix.casefold() in SUBTITLE_EXTENSIONS
                  and (p.stem.casefold() == stem or p.stem.casefold().startswith(stem + '.')))


def _run(args, *, timeout=20, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, check=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError('本地字幕检查工具未能完成')
    return result.stdout


def windows_ocr(images: list[Path]) -> list[dict]:
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    payload = json.dumps([str(p) for p in images], ensure_ascii=False).encode('utf-8')
    output = _run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                   PROJECT_ROOT / 'src/desktop/Read-SubtitleOcr.ps1'], input=payload, timeout=45)
    result = json.loads(output.decode('utf-8-sig'))
    if result['status'] != 'ok':
        raise RuntimeError(result['message'])
    return result['frames']


def inspect_subtitles(source: Path, *, tools: Path | None = None, run=_run, ocr=windows_ocr) -> dict:
    source = source.resolve()
    stat = source.stat()
    report = dict(source=str(source), source_size_bytes=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                  status='not_detected', kind='', message='抽样未检测到中文字幕；可按原选择处理。',
                  evidence=[], warnings=[])

    def found(kind, evidence):
        report.update(status='detected', kind=kind, evidence=evidence,
                      message={'sidecar': '已有外挂中文字幕', 'embedded': '已有内嵌中文字幕',
                               'burned': '抽样检测到烧录中文字幕'}[kind])
        return report

    if source.suffix.casefold() in SUBTITLE_EXTENSIONS:
        # Explicit SRT imports are requests to translate/refine that document.
        report.update(status='not_applicable', message='独立字幕文件按所选功能处理。')
        return report
    for candidate in sidecars(source):
        lines = subtitle_lines(read_subtitle(candidate), candidate.suffix)
        hits = list(dict.fromkeys(line for line in lines if chinese_dialogue(line)))
        if hits:
            return found('sidecar', [dict(path=str(candidate), text=hits[0][:160])])

    binaries = tool_root(tools) / 'ffmpeg/ffmpeg-8.1.1-essentials_build/bin'
    ffmpeg, ffprobe = binaries / 'ffmpeg.exe', binaries / 'ffprobe.exe'
    try:
        payload = json.loads(run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', source]))
        streams = payload.get('streams', [])
        subtitles = [s for s in streams if s.get('codec_type') == 'subtitle']
        for stream in subtitles:
            if _is_chinese_language(str(stream.get('tags', {}).get('language', ''))):
                return found('embedded', [dict(stream_index=stream['index'], language=stream['tags']['language'])])
        duration = float(payload.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError('无法读取视频时长，未完成烧录字幕检查。')
        # Unlabelled text tracks: seek to a few windows; never extract the full film.
        for stream in subtitles:
            language = str(stream.get('tags', {}).get('language', '')).lower()
            if language not in ('', 'und', 'unknown') or stream.get('codec_name') not in TEXT_SUBTITLE_CODECS:
                continue
            for fraction in (.1, .5, .8):
                text = run([ffmpeg, '-v', 'error', '-nostdin', '-ss', str(duration*fraction), '-i', source,
                            '-t', '30', '-map', f"0:{stream['index']}", '-c:s', 'srt', '-f', 'srt', 'pipe:1']).decode('utf-8-sig')
                hits = [line for line in subtitle_lines(text, '.srt') if chinese_dialogue(line)]
                if hits:
                    return found('embedded', [dict(stream_index=stream['index'], text=hits[0][:160])])
        if not any(s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic') for s in streams):
            raise RuntimeError('没有可检查的视频画面。')
        # Three small batches, up to 12 lower-screen samples. One clear Chinese
        # dialogue line is enough for the user's presence-only import policy.
        with tempfile.TemporaryDirectory(prefix='translation-subtitle-check-') as tmp:
            samples = [(i, duration*fraction) for i, fraction in enumerate((.04,.12,.20,.28,.36,.44,.52,.60,.68,.76,.84,.92))]
            for offset in range(0, len(samples), 4):
                paths = []
                for index, timestamp in samples[offset:offset+4]:
                    path = Path(tmp) / f'{index}.png'
                    run([ffmpeg, '-v', 'error', '-nostdin', '-n', '-ss', str(timestamp), '-i', source,
                         '-map', '0:V:0', '-frames:v', '1', '-vf',
                         "scale='min(1600,iw)':-2,crop=iw:ih*0.45:0:ih*0.55", path])
                    paths.append(path)
                frames = ocr(paths)
                for (index, timestamp), frame in zip(samples[offset:offset+4], frames):
                    for line in frame.get('lines', []):
                        if chinese_dialogue(line['text']):
                            return found('burned', [dict(seconds=round(timestamp, 2), text=line['text'][:160])])
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report.update(status='unknown', message='字幕检查未完成，保留原处理选择。')
        report['warnings'].append(str(exc))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--tool-root', type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect_subtitles(args.source, tools=args.tool_root)
    except (OSError, ValueError) as exc:
        result = dict(source=str(args.source), status='unknown', kind='', evidence=[],
                      message='字幕检查未完成，保留原处理选择。', warnings=[str(exc)])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

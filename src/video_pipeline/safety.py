from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


class UnsafePathError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def ensure_within(path: Path, root: Path) -> Path:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError(f"路径越出任务目录，拒绝操作: {resolved_path}") from exc
    if resolved_path == resolved_root:
        raise UnsafePathError("拒绝把整个任务目录作为清理目标")
    return resolved_path


def safe_remove_tree(path: Path, job_root: Path) -> None:
    target = ensure_within(path, job_root)
    if not target.exists():
        return
    if not target.is_dir():
        raise UnsafePathError(f"清理目标不是目录: {target}")
    for current, dirs, files in os.walk(target, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or _is_reparse_point(current_path):
            raise UnsafePathError(f"清理目录包含链接或联接点: {current_path}")
        for name in [*dirs, *files]:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                raise UnsafePathError(f"清理目录包含链接或联接点: {child}")
    shutil.rmtree(target)


def safe_unlink(path: Path, job_root: Path) -> None:
    target = ensure_within(path, job_root)
    if not target.exists():
        return
    if not target.is_file() or target.is_symlink() or _is_reparse_point(target):
        raise UnsafePathError(f"清理目标不是普通文件: {target}")
    target.unlink()

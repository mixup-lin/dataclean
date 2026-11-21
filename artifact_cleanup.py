# -*- coding: utf-8 -*-
"""运行产物清理（手动执行，默认 dry-run，绝不自动删除）。

本地工作台会把上传数据与运行产物落在 uploads/ 与 output*/ 目录，长期运行
会持续堆积并可能留存敏感业务数据。本模块提供按保留天数清理的受控工具：

- 默认 ``dry_run=True``，只列出候选，不改动任何文件；
- 只清理明确白名单目录（output、output_*、uploads、uploads/docs）下的内容；
- 拒绝符号链接，限定目录存在且在项目根内，避免误删；
- 需显式 ``--apply`` 才真正删除，且仅删除过期文件/空目录。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple

LOGGER = logging.getLogger("etl")

ROOT = Path(__file__).resolve().parent
# 仅这些目录的内容参与清理；rules/（规则库资产）永不纳入
CLEAN_DIRS = ("output", "output_smoke", "output_cli", "uploads")


def _candidate_dirs(root: Path) -> List[Path]:
    dirs: List[Path] = []
    for name in CLEAN_DIRS:
        base = root / name
        if base.is_dir() and not base.is_symlink():
            dirs.append(base)
    # output_* 通配：只取根下、非符号链接的目录
    for child in root.iterdir():
        if child.name.startswith("output_") and child.is_dir() and not child.is_symlink():
            dirs.append(child)
    return dirs


def scan(root: Path, retention_days: int) -> Dict[Path, List[Path]]:
    """返回每个候选目录下超过保留期的文件列表（不改动文件）。"""
    cutoff = time.time() - retention_days * 86400
    result: Dict[Path, List[Path]] = {}
    for base in _candidate_dirs(root):
        expired: List[Path] = []
        for item in base.rglob("*"):
            if item.is_symlink():
                continue
            if item.is_file():
                try:
                    if item.stat().st_mtime < cutoff:
                        expired.append(item)
                except OSError:
                    continue
        if expired:
            result[base] = expired
    return result


def cleanup(root: Path, retention_days: int, apply: bool) -> Tuple[int, int]:
    """执行清理。返回 (删除文件数, 删除目录数)。dry-run 时返回候选计数。"""
    plan = scan(root, retention_days)
    file_count = sum(len(v) for v in plan.values())
    for base, files in plan.items():
        for f in files:
            LOGGER.info("%s %s", "删除" if apply else "候选", f)
            if apply:
                try:
                    f.unlink()
                except OSError as exc:
                    LOGGER.warning("删除失败 %s: %s", f, exc)
    dir_count = 0
    if apply:
        # 自底向上删除清理后变空的目录，但保留候选根目录本身
        for base in plan:
            for d in sorted(
                (p for p in base.rglob("*") if p.is_dir() and not p.is_symlink()),
                key=lambda p: len(p.parts), reverse=True,
            ):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                        dir_count += 1
                except OSError:
                    continue
    return file_count, dir_count


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="etl_pipeline.py cleanup",
        description="清理过期运行产物（默认只预览；--apply 才真正删除）",
    )
    p.add_argument("--days", type=int, default=30,
                   help="保留天数，超过则视为过期（默认 30）")
    p.add_argument("--apply", action="store_true",
                   help="真正删除；不指定时仅列出候选（dry-run）")
    p.add_argument("--root", default=str(ROOT),
                   help="项目根目录（默认当前文件所在目录）")
    args = p.parse_args(argv)

    if args.days < 0:
        print("--days 不能为负数")
        return 1
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1

    files, dirs = cleanup(root, args.days, apply=args.apply)
    mode = "已删除" if args.apply else "预览（未改动，加 --apply 才会删除）"
    print(f"{mode}：候选/删除文件 {files} 个" + (f"，空目录 {dirs} 个" if args.apply else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

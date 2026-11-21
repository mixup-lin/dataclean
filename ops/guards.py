# -*- coding: utf-8 -*-
"""数据源路径安全校验（与算子无关的安全边界）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ops.errors import RuleValidationError


def validate_source_path(
    source: Dict[str, Any], allowed_root: Optional[Path] = None
) -> Path:
    """路径安全校验：必须存在、必须是普通文件、可选限制在工作区内。"""
    raw = str(source.get("path", ""))
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not path.exists():
        raise RuleValidationError([f"数据源不存在：{path}"])
    if not path.is_file():
        raise RuleValidationError([f"数据源不是文件：{path}"])
    if allowed_root is not None:
        root = Path(allowed_root).resolve()
        if root not in path.parents and path != root:
            raise RuleValidationError([f"数据源超出允许目录：{path}"])
    return path


__all__ = ["validate_source_path"]

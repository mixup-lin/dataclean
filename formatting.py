# -*- coding: utf-8 -*-
"""共享的展示层格式化工具（无业务依赖的中性模块）。

从 etl_pipeline 抽出，供核心管道、SVG 图表、报告层与 ops 算子共用，
切断 ``ops.builtin → etl_pipeline`` 的反向依赖。etl_pipeline 仍从此处
重新导出这些名字，历史 ``from etl_pipeline import _fmt_num`` 保持可用。
"""
from __future__ import annotations

import html as _html
from typing import Any

import numpy as np


def esc(s: Any) -> str:
    """HTML 转义（quote=True），所有进入 HTML 的不可信文本都应经过此处。"""
    return _html.escape(str(s), quote=True)


def fmt_num(x: Any) -> str:
    """将数值格式化为易读字符串，绝不使用科学计数法。

    规则：整数按千分位；小数保留有效位并去除尾随零；极小/极大值用定点
    表示以避免 ``e`` 记法；布尔值、缺失值、非数值原样转义输出。
    """
    if x is None:
        return "—"
    if isinstance(x, (bool, np.bool_)):
        return str(x)
    try:
        xf = float(x)
    except (TypeError, ValueError, OverflowError):
        return str(x)
    if np.isnan(xf):
        return "—"
    if xf == int(xf) and abs(xf) < 1e16:
        return f"{int(xf):,}"
    ax = abs(xf)
    if ax >= 1e16:
        return f"{xf:.0f}"
    if 0 < ax < 1e-3:
        return f"{xf:.10f}".rstrip("0").rstrip(".")
    return f"{xf:,.4f}".rstrip("0").rstrip(".").rstrip(",")


# 下划线别名：保持与历史私有函数同名，etl_pipeline 可直接重导出
_esc = esc
_fmt_num = fmt_num

# 为同文档内多个 SVG 生成唯一渐变 id，避免样式串扰
_SVG_UID = [0]


def next_svg_id(prefix: str = "g") -> str:
    _SVG_UID[0] += 1
    return f"{prefix}{_SVG_UID[0]}"


_next_svg_id = next_svg_id


__all__ = [
    "esc", "fmt_num", "next_svg_id",
    "_esc", "_fmt_num", "_next_svg_id",
]

# -*- coding: utf-8 -*-
"""算子层公共异常。

``RuleValidationError`` 是规则校验/执行失败的统一异常，携带**全部**错误项，
便于 Web 端一次性行内展示，而不是逐个报错。
"""
from __future__ import annotations

from typing import List


class RuleValidationError(ValueError):
    """规则校验失败，携带全部错误项供 Web 行内提示。"""

    def __init__(self, errors: List[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors

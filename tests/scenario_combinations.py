# -*- coding: utf-8 -*-
"""组合场景矩阵：正常 / 跨年 / 缺失 / 重复 / 边界面积 / 异常价格。

只描述「输入数据 + 自然语言需求 + 可验证断言」，不提供答案规则；
由前端接口级验证脚本按 /plan → /execute → /result 逐步执行。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

COLUMNS: List[str] = [
    "标的号", "市", "区县", "小区", "面积(㎡)", "物业类型",
    "成交价(元)", "评估价(元)", "开始时间", "结束时间",
]

# 每行：标的号 / 面积 / 成交价 / 结束时间 / 物业类型
_ROWS: List[tuple] = [
    ("A01", 20.0, 1000.0, "2025-01-10", "住宅"),
    ("A02", 2000.0, 4000.0, "2025-02-10", "住宅"),
    ("A03", 30.0, 1500.0, "2025-04-10", "商铺"),
    ("A04", 1500.0, 3000.0, "2025-05-10", "商铺"),
    ("B01", 40.0, 2000.0, "2026-01-10", "住宅"),
    ("B02", 1800.0, 3600.0, "2026-02-10", "住宅"),
    ("C01", 25.0, 1250.0, "2026-04-10", "商铺"),
    ("C02", 1900.0, 3800.0, "2026-05-10", "商铺"),
    # 边界面积：严格小于 50 / 大于 1000，都不应入选
    ("D01", 50.0, 5000.0, "2025-07-10", "住宅"),
    ("D02", 1000.0, 2000.0, "2025-08-10", "住宅"),
    # 重复标的号：同键不同价，去重应保留第一条
    ("A01", 20.0, 9000.0, "2025-01-10", "住宅"),
    # 缺失：面积空 / 价格空 / 结束时间空
    ("M01", None, 1000.0, "2026-01-10", "住宅"),
    ("M02", 40.0, None, "2026-01-10", "住宅"),
    ("M03", 20.0, 1000.0, None, "住宅"),
    # 异常价格：不剔除时应显著拉高 2026Q2 均价
    ("X01", 30.0, 1_000_000_000.0, "2026-04-10", "商铺"),
]


def make_frame() -> pd.DataFrame:
    rows = []
    for key, area, price, end, kind in _ROWS:
        rows.append({
            "标的号": key, "市": "福州", "区县": "鼓楼区", "小区": "测试小区",
            "面积(㎡)": area, "物业类型": kind,
            "成交价(元)": price, "评估价(元)": price * 1.1 if price else None,
            "开始时间": end, "结束时间": end,
        })
    return pd.DataFrame(rows, columns=COLUMNS)


BASE_INTENT = (
    "去重，然后提取出面积小于50或大于1000的数据，最后按结束时间，"
    "每一年按季度分析每个季度的平均成交单价的趋势，成交单价 = （成交价/面积）"
)

SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "normal-cross-year",
        "category": "正常 / 跨年",
        "intent": BASE_INTENT,
        "expect": {
            "ops_include": ["dedupe", "filter", "expr", "date_part", "dropna", "aggregate", "sort"],
            "time_column": "结束时间_年季度",
            "periods": {"2025Q01", "2025Q02", "2026Q01", "2026Q02"},
            "no_null_period": True,
            "weighted_not_equal_row_mean": True,
        },
    },
    {
        "id": "quarter-without-year-word",
        "category": "跨年 / 常识推导",
        "intent": "去重，然后提取面积小于50或大于1000的数据，最后按结束时间按季度分析平均成交单价的趋势，成交单价 = 成交价/面积",
        "expect": {
            "ops_include": ["date_part", "aggregate"],
            "time_column": "结束时间_年季度",
            "periods": {"2025Q01", "2025Q02", "2026Q01", "2026Q02"},
            "no_null_period": True,
        },
    },
    {
        "id": "monthly-bucket",
        "category": "时间粒度泛化",
        "intent": "按标的号去重，然后提取面积小于50或大于1000的数据，最后按结束时间按月分析平均成交单价趋势，成交单价 = 成交价/面积",
        "expect": {
            "ops_include": ["dedupe", "date_part", "aggregate"],
            "time_column": "结束时间_年月",
            "no_null_period": True,
        },
    },
    {
        "id": "explicit-dedupe-key",
        "category": "重复 / 去重键显式",
        "intent": "按标的号去重，然后提取面积小于50的数据，最后按结束时间每一年按季度统计每个季度的条数",
        "expect": {
            "ops_include": ["dedupe", "filter", "date_part", "count_rows"],
            "dedupe_subset": ["标的号"],
            "time_column": "结束时间_年季度",
        },
    },
    {
        "id": "missing-price-filled-zero",
        "category": "缺失 / 显式填充",
        "intent": "去重，然后提取面积小于50或大于1000的数据，成交价空值置为0，最后按结束时间每一年按季度分析平均成交单价趋势，成交单价 = 成交价/面积",
        "expect": {
            "ops_include": ["dedupe", "fill", "date_part", "aggregate", "expr"],
            "time_column": "结束时间_年季度",
            "no_null_period": True,
        },
    },
    {
        "id": "boundary-area",
        "category": "边界面积",
        "intent": "去重，然后提取面积小于50或大于1000的数据，最后按结束时间每一年按季度统计每个季度的条数",
        "expect": {
            "ops_include": ["filter", "count_rows"],
            "time_column": "结束时间_年季度",
            "excluded_area": [50.0, 1000.0],
        },
    },
    {
        "id": "outlier-not-removed",
        "category": "异常价格 / 未声明",
        "intent": BASE_INTENT,
        "expect": {
            "ops_include": ["aggregate", "expr"],
            "time_column": "结束时间_年季度",
            "outlier_step": False,
            "anomaly_inflates": True,
        },
    },
    {
        "id": "outlier-removed",
        "category": "异常价格 / 显式剔除",
        "intent": "去重，然后提取面积小于50或大于1000的数据，剔除异常价格，最后按结束时间每一年按季度分析平均成交单价趋势，成交单价 = 成交价/面积",
        "expect": {
            "ops_include": ["outlier", "aggregate", "expr"],
            "time_column": "结束时间_年季度",
            "outlier_step": True,
            "anomaly_inflates": False,
        },
    },
    {
        "id": "group-by-property-kind",
        "category": "分组维度 / 无时间",
        "intent": "去重，然后提取面积小于50或大于1000的数据，最后按物业类型分组分析平均成交单价，成交单价 = 成交价/面积",
        "expect": {
            "ops_include": ["dedupe", "filter", "aggregate", "expr"],
            "time_column": "",
            "group_column": "物业类型",
        },
    },
]


def wrong_rule(path: str, intent: str) -> Dict[str, Any]:
    """故意给一条只做去重和筛选的错误规则，验证编译器能否补全。"""
    return {
        "version": "2.0", "intent": intent,
        "source": {"path": path, "format": "csv"},
        "options": {"fill_missing": "none"},
        "task_type": "detail",
        "steps": [
            {"op": "dedupe", "subset": [], "keep": "first"},
            {"op": "filter", "logic": "or", "conditions": [
                {"column": "面积(㎡)", "operator": "lt", "value": 50},
                {"column": "面积(㎡)", "operator": "gt", "value": 1000},
            ]},
        ],
        "output": {"formats": ["csv"], "report": True},
    }


__all__ = ["SCENARIOS", "COLUMNS", "BASE_INTENT", "make_frame", "wrong_rule"]

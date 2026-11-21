# -*- coding: utf-8 -*-
"""提示词质量场景集：用于度量「生成即正确」的比例。

每个场景只给自然语言描述与真实列，不给答案规则；评测由
``ops.rule_eval.evaluate_rule`` 按任务族骨架判定。
"""
from __future__ import annotations

from typing import Any, Dict, List

SOURCE = {"path": "lots.csv", "format": "csv"}
COLUMNS: List[str] = [
    "平台", "标的号", "标题", "省", "市", "区县", "街道/镇", "小区", "地址",
    "面积(㎡)", "物业类型", "起拍价(元)", "评估价(元)", "成交价(元)",
    "出价次数", "围观数", "状态", "拍次", "开始时间", "结束时间",
    "所在楼层", "总层数", "楼层区间", "建成年份", "朝向",
]
ALIASES: Dict[str, str] = {}

SCENARIOS: List[Dict[str, Any]] = [
    # ---- 两类比率（rate）----
    {"id": "rate-01", "family": "rate", "intent": "筛选：区县：鼓楼区 判断：成交价为空-未成交，成交价不为空-成交 目标：统计成交率（成交/总数）"},
    {"id": "rate-02", "family": "rate", "intent": "筛选：市：福州 判断：成交价为空-未成交，成交价不为空-成交 目标：统计未成交率（未成交/总数）"},
    {"id": "rate-03", "family": "rate", "intent": "统计成交价为空与不为空的数据之比（未成交/成交）"},
    {"id": "rate-04", "family": "rate", "intent": "按区县统计成交率，成交价为空算未成交，不为空算成交"},
    {"id": "rate-05", "family": "rate", "intent": "统计结束时间在2026年的成交率，成交价为空视为未成交"},
    {"id": "rate-06", "family": "rate", "intent": "统计鼓楼区成交率"},
    {"id": "rate-07", "family": "rate", "intent": "成交率（成交/未成交）"},
    {"id": "rate-08", "family": "rate", "intent": "统计市为福州的未成交率（未成交/总数）"},
    {"id": "rate-09", "family": "rate", "intent": "按区县统计未成交率（未成交/总数）"},
    # ---- 明细提取/清洗（detail）----
    {"id": "detail-01", "family": "detail", "intent": "提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null"},
    {"id": "detail-02", "family": "detail", "intent": "筛选区县为台江区的数据，成交价为空填0，其他字段保留为空"},
    {"id": "detail-03", "family": "detail", "intent": "导出市为福州、结束时间在2026年的记录，不要统计，只要明细"},
    {"id": "detail-04", "family": "detail", "intent": "列出鼓楼区的小区、成交价和结束时间"},
    {"id": "detail-05", "family": "detail", "intent": "查出成交价为空的标的号，原样输出明细"},
    {"id": "detail-06", "family": "detail", "intent": "提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null，不要做统计"},
    # ---- 分组计数（count_group）----
    {"id": "count-01", "family": "count_group", "intent": "筛选市是福州，结束时间在2026年，小区不为空的数据，按小区分组，统计每个小区有几条数据"},
    {"id": "count-02", "family": "count_group", "intent": "统计每个区县的数据条数"},
    {"id": "count-03", "family": "count_group", "intent": "按物业类型分组统计条数，只保留条数大于等于10的分组"},
    {"id": "count-04", "family": "count_group", "intent": "按区县分组，统计每个区县有几条数据"},
    # ---- 日期筛选 ----
    {"id": "date-01", "family": "date_filter", "intent": "统计结束时间在2026年的数据条数"},
    {"id": "date-02", "family": "date_filter", "intent": "统计区县为鼓楼区且结束时间在2026-01-01到2026-06-30之间的数据条数"},
    # ---- 占比 ----
    {"id": "pct-01", "family": "proportion", "intent": "统计市是福州，结束时间在2026年，所在楼层和总层数都不为空的数据占总数的比例"},
    {"id": "pct-02", "family": "proportion", "intent": "统计鼓楼区成交价不为空的数据占总数的比例"},
    # ---- 汇总 / 排名 / 透视 / 去重 / 衍生 / 趋势 ----
    {"id": "agg-01", "family": "aggregate", "intent": "按区县统计成交价之和"},
    {"id": "agg-02", "family": "aggregate", "intent": "按区县统计成交价的平均值"},
    {"id": "rank-01", "family": "ranking", "intent": "取成交价最高的前10条数据"},
    {"id": "rank-02", "family": "ranking", "intent": "按区县统计条数，取条数最多的前5个区县"},
    {"id": "pivot-01", "family": "pivot", "intent": "按区县和物业类型做透视，统计成交价之和"},
    {"id": "dedupe-01", "family": "dedupe", "intent": "按标的号去重，保留第一条"},
    {"id": "derive-01", "family": "derive", "intent": "新增一列成交折扣，等于成交价除以评估价"},
    {"id": "trend-01", "family": "trend", "intent": "统计2026年每月的成交数变化趋势"},
    {"id": "composite-01", "family": "composite", "intent": "去重，然后提取出面积小于50或大于1000的数据，最后分析成交价的趋势"},
    {"id": "composite-02", "family": "composite", "intent": "去重，然后提取出面积小于50或大于1000的数据，最后按结束时间，每一年按季度分析每个季度的平均成交单价的趋势，成交单价 = （成交价/面积）"},
]

__all__ = ["SCENARIOS", "COLUMNS", "SOURCE", "ALIASES"]

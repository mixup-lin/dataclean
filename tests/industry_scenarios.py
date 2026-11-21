# -*- coding: utf-8 -*-
"""多行业场景矩阵：每条场景只给自然语言描述 + 期望，不给答案规则。

规则由「LLM 响应」经 /plan 生成；测试侧提供的是脚本化模型输出（含格式噪声
与一次语义错误），用来验证解析、自检/修复、意图编译与执行链路，而不是直接
把答案规则塞给 /execute。期望值全部从行业基准数据的原始行独立计算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from tests.industry_datasets import (
    PRODUCT_DIM_ROWS,
    DATASETS,
    DatasetVariant,
    IndustryDataset,
    dataset_by_id,
)


@dataclass
class ScenarioContext:
    scenario: "IndustryScenario"
    variant: DatasetVariant
    source: Dict[str, Any]
    columns: List[str]
    aliases: Dict[str, str]
    extra_sources: Dict[str, Dict[str, Any]]

    @property
    def dataset(self) -> IndustryDataset:
        return self.variant.dataset

    @property
    def scale(self) -> int:
        return self.variant.scale

    def col(self, canonical: str) -> str:
        return self.variant.col(canonical)


@dataclass
class IndustryScenario:
    id: str
    industry: str
    dataset_id: str
    title: str
    intent: str
    variants: List[str]
    rule_builder: Callable[[ScenarioContext], Dict[str, Any]]
    wrong_rule_builder: Callable[[ScenarioContext], Dict[str, Any]]
    expected_builder: Callable[[ScenarioContext], Dict[str, Any]]
    checker: str
    ops_include: List[str]
    task_type: Optional[str] = None
    extra_dim: bool = False
    focus: str = ""


def _base_rule(ctx: ScenarioContext, steps: List[Dict[str, Any]], task_type: str = "") -> Dict[str, Any]:
    rule: Dict[str, Any] = {
        "version": "2.0",
        "intent": ctx.scenario.intent,
        "source": dict(ctx.source),
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "steps": steps,
        "output": {"formats": ["csv"], "report": True},
    }
    if task_type:
        rule["task_type"] = task_type
    if ctx.extra_sources:
        rule["sources"] = {"main": dict(ctx.source), **ctx.extra_sources}
    return rule


def _wrong_count(ctx: ScenarioContext, group: str) -> Dict[str, Any]:
    return _base_rule(ctx, [
        {"op": "count_rows", "by": [ctx.col(group)], "alias": "条数"},
    ], "summary")


# ---------------------------------------------------------------------------
# 规则构造：模拟一个「大体正确但有噪声」的在线模型输出
# ---------------------------------------------------------------------------
def rule_ecom_paid_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    category = ctx.col("品类")
    status = ctx.col("支付状态")
    amount = ctx.col("订单金额")
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [category, status]},
        {"op": "convert_unit", "columns": [amount], "kind": "money"},
        {"op": "category_map", "column": status, "mapping": {
            "支付成功": "已支付", "PAID": "已支付",
            "待支付": "未支付", "UNPAID": "未支付",
        }},
        {"op": "filter", "conditions": [
            {"column": category, "operator": "eq", "value": "数码"},
        ], "logic": "and"},
        {"op": "rate", "column": status,
         "condition": {"column": status, "operator": "eq", "value": "已支付"},
         "true_label": "已支付", "false_label": "未支付",
         "by": [category], "alias": "支付命中率",
         "numerator": "hit", "denominator": "total"},
    ], "proportion")


def rule_fin_overdue_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    branch = ctx.col("分行")
    balance = ctx.col("贷款余额")
    overdue = ctx.col("逾期天数")
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [branch]},
        {"op": "convert_unit", "columns": [balance, overdue], "kind": "money"},
        {"op": "rate", "column": overdue,
         "condition": {"column": overdue, "operator": "gt", "value": 0},
         "true_label": "逾期", "false_label": "未逾期",
         "by": [branch], "alias": "逾期命中率",
         "numerator": "hit", "denominator": "total"},
    ], "proportion")


def rule_med_detail(ctx: ScenarioContext) -> Dict[str, Any]:
    dept = ctx.col("科室")
    lab = ctx.col("检验值")
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [dept, ctx.col("病种"), ctx.col("结果判定")]},
        {"op": "date_parse", "columns": [ctx.col("入院日期"), ctx.col("出院日期")],
         "formats": ["%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"]},
        {"op": "filter", "conditions": [
            {"column": dept, "operator": "eq", "value": "心内科"},
        ], "logic": "and"},
        {"op": "fill", "columns": [lab], "method": "value", "value": 0},
    ], "detail")


def rule_log_unit_price(ctx: ScenarioContext) -> Dict[str, Any]:
    waybill = ctx.col("运单号")
    carrier = ctx.col("承运商")
    freight = ctx.col("运费")
    distance = ctx.col("里程")
    shipped = ctx.col("发货时间")
    date_col = f"{shipped}_年季度"
    price = "运输单价"
    return _base_rule(ctx, [
        {"op": "dedupe", "subset": [waybill], "keep": "first"},
        {"op": "filter", "logic": "or", "conditions": [
            {"column": distance, "operator": "lt", "value": 50},
            {"column": distance, "operator": "gt", "value": 1000},
        ]},
        {"op": "expr", "column": price, "expression": "c0/c1",
         "operands": [freight, distance]},
        {"op": "date_part", "column": shipped, "parts": ["year", "quarter"],
         "bucket": "年季度"},
        {"op": "dropna", "subset": [shipped], "how": "any"},
        {"op": "aggregate", "by": [date_col],
         "column": [price, freight, distance], "func": ["mean", "sum", "sum"],
         "alias": [f"{price}·逐条均值", f"{freight}之和", f"{distance}之和"]},
        {"op": "expr", "column": price, "expression": "c0/c1",
         "operands": [f"{freight}之和", f"{distance}之和"]},
        {"op": "sort", "by": [date_col], "ascending": [True]},
    ], "trend")


def rule_edu_pass_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    klass = ctx.col("班级")
    score = ctx.col("分数")
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [klass]},
        {"op": "convert_unit", "columns": [score], "kind": "money"},
        {"op": "rate", "column": score,
         "condition": {"column": score, "operator": "gte", "value": 60},
         "true_label": "及格", "false_label": "不及格",
         "by": [klass], "alias": "及格率",
         "numerator": "hit", "denominator": "total"},
    ], "proportion")


def rule_mfg_yield(ctx: ScenarioContext) -> Dict[str, Any]:
    line = ctx.col("产线")
    qc = ctx.col("质检结果")
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [line, qc]},
        {"op": "category_map", "column": qc, "mapping": {
            "PASS": "合格", "合格": "合格", "NG": "不合格", "不合格": "不合格",
        }},
        {"op": "rate", "column": qc,
         "condition": {"column": qc, "operator": "eq", "value": "合格"},
         "true_label": "良品", "false_label": "不良品",
         "by": [line], "alias": "良品命中率",
         "numerator": "hit", "denominator": "total"},
    ], "proportion")


def rule_cross_group_count(ctx: ScenarioContext) -> Dict[str, Any]:
    target = ctx.aliases["地区"]
    return _base_rule(ctx, [
        {"op": "text_normalize", "columns": [target]},
        {"op": "count_rows", "by": [target], "alias": "条数", "sort": True},
    ], "summary")


def rule_ecom_gmv(ctx: ScenarioContext) -> Dict[str, Any]:
    category = ctx.col("品类")
    amount = ctx.col("订单金额")
    return _base_rule(ctx, [
        {"op": "convert_unit", "columns": [amount], "kind": "money"},
        {"op": "text_normalize", "columns": [category]},
        {"op": "join", "right": "商品维", "how": "left",
         "left_on": [category], "right_on": ["品类"]},
        {"op": "aggregate", "by": [category], "column": [amount],
         "func": ["sum"], "alias": ["销售额"]},
        {"op": "sort", "by": [category], "ascending": [True]},
    ], "summary")


# ---------------------------------------------------------------------------
# 期望值：全部由行业基准数据独立计算，与规则实现解耦
# ---------------------------------------------------------------------------
def exp_ecom_paid_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    paid = {"已支付", "支付成功", "PAID"}
    rows = [r for r in ctx.dataset.rows if r["品类"] == "数码"]
    hit = sum(1 for r in rows if r["支付状态"] in paid)
    return {
        "group_col": "品类", "group": "数码",
        "hit": hit * ctx.scale, "total": len(rows) * ctx.scale,
        "rate": round(hit / len(rows) * 100, 4),
        "hit_col": "已支付数", "miss_col": "未支付数", "rate_col": "支付命中率",
    }


def exp_fin_overdue_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    groups: Dict[str, List[int]] = {}
    for row in ctx.dataset.rows:
        value = row["逾期天数"]
        groups.setdefault(row["分行"], []).append(1 if isinstance(value, (int, float)) and value > 0 else 0)
    return {
        "group_col": "分行",
        "groups": {g: {"hit": sum(v) * ctx.scale, "total": len(v) * ctx.scale,
                       "rate": round(sum(v) / len(v) * 100, 4)} for g, v in groups.items()},
        "hit_col": "逾期数", "miss_col": "未逾期数", "rate_col": "逾期命中率",
    }


def exp_med_detail(ctx: ScenarioContext) -> Dict[str, Any]:
    rows = [r for r in ctx.dataset.rows if r["科室"] == "心内科"]
    total = len(rows) * ctx.scale
    expected_lab_null = 0
    expected_result_null = sum(1 for r in rows if r["结果判定"] is None) * ctx.scale
    return {
        "rows": total,
        "lab_col": "检验值", "null_col": "结果判定",
        "expected_lab_null": expected_lab_null,
        "expected_result_null": expected_result_null,
    }


def exp_log_unit_price(ctx: ScenarioContext) -> Dict[str, Any]:
    frame = pd.DataFrame([dict(r) for r in ctx.dataset.rows])
    frame = frame.drop_duplicates(subset=["运单号"], keep="first")
    frame = frame[(frame["里程"] < 50) | (frame["里程"] > 1000)]
    frame = frame.dropna(subset=["发货时间", "运费", "里程"])
    dt = pd.to_datetime(frame["发货时间"], format="mixed", errors="coerce")
    frame = frame.assign(
        _bucket=dt.dt.year.astype("Int64").astype(str)
        + "Q" + dt.dt.quarter.astype("Int64").map(lambda q: f"{q:02d}")
    )
    frame = frame.dropna(subset=["_bucket"])
    expected = {}
    for bucket, group in frame.groupby("_bucket"):
        num = float(group["运费"].sum())
        den = float(group["里程"].sum())
        expected[str(bucket)] = round(num / den, 4) if den else 0.0
    return {"time_col": "发货时间_年季度", "value_col": "运输单价", "periods": expected}


def exp_edu_pass_rate(ctx: ScenarioContext) -> Dict[str, Any]:
    groups: Dict[str, List[int]] = {}
    for row in ctx.dataset.rows:
        score = row["分数"]
        groups.setdefault(row["班级"], []).append(
            1 if isinstance(score, (int, float)) and score >= 60 else 0
        )
    return {
        "group_col": "班级",
        "groups": {g: {"hit": sum(v) * ctx.scale, "total": len(v) * ctx.scale,
                       "rate": round(sum(v) / len(v) * 100, 4)} for g, v in groups.items()},
        "hit_col": "及格数", "miss_col": "不及格数", "rate_col": "及格率",
    }


def exp_mfg_yield(ctx: ScenarioContext) -> Dict[str, Any]:
    good = {"合格", "PASS"}
    groups: Dict[str, List[int]] = {}
    for row in ctx.dataset.rows:
        groups.setdefault(row["产线"], []).append(1 if row["质检结果"] in good else 0)
    return {
        "group_col": "产线",
        "groups": {g: {"hit": sum(v) * ctx.scale, "total": len(v) * ctx.scale,
                       "rate": round(sum(v) / len(v) * 100, 4)} for g, v in groups.items()},
        "hit_col": "良品数", "miss_col": "不良品数", "rate_col": "良品命中率",
    }


def exp_cross_group_count(ctx: ScenarioContext) -> Dict[str, Any]:
    canonical = ctx.aliases["地区"]
    source = ctx.dataset.rows
    groups: Dict[str, int] = {}
    for row in source:
        value = row[canonical]
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        groups[str(value)] = groups.get(str(value), 0) + 1
    return {
        "group_col": canonical,
        "groups": {k: v * ctx.scale for k, v in groups.items()},
        "total": sum(groups.values()) * ctx.scale,
    }


def exp_ecom_gmv(ctx: ScenarioContext) -> Dict[str, Any]:
    sums: Dict[str, float] = {}
    for row in ctx.dataset.rows:
        value = row["订单金额"]
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        sums[row["品类"]] = sums.get(row["品类"], 0.0) + float(value)
    return {
        "group_col": "品类", "value_col": "销售额",
        "groups": {k: round(v * ctx.scale, 2) for k, v in sums.items()},
    }


# ---------------------------------------------------------------------------
# 场景清单
# ---------------------------------------------------------------------------
INDUSTRY_SCENARIOS: List[IndustryScenario] = [
    IndustryScenario(
        id="ecom-paid-rate", industry="电商", dataset_id="ecom_orders",
        title="支付状态同义词归一后的分品类支付率",
        intent="筛选品类为数码的数据，支付成功算命中，统计支付命中率",
        variants=["baseline", "renamed", "replaced", "shuffled", "format_mix",
                  "renamed+replaced+shuffled+format_mix"],
        rule_builder=rule_ecom_paid_rate,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "品类"),
        expected_builder=exp_ecom_paid_rate, checker="rate_group",
        ops_include=["rate"], task_type="proportion",
        focus="同义词归并、金额单位、字段重命名后筛选键仍绑定正确列",
    ),
    IndustryScenario(
        id="fin-overdue-rate", industry="金融", dataset_id="fin_loans",
        title="按分行统计逾期率（0/负天数与缺失值不命中）",
        intent="按分行统计逾期命中率，逾期天数大于0算命中",
        variants=["baseline", "renamed", "shuffled", "format_mix"],
        rule_builder=rule_fin_overdue_rate,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "分行"),
        expected_builder=exp_fin_overdue_rate, checker="rate_groups",
        ops_include=["rate"], task_type="proportion",
        focus="数值边界、缺失不命中、中文/斜杠日期与金额格式混用",
    ),
    IndustryScenario(
        id="med-detail-clean", industry="医疗", dataset_id="med_visits",
        title="明细提取：检验值填 0，其他空值保留 null",
        intent="提取科室为心内科的数据，其中检验值空值置为0，其他字段空值置为null",
        variants=["baseline", "renamed", "replaced", "shuffled"],
        rule_builder=rule_med_detail,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "科室"),
        expected_builder=exp_med_detail, checker="detail_med",
        ops_include=["filter", "fill"], task_type="detail",
        focus="明细不被汇总化、显式填充与保留 null 同时生效",
    ),
    IndustryScenario(
        id="log-unit-price-trend", industry="物流", dataset_id="log_waybills",
        title="去重+边界里程+跨年季度运输单价趋势（Σ运费/Σ里程）",
        intent="按运单号去重，然后提取里程小于50或大于1000的数据，最后按发货时间每一年按季度统计平均运输单价趋势，运输单价 = 运费/里程",
        variants=["baseline", "renamed", "replaced", "shuffled", "scaled"],
        rule_builder=rule_log_unit_price,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "承运商"),
        expected_builder=exp_log_unit_price, checker="ratio_trend",
        ops_include=["dedupe", "filter", "expr", "date_part", "dropna", "aggregate", "sort"],
        task_type="trend",
        focus="组合原语顺序、业务主键去重、跨年季度桶、比值均价口径",
    ),
    IndustryScenario(
        id="edu-pass-rate", industry="教育", dataset_id="edu_scores",
        title="按班级统计及格率（60 分边界与超范围分）",
        intent="按班级统计及格率，分数大于等于60算及格，小于60算不及格",
        variants=["baseline", "renamed", "replaced", "shuffled", "format_mix"],
        rule_builder=rule_edu_pass_rate,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "班级"),
        expected_builder=exp_edu_pass_rate, checker="rate_groups",
        ops_include=["rate"], task_type="proportion",
        focus="闭区间边界、缺考不命中、105 分异常值仍按原值判定",
    ),
    IndustryScenario(
        id="mfg-yield-rate", industry="制造", dataset_id="mfg_batches",
        title="按产线统计良品率（PASS/NG 归一）",
        intent="按产线统计良品命中率，质检结果为合格或PASS算命中",
        variants=["baseline", "renamed", "replaced", "shuffled", "format_mix",
                  "renamed+replaced+shuffled+format_mix"],
        rule_builder=rule_mfg_yield,
        wrong_rule_builder=lambda ctx: _wrong_count(ctx, "产线"),
        expected_builder=exp_mfg_yield, checker="rate_groups",
        ops_include=["rate"], task_type="proportion",
        focus="枚举同义词、空质检结果不命中、重复批次不影响分组率口径",
    ),
    IndustryScenario(
        id="cross-group-count", industry="跨行业", dataset_id="ecom_orders",
        title="同一「按地区分组统计条数」意图套用六套结构",
        intent="按地区分组统计条数",
        variants=["baseline", "renamed", "shuffled", "scaled"],
        rule_builder=rule_cross_group_count,
        wrong_rule_builder=lambda ctx: _base_rule(ctx, [
            {"op": "select", "columns": [ctx.aliases["地区"]]},
        ], "detail"),
        expected_builder=exp_cross_group_count, checker="group_count",
        ops_include=["count_rows"], task_type="summary",
        focus="同一逻辑跨行业复用；字段名变化只靠显式别名，不依赖样本语义",
    ),
    IndustryScenario(
        id="ecom-category-gmv", industry="电商×维表", dataset_id="ecom_orders",
        title="订单表关联商品维表后按品类统计销售额",
        intent="关联商品维表，按品类统计销售额之和",
        variants=["baseline", "renamed", "format_mix"],
        rule_builder=rule_ecom_gmv,
        wrong_rule_builder=lambda ctx: _base_rule(ctx, [
            {"op": "select", "columns": [ctx.col("品类"), ctx.col("订单金额")]},
        ], "detail"),
        expected_builder=exp_ecom_gmv, checker="gmv_join",
        ops_include=["join", "aggregate"], task_type="summary", extra_dim=True,
        focus="多源别名、关联键对齐、join 不膨胀、金额格式归一后求和",
    ),
]


def scenario_by_id(scenario_id: str) -> IndustryScenario:
    return next(s for s in INDUSTRY_SCENARIOS if s.id == scenario_id)


def make_context(
    scenario: IndustryScenario,
    variant: str,
    source_path: str,
    extra_paths: Optional[Dict[str, str]] = None,
    scale: int = 1,
    dataset_id: Optional[str] = None,
) -> ScenarioContext:
    dataset = dataset_by_id(dataset_id or scenario.dataset_id)
    dv = DatasetVariant(dataset=dataset, variant=variant, scale=scale)
    aliases = dict(dv.aliases)
    if scenario.id == "cross-group-count":
        # 六个行业分别把同一个描述词「地区」映射到自己的分组维度
        aliases["地区"] = CROSS_REGION_MAP[dataset.id]
    ctx = ScenarioContext(
        scenario=scenario, variant=dv,
        source={"path": source_path, "format": "csv"},
        columns=dv.columns, aliases=aliases,
        extra_sources={},
    )
    if scenario.extra_dim:
        dim_path = (extra_paths or {}).get("商品维", "")
        ctx.extra_sources["商品维"] = {"path": dim_path, "format": "csv"}
    return ctx


CROSS_REGION_MAP: Dict[str, str] = {
    "ecom_orders": "品类",
    "fin_loans": "分行",
    "med_visits": "科室",
    "log_waybills": "承运商",
    "edu_scores": "班级",
    "mfg_batches": "产线",
}


def cross_industry_contexts(source_paths: Dict[str, str], variant: str = "baseline",
                            scale: int = 1) -> List[ScenarioContext]:
    scenario = scenario_by_id("cross-group-count")
    out: List[ScenarioContext] = []
    for dataset_id, path in source_paths.items():
        out.append(make_context(scenario, variant, path, scale=scale, dataset_id=dataset_id))
    return out


__all__ = [
    "CROSS_REGION_MAP", "INDUSTRY_SCENARIOS", "IndustryScenario", "ScenarioContext",
    "make_context", "scenario_by_id",
]

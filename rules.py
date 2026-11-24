#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则层：自然语言 -> 规则 JSON -> 校验 -> PipelineConfig 映射。

规则是两条链路中「自然语言输入→规则生成→规则执行」的唯一中间产物。
LLM 的输出只被当作**不可信文本**：先解析 JSON，再经白名单校验与规范化，
绝不直接注入执行；source 与 intent 一律以调用方输入为准，防止模型篡改路径。

规则 JSON 契约（version 2.0）
----------------------------
{
  "version": "2.0",
  "intent": "原始自然语言描述",
  "source": {"path": "lots.csv", "format": "csv"},
  "options": {
    "drop_duplicates": true, "fill_missing": "median",
    "outlier_method": "iqr", "outlier_action": "clip",
    "normalize_columns": true, "drop_constant_cols": false
  },
  "steps": [
    {"op": "select", "columns": ["起拍价(元)", "成交价(元)"]},
    {"op": "filter", "column": "起拍价(元)", "operator": "gt", "value": 0},
    {"op": "sort", "by": "成交价(元)", "ascending": false}
  ],
  "output": {"formats": ["csv", "xlsx", "json"], "report": true}
}
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from llm_provider import (
    LLMError,
    LLMProvider,
    describe_provider,
    get_provider,
    parse_json_response,
)
from ops import contract as _contract
from ops.compiler import reconcile_rule_columns as _reconcile_columns
from ops.compiler import validate_steps as _validate_steps
from ops.constants import TASK_TYPES
from ops.intent import apply_intent_plan
from ops.intent_schema import compile_schema as compile_intent_schema
from ops.intent_schema import parse_intent_schema
from ops.prompt import build_catalog
from ops.prompt_repair import repair_date_steps
from ops.prompt_kit import (
    build_prompt_sections,
    build_repair_user_payload,
    primary_family,
    select_ops,
)
from ops.registry import get_op, op_names
from ops.rule_eval import BLOCKING_CODES, evaluate_rule, format_issues

LOGGER = logging.getLogger("etl")

RULE_VERSION = "2.0"

# ------------------------------------------------------------
# 白名单定义
# ------------------------------------------------------------
FORMAT_WHITELIST = ("csv", "json", "excel", "sqlite")
FILL_WHITELIST = ("none", "median", "mean", "mode", "drop", "zero", "ffill", "bfill")
OUTLIER_METHOD_WHITELIST = ("iqr", "zscore", "none")
OUTLIER_ACTION_WHITELIST = ("report", "remove", "clip")

CORE_AGG = ("count", "mean", "sum", "min", "max", "median")
EXT_AGG = ("std", "var", "nunique", "first", "last", "quantile")
AGG_WHITELIST = CORE_AGG + EXT_AGG
GROUP_AGG_WHITELIST = CORE_AGG + ("std", "var", "nunique", "first", "last")
PIVOT_AGG_WHITELIST = CORE_AGG

EXPORT_WHITELIST = ("csv", "xlsx", "json")
FILTER_OPERATORS = (
    "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "startswith", "endswith", "regex",
    "in", "not_in", "between", "notna", "isna",
)
DERIVE_OPERATORS = ("add", "sub", "mul", "div", "ratio", "pct_change", "round")
LOGIC_OPERATORS = ("and", "or")
PCT_SCOPES = ("condition", "overall", "group")
JOIN_HOWS = ("inner", "left", "right", "outer")
CAST_TYPES = ("int", "float", "str", "datetime", "category", "bool")
FILL_METHODS = ("value", "mean", "median", "mode", "ffill", "bfill", "zero", "drop")
STRING_ACTIONS = (
    "strip", "lower", "upper", "title", "replace",
    "regex_extract", "slice", "pad", "cat",
)
DATE_PARTS = ("year", "quarter", "month", "week", "day", "weekday", "hour")
DATE_PART_LABELS = {
    "year": "年", "quarter": "季度", "month": "月", "week": "周",
    "day": "日", "weekday": "星期", "hour": "小时",
}
RANK_METHODS = ("average", "min", "max", "dense", "first")
DEDUPE_KEEP = ("first", "last", "all")
BIN_METHODS = ("width", "quantile")

# pct 算子产出的固定列名：分子 / 分母 / 占比（三口径共用，便于下游排序与筛选）
PCT_NUM_COL = "分子"
PCT_DEN_COL = "分母"
PCT_DEFAULT_ALIAS = "占比"
PCT_COLUMNS = (PCT_NUM_COL, PCT_DEN_COL, PCT_DEFAULT_ALIAS)

# 允许由规则覆盖的 PipelineConfig 选项（其余字段保持核心默认值）
BOOL_OPTIONS = (
    "drop_duplicates", "normalize_columns", "clean_text",
    "drop_empty_cols", "drop_constant_cols",
)
ENUM_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "fill_missing": FILL_WHITELIST,
    "outlier_method": OUTLIER_METHOD_WHITELIST,
    "outlier_action": OUTLIER_ACTION_WHITELIST,
    "pivot_agg": PIVOT_AGG_WHITELIST,
}
NUMERIC_OPTIONS = ("outlier_threshold", "max_charts")
STR_OPTIONS = (
    "group_by", "pivot_index", "pivot_columns", "pivot_values",
    "time_column", "resample_freq", "encoding", "sqlite_table",
    "sqlite_query", "excel_sheet",
)
LIST_OPTIONS = ("agg_funcs", "resample_agg")

# steps 算子白名单：唯一事实源是 OpSpec 注册表，此处只做派生，不再手写第二份。
# 参数契约（必填字段/类型/枚举/列引用）由 ops.contract 从 ParamSpec 统一校验。
STEP_SCHEMA: Dict[str, Dict[str, Any]] = {name: {} for name in op_names()}


class _OpNamesView:
    """动态视图：任何时刻都返回注册表当前的算子名（含运行时插件算子）。"""

    def __iter__(self):
        return iter(op_names())

    def __len__(self) -> int:
        return len(op_names())

    def __contains__(self, item: Any) -> bool:
        return item in op_names()

    def __repr__(self) -> str:
        return f"STEP_OPS({list(self)})"


STEP_OPS = _OpNamesView()

DEFAULT_OUTPUT: Dict[str, Any] = {"formats": ["csv"], "report": True}


from ops.errors import RuleValidationError  # noqa: E402  统一异常类型，避免双类


# ------------------------------------------------------------
# Prompt 组装
# ------------------------------------------------------------
_RULES_SYSTEM = """
你是数据处理规则生成器。仅输出一个 JSON 对象，不要解释、不要代码围栏。
规则必须符合以下契约：
{
  "version": "2.0",
  "intent": "<原样保留用户描述>",
  "task_type": "<可选，显式声明任务类型：summary|ranking|trend|distribution|proportion|pivot|join|correlation|detail>",
  "source": {"path": "<数据源路径>", "format": "csv|json|excel|sqlite"},
  "options": {
    "drop_duplicates": true|false,
    "fill_missing": "none|median|mean|mode|drop|zero|ffill|bfill",
    "outlier_method": "iqr|zscore|none",
    "outlier_action": "report|remove|clip",
    "outlier_threshold": 1.5,
    "normalize_columns": true, "clean_text": true,
    "drop_empty_cols": true, "drop_constant_cols": false,
    "max_charts": 6,
    "group_by": "<列名或 null>", "agg_funcs": ["count","mean","sum","min","max"],
    "time_column": "<列名或 null>", "resample_freq": "D|W|M|Q|Y",
    "encoding": "utf-8"
  },
  "steps": [
    {"op":"filter","conditions":[{"column":"<列>","operator":"gt","value":0}],"logic":"and"},
    {"op":"filter","column":"<列>","operator":"in","value":["A","B"]},
    {"op":"dedupe","subset":["<列>"],"keep":"first"},
    {"op":"cast","columns":["<列>"],"to":"float|int|str|datetime|category"},
    {"op":"fill","columns":["<列>"],"method":"value|mean|median|mode|ffill|bfill|zero","value":0},
    {"op":"string","column":"<列>","action":"strip|lower|upper|replace|regex_extract|slice","pattern":"...","repl":"...","into":"<新列>"},
    {"op":"split","column":"<列>","sep":"-","into":["<新列1>","<新列2>"]},
    {"op":"concat","columns":["<列1>","<列2>"],"sep":"-","into":"<新列>"},
    {"op":"date_part","column":"<日期列>","parts":["year","month"]},  # 生成 <日期列>_年、<日期列>_月 等新列
    {"op":"bin","column":"<数值列>","bins":5,"method":"width|quantile","into":"<新列>"},
    {"op":"assign","column":"<新列>","rules":[{"condition":{"column":"<列>","operator":"gt","value":0},"value":"高"}],"default":"低"},
    {"op":"derive","column":"<新列>","operator":"add|sub|mul|div|ratio|round","left":"<列>","right":"<列或数字>"},
    {"op":"sort","by":["<列1>","<列2>"],"ascending":[false,true]},
    {"op":"rank","by":"<数值列>","into":"<排名列>","method":"dense","group_by":"<可选分组列>","topn":10},
    {"op":"aggregate","by":["<分组列>"],"column":["<数值列>"],"func":["sum","count"],"alias":["<结果列>"]},
    {"op":"pct","scope":"condition|overall|group","condition":{"column":"<列>","operator":"gt","value":0},"by":"<分组列>","column":"<数值列>","within":"<组内列>","alias":"<占比列>","cumulative":false},
    {"op":"pct","scope":"condition","condition":{"conditions":[{"column":"<列A>","operator":"notna"},{"column":"<列B>","operator":"notna"}],"logic":"and"},"alias":"<占比列>"},
    {"op":"rate","column":"<判空列>","condition":{"column":"<判空列>","operator":"notna"},"true_label":"成交","false_label":"未成交","numerator":"hit|miss","denominator":"total|hit|miss","by":["<可选分组列>"],"alias":"<目标率列名>"},
    {"op":"pivot","index":"<行维度>","columns":"<列维度>","values":"<数值列>","aggfunc":"sum"},
    {"op":"join","right":"<别名>","how":"left|inner|right|outer","left_on":"<主表键>","right_on":"<右表键>","suffixes":["","_右表"]},
    {"op":"union","right":"<别名>"},
    {"op":"sample","n":1000,"random_state":42},
    {"op":"head","n":100},
    {"op":"select","columns":["<列>"]},
    {"op":"date_filter","column":"<日期列>","year":2026},
    {"op":"date_filter","column":"<日期列>","start":"2026-01-01","end":"2026-12-31"},
    {"op":"count_rows","by":["<分组列>"],"alias":"条数"},
    {"op":"having","column":"条数","operator":"gte","value":10},
    {"op":"group_rank","by":"<分组列>","column":"<数值列>","topn":3},
    {"op":"resample","column":"<日期列>","freq":"M","funcs":["mean","count"]},
    {"op":"rolling","column":"<数值列>","window":7,"func":"mean"},
    {"op":"lag","columns":["<数值列>"],"periods":1},
    {"op":"date_parse","columns":["<日期列>"],"formats":["%Y/%m/%d","%Y年%m月%d日"]},
    {"op":"corr","columns":["<数值列1>","<数值列2>"],"method":"pearson"},
    {"op":"convert_unit","columns":["<金额列>"],"kind":"money"},
    {"op":"text_normalize","columns":["<文本列>"]},
    {"op":"category_map","column":"<类别列>","mapping":{"北京市":"北京"},"min_count":10},
    {"op":"dedupe_fuzzy","columns":["<名称列>"],"similarity":0.9},
    {"op":"cross_column_filter","left":"<列A>","right":"<列B>","operator":"gt"},
    {"op":"assert","checks":[{"column":"<金额列>","operator":"gt","value":0}],"action":"flag"},
    {"op":"profile"},
    {"op":"mask_pii","columns":["<手机号列>"],"kinds":["phone"]},
    {"op":"anti_join","right":"<别名>","left_on":"<主表键>","right_on":"<右表键>"},
    {"op":"semi_join","right":"<别名>","left_on":"<主表键>","right_on":"<右表键>"}
  ],
  "output": {"formats":["csv","xlsx","json"],"report":true}
}
严格要求：
1) 只能使用 op 白名单中的算子，不得生成代码、表达式或文件操作指令。
2) steps 中引用的列名必须来自「可用列」清单，不得臆造。
3) source / sources 只能引用给定数据源，不得改写为其它路径。
4) 无对应需求时 steps 为空数组，options 使用示例默认值。
5) 筛选必须用 steps.filter，分组计数必须用 steps.aggregate（func="count"）；
   不要只设置 options.group_by，也不要用 options.fill_missing="drop" 代替筛选。
6) 除非用户明确要求「删除含缺失的行」，否则严禁使用 fill_missing="drop"；
   缺失密集的数据集上它会把数据全部删光，导致结果为空。
7) 用户问「每个分组有几条 / 条数」时，最终结果表的每一行就是一个分组，
   其中一列是计数，必须能直接回答该问题。
8) 占比必须用 steps.pct：条件占比 scope="condition"；整体构成占比
   scope="overall" 且提供 by；组内占比 scope="group" 且提供 by 与 within；
   需要帕累托/累计占比时 cumulative=true。
   条件占比的 condition 可以是单条件 {"column","operator","value"}，也可是复合条件
   {"conditions":[{...},{...}],"logic":"and"|"or"}，用于「A 和 B 都不为空」「占比 = 满足
   多条件的行数 / 全部行数」这类需求；"不空"用 operator="notna"、"为空"用 "isna"。
9) 多表需求在 sources 中声明附加表，并在 steps 中用 join/union 引用其别名，
   只能使用已给出的别名。
10) 趋势/按月/同比需求用 date_part + aggregate 表达；排名/TopN 用 rank；
    分箱用 bin；分段打标用 assign。
11) 最终结果必须能直接回答用户问题：占比任务要有占比列，排名任务要按值降序，
    透视任务要是一张交叉表。
12) 多步需求按顺序编排：先筛选，再做统计。例如用户问
    「筛选地区是福州、结束时间在2026年的数据，统计其中所在楼层和总楼层都不为空
    的数据占总数的比例」，应生成：
    steps: [
      {"op":"filter","conditions":[
         {"column":"地区","operator":"eq","value":"福州"},
         {"column":"所在楼层","operator":"notna"},
         {"column":"总楼层","operator":"notna"}],"logic":"and"},
      {"op":"date_filter","column":"结束时间","year":2026},
      {"op":"pct","scope":"condition",
       "condition":{"conditions":[
         {"column":"所在楼层","operator":"notna"},
         {"column":"总楼层","operator":"notna"}],"logic":"and"},
       "alias":"非空占比"}
    ]
    —— 年份筛选用 date_filter 一步完成，不要为了筛选而造 date_part 中间列；
       占比的需求一定要用 pct 算子，不要只做筛选。
       注意：占比任务的分母是「筛选后的全部行」，因此不要把「非空」条件
       重复放进筛选步骤，只放进 pct.condition。
13) 筛选条件的列必须与需求里的主语对应：优先查 column_aliases（使用者提供的
    「描述 → 真实列名」映射），命中就用右侧列名；没有映射时在 columns 里选语义最贴近的
    现有列，绝不臆造列名。「小区不为空」这类条件要写在被描述的那一列上，
    严禁把「不为空 / 为空」套到与主语无关的数值列上。
    多个条件按需求描述的顺序放进同一个 filter 的 conditions 数组，不要拆成互相矛盾的多步筛选。
14) 常规清洗/整理优先用对应算子，而不是全局 options：
    dropna（按列删缺失行）、replace（取值统一/别名归并）、clip（数值截断）、
    outlier（异常值标记/裁剪/删除）、normalize（minmax/zscore/rank_pct）、
    date_diff（两时间列相差天/月/年）、cumsum（累计，可按分组）、
    melt（宽表转长表）、describe（数值列统计概览）、tail（取后 N 行）、dedupe（去重）。
15) 分组计数类需求（「每个 X 有几条」）的最终表必须一行一个 X、其中一列是计数，
    不得只做筛选或只输出明细。
16) column_aliases 是使用者显式提供的「描述 → 真实列名」映射（例如 {"地区": "city"}）。
    命中映射时一切列引用都必须使用右侧的真实列名；程序不内置任何语义词典，
    也不允许你在没有映射、没有同名列的情况下把描述词替换成自认为相近的列名。
17) 日期筛选优先用 ``date_filter`` 一步完成（year/quarter/month/start/end），
    不要为了筛选而先用 date_part 造中间列；date_part 只用于真正要按时间维度分组的趋势需求。
18) 「每个 X 有几条 / 分组计数」优先用 ``count_rows``（无需指定计数值列），
    这样不会因计数列存在缺失而少算；需要同时算多个指标时才用 aggregate。
19) 用户明确说「统计 / 汇总 / 分布 / 趋势 / 占比」时，可在顶层写 task_type 显式声明，
    程序以该声明为准，不再从步骤里猜测。取值口径必须与方法一致：
    「统计/汇总/每个分组有几条」→ summary；「分箱/区间分布」→ distribution；
    「按月/逐年趋势」→ trend；「占比」→ proportion；「透视交叉表」→ pivot；
    「排名/TopN」→ ranking；「多表关联」→ join；「相关系数」→ correlation。
    纯分组计数绝不是 distribution，也不要写 trend。
20) 单位混用（1.2万 / ¥1,234 / 120㎡）先用 convert_unit 统一；全半角、零宽字符
    用 text_normalize；同义类别用 category_map；近似重复用 dedupe_fuzzy；
    业务规则用 assert；列画像用 profile；敏感字段用 mask_pii 脱敏。
21) **严禁对原始日期时间列用 eq/ne 搭配 4 位年份字符串**（如
    {"column":"结束时间","operator":"eq","value":"2026"}）。该列的真实值是
    完整时间戳「2026-03-08T10:00:00+08:00」，与「2026」字符串全等永远为 False，
    会把整张表筛成 0 行。凡是「X 年在 / 某年 / 某季度 / 某月 / 日期区间 /
    近 N 天」的需求，一律用 date_filter 的 year/quarter/month/start/end/recent_days；
    只有真正要「按时间维度分组做趋势」时才用 date_part。
22) 「小区不为空」「A 和 B 都不为空」等条件用 operator="notna"，不要写
    ne + 空字符串；多个条件按需求顺序放进同一个 filter 的 conditions 数组。
23) **「成交率 / 未成交率 / 转化率 / 成功率 / 及格率 / 空值率 / 两类之比」
    这类「按某列是否为空（或任意条件）分成两类，再求比率」必须用 steps.rate**。
    方向由 numerator 决定（hit=命中类，miss=未命中类），分母由 denominator
    决定（total=两类总数，默认；hit/miss 用于 A/B 之比）。换问法只改这两个
    参数与 alias，不要换算子、不要改链路。例如「统计未成交率（未成交/总数）」：
      steps: [
        {"op":"filter","conditions":[{"column":"市","operator":"eq","value":"福州"}],"logic":"and"},
        {"op":"rate","column":"成交价(元)","condition":{"column":"成交价(元)","operator":"notna"},
         "true_label":"成交","false_label":"未成交","numerator":"miss","alias":"未成交率"}
      ]
    「统计成交率」则把 numerator 改成 "hit"、alias 改成 "成交率"；一律写
    task_type="proportion"。rate 一次产出「成交数 / 未成交数 / 总数 / 目标率 /
    两类比」，结果表一行即可回答问题。
    严禁把这类需求写成 assign + count_rows（没有比率列，且清洗填充缺失会把两类
    合并成一类），也不要用 pct 代替（pct 只给命中占比，不给两类条数与两类比值），
    更不要在 rate 之后再接 pct（rate 的结果已是汇总表，不是逐行标签）。
    需要分组成交率时才在 rate.by 里写分组列。
24) **分类判据不是筛选条件**：「成交价为空=未成交，不为空=成交」是 rate 的
    两类定义，绝不能写进 filter。filter 里一旦出现判据列的 isna/notna，
    分母会被截断成只剩一类，比率必然算错或为 0。只有需求里明确的筛选
    （「市：福州」「区县：鼓楼区」「小区不为空」「结束时间在2026年」）才写进
    filter / date_filter，并按描述顺序放进同一个 filter 的 conditions 数组，
    不要拆成互相矛盾的多步筛选。
25) 只要规则里出现 isna / notna（含 assign.rules[].condition、pct.condition、
    rate.condition、filter.conditions），就表示业务语义依赖「原始是否为空」：
    options.fill_missing 保持常规值，严禁 "drop"；执行层会自动还原判空列的
    原始空值，不要为了绕过填充而自己造中间标记列。
26) **「提取 / 导出 / 列出 / 保留 / 查出 / 清洗 + 数据」这类明细需求**，
    结果必须是逐行明细，严禁生成 aggregate / count_rows / pivot / rate / pct /
    rank / group_rank 等汇总算子，也不要写 task_type="summary"；写
    task_type="detail"。按列筛选用 steps.filter；「某列空值置为某值」用
    steps.fill（method="value", value 为实际值）；要求「其他字段空值置为
    null / 保留为空 / 不填充」时 options.fill_missing 写 "none"（新增可选值），
    严禁写 median / zero 把原始空值提前填掉。
    例：「提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null」
    → steps: [{"op":"filter","conditions":[{"column":"区县","operator":"eq",
    "value":"鼓楼区"}],"logic":"and"},{"op":"fill","columns":["成交价(元)"],
    "method":"value","value":0}], options.fill_missing="none", task_type="detail"。
"""


# 通用契约按编号切片：按任务族只注入相关条款，避免全量 26 条稀释注意力。
_RULE_BLOCKS: Dict[int, str] = {}
_RULE_HEADER = ""
_RULE_MATCHES = list(re.finditer(r"(?m)^\s*(\d+)\)\s", _RULES_SYSTEM))
if _RULE_MATCHES:
    _RULE_HEADER = _RULES_SYSTEM[: _RULE_MATCHES[0].start()].strip()
    for _idx, _match in enumerate(_RULE_MATCHES):
        _end = (
            _RULE_MATCHES[_idx + 1].start()
            if _idx + 1 < len(_RULE_MATCHES)
            else len(_RULES_SYSTEM)
        )
        _RULE_BLOCKS[int(_match.group(1))] = _RULES_SYSTEM[_match.start():_end].strip()

_CORE_RULE_IDS = frozenset({1, 2, 3, 4, 6, 11, 13, 14, 16, 17, 21, 22, 25})
_FAMILY_RULE_IDS: Dict[str, frozenset] = {
    "composite": frozenset({5, 12, 15, 19, 23, 26}),
    "rate": frozenset({8, 19, 23, 24}),
    "detail": frozenset({14, 22, 26}),
    "count_group": frozenset({5, 7, 15, 18, 19}),
    "date_filter": frozenset({12, 17, 19, 21}),
    "proportion": frozenset({8, 12, 19}),
    "ranking": frozenset({10, 11, 19}),
    "trend": frozenset({10, 17, 19}),
    "pivot": frozenset({11, 19}),
    "dedupe": frozenset({14, 19}),
    "derive": frozenset({14, 20, 26}),
    "aggregate": frozenset({5, 11, 19}),
}


def build_contract(families: Sequence[str]) -> str:
    """按任务族拼装契约：核心条款 + 命中族的专属条款。"""
    if not _RULE_BLOCKS:
        return _RULES_SYSTEM
    rule_ids = set(_CORE_RULE_IDS)
    for family in families:
        rule_ids |= _FAMILY_RULE_IDS.get(family, frozenset())
    blocks = [
        _RULE_BLOCKS[number]
        for number in sorted(_RULE_BLOCKS)
        if number in rule_ids
    ]
    return (_RULE_HEADER + "\n" + "\n".join(blocks)).strip()


_ALIAS_SEPARATORS = ("=>", "=", "：", ":", "\t")


def _split_alias_line(line: str) -> Optional[Tuple[str, str]]:
    """把一行「描述=列名」拆成二元组；空行与 ``#`` 注释返回 None。"""
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    for sep in _ALIAS_SEPARATORS:
        if sep in text:
            label, column = text.split(sep, 1)
            label, column = label.strip(), column.strip()
            return (label, column) if label and column else None
    return None


def parse_column_aliases(
    raw: Any, columns: Optional[List[str]] = None
) -> Tuple[Dict[str, str], List[str]]:
    """解析使用者提供的「描述 → 真实列名」映射。

    支持 dict、多行文本（``地区=city``，也接受 ``：``/``:``/``tab``/``=>``）与字符串数组；
    ``#`` 开头为注释。映射值必须是真实列，否则丢弃并给出警告——列名对应关系
    完全由使用者显式给出，程序不做任何语义猜测。
    """
    pairs: List[Tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(k), str(v)) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        pairs = [p for p in (_split_alias_line(str(x)) for x in raw) if p]
    elif isinstance(raw, str):
        pairs = [
            p for p in (_split_alias_line(line) for line in re.split(r"[\r\n;；]+", raw)) if p
        ]
    known = {str(c) for c in (columns or [])}
    mapping: Dict[str, str] = {}
    warnings: List[str] = []
    for label, column in pairs:
        if known and column not in known:
            warnings.append(
                f"列名映射「{label} → {column}」引用了不存在的列，已忽略"
            )
            continue
        if label in mapping and mapping[label] != column:
            warnings.append(f"列名映射「{label}」重复定义，已采用最后一条")
        mapping[label] = column
    return mapping, warnings


def build_rule_prompt(
    intent: str,
    source: Dict[str, Any],
    columns: List[str],
    sources: Optional[Dict[str, Any]] = None,
    source_columns: Optional[Dict[str, List[str]]] = None,
    column_aliases: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """返回 (system, user)；user 为结构化 JSON，供在线模型消费。"""
    payload: Dict[str, Any] = {
        "intent": intent,
        "source": source,
        "columns": columns[:200],
        "allowed_ops": list(STEP_OPS),
        "allowed_formats": list(EXPORT_WHITELIST),
        "instruction": "请将需求转换为符合契约的规则 JSON，仅输出 JSON。",
    }
    if column_aliases:
        payload["column_aliases"] = dict(column_aliases)
    if sources:
        payload["sources"] = {
            alias: {
                "source": src,
                "columns": (source_columns or {}).get(alias, [])[:200],
            }
            for alias, src in sources.items()
        }
    # 提示词工程：任务路由 + 少样本 + 负面清单 + 真实列/值 + 输出前自检。
    # 只有「先分类、再填槽」才能避免同一类需求换个问法就生成另一种链路。
    hints = probe_value_hints(source, columns)
    guidance, router_info = build_prompt_sections(
        intent, columns, hints=hints, aliases=column_aliases, source=source,
    )
    payload["task_router"] = router_info
    reminder = router_info.pop("reminder", "")
    payload["task_router"]["reminder"] = reminder
    families = list(router_info.get("families") or [])
    catalog_ops = select_ops(families)
    payload["allowed_ops_for_intent"] = catalog_ops
    # 按需引用：只注入命中任务族的契约条款与算子目录，仍是注册表单一事实源。
    system = (
        guidance
        + "\n\n===== 通用契约（按本需求任务族筛选；与骨架冲突时以骨架为准） =====\n"
        + build_contract(families)
        + "\n\n本需求可用算子目录（按需生成，只能使用以下 op 与参数）：\n"
        + build_catalog(catalog_ops)
        + "\n\n" + reminder
    )
    return system, json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------
# 校验与规范化
# ------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_source(
    source: Dict[str, Any], errors: List[str], prefix: str = "source"
) -> None:
    if not isinstance(source, dict):
        errors.append(f"{prefix} 必须是对象")
        return
    fmt = source.get("format")
    if fmt not in FORMAT_WHITELIST:
        errors.append(f"{prefix}.format 非法：{fmt!r}，可选 {FORMAT_WHITELIST}")
    path = source.get("path")
    if not isinstance(path, str) or not path.strip():
        errors.append(f"{prefix}.path 必须是非空字符串")
    elif "\x00" in path or any(p in path for p in ("://",)):
        errors.append(f"{prefix}.path 含非法字符或 URL，只允许本地文件路径")


def _check_multi_sources(sources: Any, errors: List[str]) -> None:
    if not isinstance(sources, dict) or not sources:
        errors.append("sources 必须是非空对象")
        return
    if "main" not in sources:
        errors.append("sources 必须包含 main 主表")
    if len(sources) > 5:
        errors.append("sources 最多 5 个数据源")
    for alias, src in sources.items():
        if not isinstance(alias, str) or not alias.strip():
            errors.append("sources 的别名必须是非空字符串")
            continue
        _check_source(src, errors, prefix=f"sources.{alias}")


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return list(value)
    return [value] if value is not None else []


def _dedupe_name(name: str, used: set) -> str:
    """列名冲突时追加序号，保证 aggregate 产出的列名稳定且唯一。"""
    if name not in used:
        return name
    i = 2
    while f"{name}_{i}" in used:
        i += 1
    return f"{name}_{i}"


def aggregate_pairs(step: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """展开 aggregate 的 (列, 函数, 别名) 三元组。

    执行层与校验层共用同一口径，避免默认列名在两侧不一致导致后续步骤
    引用不到聚合结果。
    """
    cols = [str(c) for c in _as_list(step.get("column"))]
    funcs = [str(f) for f in _as_list(step.get("func"))] or ["count"]
    if len(funcs) == 1:
        pairs = [(c, funcs[0]) for c in cols]
    elif len(cols) == 1:
        pairs = [(cols[0], f) for f in funcs]
    elif len(funcs) == len(cols):
        pairs = list(zip(cols, funcs))
    else:
        pairs = [(cols[0], funcs[0])] if cols else []
    aliases = [str(a) for a in _as_list(step.get("alias")) if str(a).strip()]
    used = {str(b) for b in _as_list(step.get("by"))}
    out: List[Tuple[str, str, str]] = []
    for i, (col, func) in enumerate(pairs):
        alias = aliases[i] if i < len(aliases) else ""
        if not alias:
            alias = "条数" if func == "count" else f"{col}_{func}"
        if alias in used:
            alias = _dedupe_name(f"{col}_{func}", used)
        used.add(alias)
        out.append((col, func, alias))
    return out


def pct_alias(step: Dict[str, Any]) -> str:
    alias = step.get("alias")
    return str(alias).strip() if isinstance(alias, str) and alias.strip() else PCT_DEFAULT_ALIAS


def _check_options(options: Dict[str, Any], errors: List[str]) -> None:
    for key in BOOL_OPTIONS:
        if key in options and not isinstance(options[key], bool):
            errors.append(f"options.{key} 必须是布尔值")
    for key, allowed in ENUM_OPTIONS.items():
        if key in options and options[key] not in allowed:
            errors.append(f"options.{key} 非法：{options[key]!r}，可选 {allowed}")
    for key in NUMERIC_OPTIONS:
        if key in options and not _is_number(options[key]):
            errors.append(f"options.{key} 必须是数字")
    if "outlier_threshold" in options and options["outlier_threshold"] <= 0:
        errors.append("options.outlier_threshold 必须为正数")
    if "max_charts" in options and options["max_charts"] < 0:
        errors.append("options.max_charts 不能为负数")
    for key in STR_OPTIONS:
        if key in options and options[key] is not None and not isinstance(options[key], str):
            errors.append(f"options.{key} 必须是字符串或 null")
    for key in LIST_OPTIONS:
        if key in options:
            val = options[key]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                errors.append(f"options.{key} 必须是字符串数组")
            elif key == "agg_funcs" and any(x not in GROUP_AGG_WHITELIST for x in val):
                errors.append(f"options.{key} 含非法聚合函数")
    # 透视表三要素一致性（与 PipelineConfig.__post_init__ 保持一致）
    pivots = [options.get(k) for k in ("pivot_index", "pivot_columns", "pivot_values")]
    if any(pivots) and not all(pivots):
        errors.append("options 中透视表三要素必须同时提供")


def _check_condition(
    cond: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    column = cond.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"{tag}.column 必须是非空字符串")
    elif known and column not in known:
        errors.append(f"{tag}.column 引用了不存在的列 {column!r}")
    operator = cond.get("operator")
    if operator not in FILTER_OPERATORS:
        errors.append(f"{tag}.operator 非法算子 {operator!r}")
        return
    value = cond.get("value")
    if operator in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            errors.append(f"{tag}.value 必须是非空数组")
    elif operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            errors.append(f"{tag}.value 必须是 [下界, 上界]")
    elif operator not in ("notna", "isna") and "value" not in cond:
        errors.append(f"{tag} 缺少字段 value")


def _check_condition_or_compound(
    cond: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    """校验单条件或复合条件（conditions + logic）。"""
    if isinstance(cond.get("conditions"), list) and cond["conditions"]:
        if cond.get("logic", "and") not in LOGIC_OPERATORS:
            errors.append(f"{tag}.logic 必须是 and 或 or")
        for i, c in enumerate(cond["conditions"]):
            if not isinstance(c, dict):
                errors.append(f"{tag}.conditions[{i}] 必须是对象")
                continue
            _check_condition(c, f"{tag}.conditions[{i}]", known, errors)
    else:
        _check_condition(cond, tag, known, errors)


def _check_filter(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    conditions = step.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"{tag}.conditions 必须是非空数组")
            return
        if step.get("logic", "and") not in LOGIC_OPERATORS:
            errors.append(f"{tag}.logic 必须是 and 或 or")
        for i, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                errors.append(f"{tag}.conditions[{i}] 必须是对象")
                continue
            _check_condition(cond, f"{tag}.conditions[{i}]", known, errors)
    else:
        _check_condition(step, tag, known, errors)


def _check_aggregate(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    bys = _as_list(step.get("by"))
    if not bys or not all(isinstance(x, str) and x for x in bys):
        errors.append(f"{tag}.by 必须是字符串或字符串数组")
    for col in bys:
        if isinstance(col, str) and known and col not in known:
            errors.append(f"{tag}.by 引用了不存在的列 {col!r}")
    cols = _as_list(step.get("column"))
    funcs = _as_list(step.get("func")) or ["count"]
    if not cols:
        errors.append(f"{tag}.column 不能为空")
    for col in cols:
        if not isinstance(col, str) or not col:
            errors.append(f"{tag}.column 必须是字符串或字符串数组")
        elif known and col not in known:
            errors.append(f"{tag}.column 引用了不存在的列 {col!r}")
    for func in funcs:
        if func not in AGG_WHITELIST:
            errors.append(f"{tag}.func 非法聚合函数 {func!r}")
    if len(funcs) not in (1, len(cols)) and len(cols) != 1:
        errors.append(f"{tag}.func 数量需为 1 或与 column 数量一致")
    alias = step.get("alias")
    if alias is not None and not isinstance(alias, (str, list)):
        errors.append(f"{tag}.alias 必须是字符串或字符串数组")


def _check_pct(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    scope = step.get("scope")
    if scope not in PCT_SCOPES:
        errors.append(f"{tag}.scope 非法，可选 {list(PCT_SCOPES)}")
        return
    for key in ("by", "column", "within"):
        for col in _as_list(step.get(key)):
            if not isinstance(col, str) or not col:
                errors.append(f"{tag}.{key} 必须是字符串或字符串数组")
            elif known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")
    if scope == "condition":
        condition = step.get("condition")
        if not isinstance(condition, dict):
            errors.append(f"{tag}.condition 必须是对象")
        else:
            _check_condition_or_compound(condition, f"{tag}.condition", known, errors)
    if scope == "group" and not step.get("within"):
        errors.append(f"{tag}.within 在组内占比时必须提供")
    if scope == "overall" and not (step.get("by") or step.get("column")):
        errors.append(f"{tag} 需提供 by 或 column")
    if step.get("cumulative") is not None and not isinstance(step["cumulative"], bool):
        errors.append(f"{tag}.cumulative 必须是布尔值")


def _check_pivot(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    for key in ("index", "columns", "values"):
        for col in _as_list(step.get(key)):
            if not isinstance(col, str) or not col:
                errors.append(f"{tag}.{key} 必须是字符串或字符串数组")
            elif known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")
    aggfunc = step.get("aggfunc")
    if aggfunc is not None and aggfunc not in PIVOT_AGG_WHITELIST:
        errors.append(f"{tag}.aggfunc 非法，可选 {list(PIVOT_AGG_WHITELIST)}")


def _check_typed_columns(
    step: Dict[str, Any], tag: str, known: set, errors: List[str]
) -> None:
    op = step.get("op")
    keys = ["column", "columns", "by", "left", "within", "index"]
    if op in ("derive", "assign"):
        keys = [k for k in keys if k != "column"]  # 该算子的 column 是输出列
    for key in keys:
        for col in _as_list(step.get(key)):
            if isinstance(col, str) and known and col not in known:
                errors.append(f"{tag}.{key} 引用了不存在的列 {col!r}")


def _check_steps(steps: Any, columns: List[str], errors: List[str]) -> None:
    """委托注册表契约校验全部步骤（校验规则的单一事实源）。"""
    errors.extend(_validate_steps(steps, columns or []))


def _check_output(output: Dict[str, Any], errors: List[str]) -> None:
    if not isinstance(output, dict):
        errors.append("output 必须是对象")
        return
    fmts = output.get("formats", DEFAULT_OUTPUT["formats"])
    if not isinstance(fmts, list) or any(f not in EXPORT_WHITELIST for f in fmts):
        errors.append(f"output.formats 非法，可选 {list(EXPORT_WHITELIST)}")
    if "report" in output and not isinstance(output["report"], bool):
        errors.append("output.report 必须是布尔值")


def normalize_rule(
    rule: Dict[str, Any],
    source: Dict[str, Any],
    intent: str,
    sources: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """补齐默认值；强制以调用方输入覆盖 source / intent（防模型篡改）。"""
    out = dict(rule) if isinstance(rule, dict) else {}
    out["version"] = RULE_VERSION
    out["intent"] = intent
    declared = out.get("task_type")
    if isinstance(declared, str) and declared in TASK_TYPES:
        out["task_type"] = declared
    else:
        out.pop("task_type", None)
    src = dict(source)
    out["source"] = src
    if sources:
        cleaned = {
            str(k): dict(v) for k, v in sources.items() if isinstance(v, dict)
        }
        cleaned.setdefault("main", src)
        out["sources"] = cleaned
    else:
        out.pop("sources", None)
    options = out.get("options")
    out["options"] = options if isinstance(options, dict) else {}
    steps = out.get("steps")
    out["steps"] = steps if isinstance(steps, list) else []
    output = out.get("output")
    out["output"] = {
        "formats": list(output.get("formats", DEFAULT_OUTPUT["formats"]))
        if isinstance(output, dict) and isinstance(output.get("formats"), list)
        else list(DEFAULT_OUTPUT["formats"]),
        "report": bool(output.get("report", True)) if isinstance(output, dict) else True,
    }
    out.pop("generated_by", None)
    return out


def reconcile_rule_columns(
    rule: Dict[str, Any], columns: List[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """委托编译层做列名对齐（含 date_part 派生列短标签别名回退）。"""
    return _reconcile_columns(rule, columns)


def _step_outputs(step: Dict[str, Any]) -> List[str]:
    """步骤新增的列名：委托注册表声明，新增算子自动参与链式引用校验。"""
    return _contract.step_outputs(step)


def _step_refs(step: Dict[str, Any]) -> Optional[List[Any]]:
    """步骤引用的输入列：委托注册表声明；None 表示无需列存在性校验。"""
    return _contract.step_refs(step)


_ALLOWED_OPTION_KEYS = frozenset(
    BOOL_OPTIONS + tuple(ENUM_OPTIONS) + NUMERIC_OPTIONS + STR_OPTIONS + LIST_OPTIONS
)


def _fix_step(step: Dict[str, Any], columns: List[str]) -> Optional[Dict[str, Any]]:
    """对单个候选步骤做最小修复；无法修复时返回 None（由调用方剔除）。"""
    op = step.get("op")
    fixed = dict(step)
    if op == "filter":
        conds = fixed.get("conditions")
        if isinstance(conds, list):
            valid = [
                c for c in conds
                if isinstance(c, dict) and c.get("operator") in FILTER_OPERATORS
            ]
            if not valid:
                return None
            fixed["conditions"] = valid
            if fixed.get("logic") not in LOGIC_OPERATORS:
                fixed["logic"] = "and"
            return fixed
        return fixed if fixed.get("operator") in FILTER_OPERATORS else None
    if op == "aggregate":
        funcs = [f for f in _as_list(fixed.get("func")) if f in AGG_WHITELIST]
        cols = _as_list(fixed.get("column"))
        if not cols:
            return None
        funcs = funcs or ["count"]
        if len(funcs) == 1:
            fixed["func"] = funcs[0]
        elif len(funcs) == len(cols):
            fixed["func"] = funcs
        else:
            fixed["func"] = funcs[0]
        return fixed
    if op == "sort":
        if not isinstance(fixed.get("ascending"), (bool, list)):
            fixed["ascending"] = True
        if fixed.get("na_position") not in ("first", "last"):
            fixed.pop("na_position", None)
        return fixed
    if op == "dedupe":
        if fixed.get("keep") not in DEDUPE_KEEP:
            fixed["keep"] = "first"
        return fixed
    if op == "join":
        if fixed.get("how") not in JOIN_HOWS:
            fixed["how"] = "inner"
        return fixed
    if op == "pct":
        return fixed if fixed.get("scope") in PCT_SCOPES else None
    if op == "rate":
        for key, allowed in (("numerator", ("hit", "miss")),
                             ("denominator", ("total", "hit", "miss"))):
            if key in fixed and str(fixed[key]).strip().lower() not in allowed:
                fixed.pop(key, None)
        return fixed
    if op == "cast":
        return fixed if fixed.get("to") in CAST_TYPES else None
    if op == "fill":
        if fixed.get("method") not in FILL_METHODS:
            return None
        if fixed.get("method") == "value" and "value" not in fixed:
            return None
        return fixed
    if op == "string":
        return fixed if fixed.get("action") in STRING_ACTIONS else None
    if op == "date_part":
        parts = [p for p in _as_list(fixed.get("parts")) if p in DATE_PARTS]
        if not parts:
            return None
        fixed["parts"] = parts
        return fixed
    if op == "bin":
        if fixed.get("method") not in (None,) + BIN_METHODS:
            fixed["method"] = "width"
        return fixed
    if op == "rank":
        if fixed.get("method") not in (None,) + RANK_METHODS:
            fixed["method"] = "average"
        return fixed
    if op in ("sample", "pivot", "union", "split", "concat", "assign", "select",
              "drop_columns", "rename", "derive", "head"):
        return fixed
    return fixed


def sanitize_rule(
    rule: Dict[str, Any], columns: List[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """把 LLM 输出的候选规则规整为可执行规则（白名单过滤 + 列引用剔除）。

    仅作用于「模型生成的候选规则」，**不放松** ``validate_rule`` 的严格校验；
    所有丢弃/修正项以 warnings 返回，便于在 Web 与执行摘要中审计。
    """
    out = dict(rule)
    warnings: List[str] = []
    declared = out.get("task_type")
    if not (isinstance(declared, str) and declared in TASK_TYPES):
        out.pop("task_type", None)
        if declared is not None:
            warnings.append(f"已忽略非法 task_type {declared!r}")

    raw_opts = out.get("options") if isinstance(out.get("options"), dict) else {}
    opts: Dict[str, Any] = {}
    for key, val in raw_opts.items():
        if key not in _ALLOWED_OPTION_KEYS:
            warnings.append(f"已忽略未知选项 options.{key}")
        elif key in BOOL_OPTIONS:
            if isinstance(val, bool):
                opts[key] = val
            else:
                warnings.append(f"options.{key} 类型非法，已忽略")
        elif key in ENUM_OPTIONS:
            if isinstance(val, str) and val in ENUM_OPTIONS[key]:
                opts[key] = val
            else:
                warnings.append(f"options.{key} 取值非法，已忽略")
        elif key in NUMERIC_OPTIONS:
            positive = key == "outlier_threshold"
            if (
                _is_number(val) and not isinstance(val, bool)
                and (float(val) > 0 if positive else float(val) >= 0)
            ):
                opts[key] = float(val) if key == "outlier_threshold" else int(float(val))
            else:
                warnings.append(f"options.{key} 取值非法，已忽略")
        elif key in STR_OPTIONS:
            if val is None or isinstance(val, str):
                opts[key] = val
            else:
                warnings.append(f"options.{key} 类型非法，已忽略")
        elif isinstance(val, list) and all(isinstance(x, str) for x in val):
            if key == "agg_funcs":
                val = [x for x in val if x in AGG_WHITELIST]
            opts[key] = val
        else:
            warnings.append(f"options.{key} 类型非法，已忽略")
    pivot_keys = ("pivot_index", "pivot_columns", "pivot_values")
    if any(opts.get(k) for k in pivot_keys) and not all(opts.get(k) for k in pivot_keys):
        for key in pivot_keys:
            opts.pop(key, None)
        warnings.append("透视表三要素不完整，已忽略透视配置")
    out["options"] = opts

    kept: List[Dict[str, Any]] = []
    known_chain: List[str] = list(columns)
    for idx, step in enumerate(out.get("steps") or []):
        tag = f"steps[{idx}]"
        if not isinstance(step, dict) or get_op(str(step.get("op"))) is None:
            warnings.append(f"{tag} 算子不在注册表白名单，已剔除")
            continue
        fixed = _fix_step(step, columns)
        if fixed is None:
            warnings.append(f"{tag}({step.get('op')}) 参数不合法，已剔除")
            continue
        step_errors: List[str] = []
        _check_steps([fixed], known_chain, step_errors)
        if step_errors:
            warnings.append(
                f"{tag}({fixed.get('op')}) 校验未通过，已剔除：{step_errors[:2]}"
            )
            continue
        kept.append(fixed)
        known_chain += _step_outputs(fixed)
    out["steps"] = kept

    output = out.get("output") if isinstance(out.get("output"), dict) else {}
    fmts = [
        f for f in (output.get("formats") or [])
        if isinstance(f, str) and f in EXPORT_WHITELIST
    ]
    out["output"] = {
        "formats": fmts or list(DEFAULT_OUTPUT["formats"]),
        "report": bool(output.get("report", True)),
    }
    out, col_warnings = reconcile_rule_columns(out, columns)
    warnings.extend(col_warnings)
    return out, warnings


def validate_rule(
    rule: Dict[str, Any], columns: Optional[List[str]] = None
) -> List[str]:
    """返回错误列表（空列表表示通过）。不抛异常，便于 Web 行内展示。"""
    errors: List[str] = []
    if not isinstance(rule, dict):
        return ["规则必须是 JSON 对象"]
    if rule.get("version") != RULE_VERSION:
        errors.append(f"version 必须为 {RULE_VERSION!r}")
    if not isinstance(rule.get("intent"), str):
        errors.append("intent 必须是字符串")
    task_type = rule.get("task_type")
    if task_type is not None and (not isinstance(task_type, str) or task_type not in TASK_TYPES):
        errors.append(f"task_type 非法，可选 {list(TASK_TYPES)}")
    sources = rule.get("sources")
    if sources is not None:
        _check_multi_sources(sources, errors)
        if isinstance(sources, dict) and isinstance(sources.get("main"), dict):
            _check_source(sources["main"], errors)
        else:
            _check_source(rule.get("source", {}), errors)
    else:
        _check_source(rule.get("source", {}), errors)
    _check_options(rule.get("options", {}), errors)
    _check_steps(rule.get("steps", []), columns or [], errors)
    _check_output(rule.get("output", {}), errors)
    return errors


def assert_valid_rule(rule: Dict[str, Any], columns: Optional[List[str]] = None) -> None:
    errors = validate_rule(rule, columns)
    if errors:
        raise RuleValidationError(errors)


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


# ------------------------------------------------------------
# 结构化意图 Schema 路径（v1.0，真实 LLM 调用，不依赖中文正则）
# ------------------------------------------------------------
def generate_rules_from_schema(
    intent: str,
    source: Dict[str, Any],
    columns: Optional[List[str]] = None,
    provider: Optional[LLMProvider] = None,
    sources: Optional[Dict[str, Any]] = None,
    source_columns: Optional[Dict[str, List[str]]] = None,
    column_aliases: Any = None,
    max_attempts: int = 2,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """描述 → 真实 LLM 解析结构化意图 → 确定性编译 → 规则校验。

    与 ``generate_rules`` 的区别：意图理解由 LLM 填 Schema 完成，规则步骤不再
    由模型直接输出，也不再经过中文正则 ``apply_intent_plan``。Schema 解析失败
    时抛出 ``LLMError``（不静默降级到关键词猜测），调用方可回退到旧链路。
    """
    if not columns:
        try:
            columns = probe_columns(source)
        except Exception:  # noqa: BLE001
            columns = []
    columns = [str(c) for c in columns]
    p = provider or get_provider()
    info = describe_provider(p)
    alias_map, alias_warnings = parse_column_aliases(column_aliases, columns)
    schema, schema_meta = parse_intent_schema(
        intent, p, columns, alias_map, sources, source_columns, max_attempts=max_attempts
    )
    if schema is None:
        raise LLMError(
            "结构化意图解析失败：" + "；".join(schema_meta.get("errors", [])[:5]),
            "parse",
            codes=schema_meta.get("error_codes") or [],
        )
    compiled = compile_intent_schema(schema)
    # 只校验步骤契约：完整 rule 尚未组装，validate_rule 会误报 source/version 缺失
    errors = _validate_steps(compiled.steps, columns)
    if errors:
        raise RuleValidationError(errors)
    rule: Dict[str, Any] = {
        "version": RULE_VERSION,
        "intent": intent,
        "source": dict(source),
        "options": {"fill_missing": "none", "outlier_method": "none"},
        "task_type": compiled.task_type,
        "steps": compiled.steps,
        # 编译层据此从 Schema 字段读取意图，避免对已结构化的规则再做中文正则推断。
        "intent_schema": schema.to_dict(),
        "output": {"formats": list(DEFAULT_OUTPUT["formats"]), "report": True},
    }
    if sources:
        cleaned = {str(k): dict(v) for k, v in sources.items() if isinstance(v, dict)}
        cleaned.setdefault("main", dict(source))
        rule["sources"] = cleaned
    issues = evaluate_rule(rule, intent, columns, alias_map)
    meta: Dict[str, Any] = {
        "provider": info.name,
        "mode": info.mode,
        "model": info.model,
        "reason": info.reason,
        "degraded": False,
        "generation_path": "intent_schema",
        "intent_schema": schema_meta,
        "schema": schema.to_dict(),
        "compile_notes": compiled.notes,
        "semantic_issues": format_issues(issues),
        "llm_attempts": int(schema_meta.get("attempts", 0)),
        "elapsed": float(schema_meta.get("elapsed", 0.0)),
    }
    if alias_map:
        meta["column_aliases"] = dict(alias_map)
        rule["column_aliases"] = dict(alias_map)
    if alias_warnings:
        meta["alias_warnings"] = alias_warnings
    LOGGER.info("结构化意图生成：%d 步，语义问题 %d，模型调用 %d 次",
                len(compiled.steps), len(issues), meta["llm_attempts"])
    return rule, meta


# ------------------------------------------------------------
# 自然语言 -> 规则
# ------------------------------------------------------------
def generate_rules_legacy(
    intent: str,
    source: Dict[str, Any],
    columns: Optional[List[str]] = None,
    provider: Optional[LLMProvider] = None,
    sources: Optional[Dict[str, Any]] = None,
    source_columns: Optional[Dict[str, List[str]]] = None,
    column_aliases: Any = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """兼容链路：模型直接输出规则 JSON + 中文正则意图编译。

    主路径已迁移到 :func:`generate_rules_from_schema`。本函数仅用于显式指定
    ``generation_path='legacy'`` 的诊断/回归场景，不再作为默认入口。
    """
    if not columns:
        try:
            columns = probe_columns(source)
        except Exception:  # noqa: BLE001 - 探测失败不影响规则生成
            columns = []
    # 主表列单独给生成器（否则关联键会误选维表列），校验时再合并全部源
    main_columns = [str(c) for c in columns]
    columns = list(dict.fromkeys(
        main_columns
        + [str(c) for cols in (source_columns or {}).values() for c in cols]
    ))
    alias_map, alias_warnings = parse_column_aliases(column_aliases, columns)
    started = time.monotonic()
    p = provider or get_provider()
    info = describe_provider(p)
    meta: Dict[str, Any] = {
        "provider": info.name,
        "mode": info.mode,
        "model": info.model,
        "reason": info.reason,
        "degraded": False,
        "elapsed": 0.0,
    }
    if alias_map:
        meta["column_aliases"] = dict(alias_map)
    if alias_warnings:
        meta["alias_warnings"] = alias_warnings
    system, user = build_rule_prompt(
        intent, source, main_columns, sources=sources,
        source_columns=source_columns, column_aliases=alias_map,
    )
    raw = ""
    attempts = 0
    repair_rounds = 0
    meta["prompt_family"] = primary_family(intent, main_columns, alias_map)
    try:
        raw = p.complete(system, user)
        attempts = 1
        try:
            candidate = parse_json_response(raw)
        except LLMError as parse_exc:
            if parse_exc.kind != "parse":
                raise
            # 模型偶尔输出尾逗号/注释/单引号等噪声；先用错误原文回灌一次，
            # 再让解析器做结构化清洗，避免直接把可修复的响应判为失败。
            LOGGER.warning("规则响应 JSON 解析失败，回灌重试：%s", str(parse_exc)[:160])
            repair_payload = json.loads(user)
            repair_payload["instruction"] = (
                "上次响应不是合法 JSON（" + str(parse_exc)[:180] + "）。"
                "请只输出一个合法 JSON 对象：键和字符串值必须双引号，不能有尾随逗号、"
                "注释或解释文字，不要代码围栏。"
            )
            raw = p.complete(system, json.dumps(repair_payload, ensure_ascii=False))
            attempts += 1
            candidate = parse_json_response(raw)
            meta["json_repair"] = True
        rule = normalize_rule(candidate, source, intent, sources=sources)
        errors = validate_rule(rule, columns)
        if errors:
            # 在线模型常臆造列名或附带多余字段：先白名单规整，尽量保留在线结果
            LOGGER.warning("在线规则首次校验未通过，尝试白名单规整：%s", errors[:3])
            sanitized, sanitize_warnings = sanitize_rule(rule, columns)
            errors = validate_rule(sanitized, columns)
            if errors:
                raise RuleValidationError(errors)
            rule = sanitized
            meta["reconcile_warnings"] = sanitize_warnings
            LOGGER.info("在线规则规整后通过校验，剔除项=%d", len(sanitize_warnings))

        # 语义自检：结构合法不代表回答了问题。发现问题就把 issue code 回灌模型
        # 定点修复（最多两轮），始终保留问题更少的一版，避免「修一个坏一个」。
        issues = evaluate_rule(rule, intent, columns, alias_map)
        if issues:
            LOGGER.warning("规则语义自检未通过（%d 项）：%s", len(issues), format_issues(issues)[:3])
            repair_user = build_repair_user_payload(
                json.loads(user), rule, format_issues(issues)
            )
            for _ in range(2):
                repaired_raw = p.complete(system, repair_user)
                attempts += 1
                repair_rounds += 1
                repaired = normalize_rule(
                    parse_json_response(repaired_raw), source, intent, sources=sources
                )
                repair_errors = validate_rule(repaired, columns)
                if repair_errors:
                    sanitized2, warnings2 = sanitize_rule(repaired, columns)
                    if validate_rule(sanitized2, columns):
                        continue
                    repaired = sanitized2
                    meta.setdefault("reconcile_warnings", []).extend(warnings2)
                repaired_issues = evaluate_rule(repaired, intent, columns, alias_map)
                if len(repaired_issues) < len(issues):
                    rule = repaired
                    issues = repaired_issues
                if not issues:
                    break
                repair_user = build_repair_user_payload(
                    json.loads(user), rule, format_issues(issues)
                )
            # 日期条件被塞进 filter.conditions 时，校验会整段剔除，模型两轮修复
            # 也难以恢复；此处做一次纯结构提升，不引入任何业务猜测。
            codes = {i.code for i in issues}
            if codes & {
                "date_filter_nested", "missing_date_filter", "date_in_filter",
                "date_range_in_filter", "date_eq_year",
            }:
                structurally_fixed, changed = repair_date_steps(rule, intent)
                if changed and not validate_rule(structurally_fixed, columns):
                    fixed_issues = evaluate_rule(structurally_fixed, intent, columns, alias_map)
                    if len(fixed_issues) < len(issues):
                        rule = structurally_fixed
                        issues = fixed_issues
                        meta.setdefault("reconcile_warnings", []).append(
                            "已将嵌套/错写的日期条件提升为独立 date_filter 步骤"
                        )
            meta["semantic_issues"] = format_issues(issues)
            if issues:
                LOGGER.warning("语义修复后仍有 %d 项问题，已保留最佳版本", len(issues))
        meta["llm_attempts"] = attempts
        meta["repair_rounds"] = repair_rounds
        LOGGER.info("规则生成完成 provider=%s 算子数=%d 语义问题=%d",
                    info.name, len(rule["steps"]), len(issues))
    except (LLMError, RuleValidationError, ValueError, TypeError) as exc:
        # 不再降级到关键词模板：猜出来的规则曾导致列绑定错误与任务类型误判。
        # 失败时明确报错，由调用方选择重试、改用在线模型或提供显式规则 JSON。
        detail = str(exc).strip()[:300] or type(exc).__name__
        LOGGER.error("规则生成失败 provider=%s：%s | %s", info.name, type(exc).__name__, detail)
        meta["validation_errors"] = getattr(exc, "errors", [])
        meta["reason"] = detail
        raise LLMError(
            f"规则生成失败（{info.name}）：{detail}；"
            "如需离线运行，请使用 --rule-file 提供显式规则 JSON",
            getattr(exc, "kind", "generation"),
        ) from exc
    # 兼容旧规则里残留的模板字段；新链路不再产生这些字段
    missing = [
        str(x) for x in (rule.pop("unresolved_subjects", None) or []) if str(x).strip()
    ]
    if missing:
        meta.setdefault("alias_warnings", []).extend(
            f"需求中的「{name}」未匹配到同名列，请在列名映射中指定对应列"
            for name in missing
        )
    if alias_map:
        # 保留使用者原始映射，便于结果页/规则回看时追溯「中文描述 → 真实列名」
        rule["column_aliases"] = dict(alias_map)
    else:
        rule.pop("column_aliases", None)

    # 确定性意图编译只做「守门/兜底」，不再无条件替换。实测比率类描述会让同一个
    # 模型产出四种互不相同的规则，其中三种口径错误；但确定性编译器也不可能枚举
    # 全部业务原语，若模型已经答对却强制替换，反而会丢掉合法语义。策略是：
    #   1. 模型规则语义自检通过 → 原样保留；
    #   2. 模型规则有问题、编译候选无问题（或问题更少）→ 采用编译候选；
    #   3. 编译候选结构非法、问题更多或没有收益 → 保留当前最佳模型规则。
    value_hints = probe_value_hints(source, main_columns)
    planned, plan_warnings, plan_info = apply_intent_plan(
        rule, intent, columns, alias_map, value_hints=value_hints
    )
    pre_plan_issues = list(issues)
    plan_meta: Dict[str, Any] = {
        "applied": False, "decision": "not_applicable", "candidate_found": False,
    }
    if plan_info.get("applied"):
        plan_meta = dict(plan_info)
        plan_meta["candidate_found"] = True
        plan_meta["pre_plan_issue_count"] = len(pre_plan_issues)
        errors = validate_rule(planned, columns)
        if errors:
            plan_meta["decision"] = "rejected_invalid_plan"
            plan_meta["reason"] = "；".join(errors[:3])
            LOGGER.warning("意图编译候选未通过校验，保留模型规则：%s", errors[:3])
            meta.setdefault("reconcile_warnings", []).append(
                "意图编译候选校验未通过，已保留模型规则：" + "；".join(errors[:2])
            )
        else:
            planned_issues = evaluate_rule(planned, intent, columns, alias_map)
            plan_meta["planned_issue_count"] = len(planned_issues)
            if not pre_plan_issues:
                if planned_issues:
                    decision = "rejected_would_break_correct_llm"
                    reason = "模型规则已通过语义自检，确定性候选反而引入问题"
                else:
                    decision = "preserved_correct_llm"
                    reason = "模型规则已通过语义自检，保留原始算子链以覆盖编译器未枚举的语义"
            else:
                # 阻塞性错误码由评测器统一声明（单一事实源），避免此处散落
                # 字符串与 Issue.code 漂移；新增阻塞码只需改 rule_eval.BLOCKING_CODES。
                blocking = BLOCKING_CODES
                pre_codes = {item.code for item in pre_plan_issues}
                plan_codes = {item.code for item in planned_issues}
                resolved_blocking = (pre_codes & blocking) - plan_codes
                if not planned_issues or len(planned_issues) < len(pre_plan_issues) or resolved_blocking:
                    decision = "compiled"
                    reason = (
                        "确定性候选解决了阻塞性语义问题：" + "、".join(sorted(resolved_blocking))
                        if resolved_blocking else "确定性候选语义问题更少或无问题"
                    )
                else:
                    decision = "rejected_no_gain"
                    reason = "确定性候选未减少语义问题"
            plan_meta["decision"] = decision
            plan_meta["reason"] = reason
            if decision == "compiled":
                rule = planned
                if pre_plan_issues:
                    meta["pre_plan_semantic_issues"] = format_issues(pre_plan_issues)
                meta["semantic_issues"] = format_issues(planned_issues)
                meta.setdefault("reconcile_warnings", []).extend(plan_warnings)
                LOGGER.info(
                    "意图编译生效：%s，替换 %d 个上游步骤",
                    plan_info["spec"].get("alias") or "明细/清洗",
                    plan_info["replaced_steps"],
                )
            else:
                if decision in {"rejected_would_break_correct_llm", "rejected_no_gain"}:
                    meta.setdefault("reconcile_warnings", []).append(
                        "意图编译候选未采用：" + reason
                    )
                LOGGER.info("意图编译未采用：%s", reason)
    plan_meta["applied"] = plan_meta["decision"] in {"compiled"}
    meta["intent_plan"] = plan_meta
    meta.setdefault("semantic_issues", format_issues(issues))

    meta["elapsed"] = round(time.monotonic() - started, 3)
    return rule, meta


def generate_rules(
    intent: str,
    source: Dict[str, Any],
    columns: Optional[List[str]] = None,
    provider: Optional[LLMProvider] = None,
    sources: Optional[Dict[str, Any]] = None,
    source_columns: Optional[Dict[str, List[str]]] = None,
    column_aliases: Any = None,
    generation_path: str = "schema",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """描述 → 规则的主入口。

    默认走结构化意图 Schema（真实 LLM 解析 → 结构校验 → 确定性编译 → 语义复评），
    规则中不依赖中文正则猜测业务口径。``generation_path='legacy'`` 仅供回归诊断，
    对应旧版“模型直接输出规则 JSON + 正则意图编译”链路。
    """
    if generation_path == "schema":
        return generate_rules_from_schema(
            intent, source, columns, provider=provider, sources=sources,
            source_columns=source_columns, column_aliases=column_aliases,
        )
    if generation_path == "legacy":
        return generate_rules_legacy(
            intent, source, columns, provider=provider, sources=sources,
            source_columns=source_columns, column_aliases=column_aliases,
        )
    raise ValueError(f"未知 generation_path: {generation_path!r}；可选 schema/legacy")


def generate_rules_from_dict(
    payload: Dict[str, Any], provider: Optional[LLMProvider] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """从 Web/CLI 传入的字典生成规则（intent/source/columns/column_aliases）。

    columns 缺省时自动探测数据源真实列名，避免 LLM 臆造列引用；
    column_aliases 为使用者提供的「中文描述 → 真实列名」映射（dict 或多行文本）。
    """
    intent = str(payload.get("intent", "")).strip()
    if not intent:
        raise RuleValidationError(["自然语言需求描述不能为空"])
    sources = payload.get("sources")
    if sources is not None and not isinstance(sources, dict):
        raise RuleValidationError(["sources 必须是对象"])
    source = payload.get("source") or {}
    if not isinstance(source, dict):
        raise RuleValidationError(["source 必须是对象"])
    if not source and isinstance(sources, dict):
        source = sources.get("main") or {}
    if not isinstance(source, dict) or not source:
        raise RuleValidationError(["缺少主数据源"])
    columns = [str(c) for c in payload.get("columns", [])]
    if not columns:
        columns = probe_columns(source)
    source_columns = {
        str(alias): probe_columns(src)
        for alias, src in (sources or {}).items()
        if isinstance(src, dict)
    }
    return generate_rules(
        intent, source, columns, provider,
        sources=sources, source_columns=source_columns,
        column_aliases=payload.get("column_aliases"),
        generation_path=str(payload.get("generation_path") or "schema"),
    )


# ------------------------------------------------------------
# 规则 -> PipelineConfig
# ------------------------------------------------------------
def rule_to_config(rule: Dict[str, Any], output_dir: str = "./output") -> Any:
    """把规则 options/source 映射为现有 PipelineConfig。

    延迟导入避免 etl_pipeline 与新层形成循环依赖；仅传入白名单字段。
    """
    from etl_pipeline import PipelineConfig

    src = rule.get("source", {})
    opts = rule.get("options", {})
    kwargs: Dict[str, Any] = {
        "input_path": str(src.get("path", "")),
        "input_format": str(src.get("format", "csv")),
        "output_dir": output_dir,
    }
    for key in BOOL_OPTIONS + tuple(ENUM_OPTIONS) + NUMERIC_OPTIONS + STR_OPTIONS + LIST_OPTIONS:
        if key in opts and opts[key] is not None:
            kwargs[key] = tuple(opts[key]) if key in LIST_OPTIONS else opts[key]
    # source 中的读取细节
    for key in ("sqlite_table", "sqlite_query", "excel_sheet", "encoding"):
        if src.get(key):
            kwargs[key] = src[key]
    return PipelineConfig(**kwargs)


def probe_value_hints(
    source: Dict[str, Any], columns: Optional[List[str]] = None,
    rows: int = 60, per_column: int = 24,
) -> Dict[str, List[str]]:
    """采样每列的少量真实取值，供意图层把描述中的裸词绑定到唯一列。

    例如「提取鼓楼区数据」没有写「区县为」，但采样值里只有「区县」列出现
    「鼓楼区」，即可确定性绑定；出现多列命中时放弃绑定，绝不猜测。
    读取失败时返回空字典，不影响规则生成。
    """
    hints: Dict[str, List[str]] = {}
    try:
        import pandas as pd
        from etl_pipeline import PipelineConfig

        # 与执行层共用路径守卫：统一解析/校验口径，避免探测与执行对同一路径
        # 得出不同结论。allowed_root 由调用方按需传入，本地工具默认不限制目录。
        from ops.guards import validate_source_path
        validated = validate_source_path(source)
        source = {**source, "path": str(validated)}

        cfg = PipelineConfig(
            input_path=str(source.get("path", "")),
            input_format=str(source.get("format", "csv")),
            sqlite_table=str(source.get("sqlite_table", "data") or "data"),
            sqlite_query=source.get("sqlite_query"),
            excel_sheet=source.get("excel_sheet"),
            encoding=str(source.get("encoding", "utf-8") or "utf-8"),
        )
        fmt = cfg.input_format
        if fmt == "csv":
            df = pd.read_csv(cfg.input_path, nrows=rows, encoding=cfg.encoding)
        elif fmt == "excel":
            df = pd.read_excel(cfg.input_path, nrows=rows,
                               sheet_name=cfg.excel_sheet if cfg.excel_sheet else 0,
                               engine="openpyxl")
        elif fmt == "json":
            df = pd.read_json(cfg.input_path, encoding=cfg.encoding).head(rows)
        else:
            import sqlite3
            query = cfg.sqlite_query or f"SELECT * FROM {cfg.sqlite_table} LIMIT {rows}"
            with sqlite3.connect(cfg.input_path) as conn:
                df = pd.read_sql_query(query, conn).head(rows)
        wanted = set(columns or [str(c) for c in df.columns])
        for col in df.columns:
            name = str(col)
            if name not in wanted:
                continue
            values = df[col].dropna().astype(str).str.strip().unique().tolist()
            values = [v for v in values if v and v.lower() != "nan"][:per_column]
            if values:
                hints[name] = values
    except Exception:  # noqa: BLE001 - 采样只增强解析，不阻塞生成
        return {}
    return hints


def probe_columns(source: Dict[str, Any], limit: int = 200) -> List[str]:
    """轻量探测数据源列名，供规则校验与 Web 展示（只读表头，不载入全量）。"""
    try:
        import pandas as pd
        from etl_pipeline import PipelineConfig

        # 与执行层共用路径守卫，统一路径解析口径；失败时下面的兜底返回空列表。
        from ops.guards import validate_source_path
        validated = validate_source_path(source)
        source = {**source, "path": str(validated)}

        cfg = PipelineConfig(
            input_path=str(source.get("path", "")),
            input_format=str(source.get("format", "csv")),
            sqlite_table=str(source.get("sqlite_table", "data") or "data"),
            sqlite_query=source.get("sqlite_query"),
            excel_sheet=source.get("excel_sheet"),
            encoding=str(source.get("encoding", "utf-8") or "utf-8"),
        )
        fmt = cfg.input_format
        if fmt == "csv":
            df = pd.read_csv(cfg.input_path, nrows=5, encoding=cfg.encoding)
        elif fmt == "excel":
            df = pd.read_excel(
                cfg.input_path, nrows=5,
                sheet_name=cfg.excel_sheet if cfg.excel_sheet else 0,
                engine="openpyxl",
            )
        elif fmt == "json":
            df = pd.read_json(cfg.input_path, nrows=5, encoding=cfg.encoding)
        else:
            import sqlite3
            query = cfg.sqlite_query or f"SELECT * FROM {cfg.sqlite_table} LIMIT 5"
            with sqlite3.connect(cfg.input_path) as conn:
                df = pd.read_sql_query(query, conn)
        return [str(c) for c in df.columns][:limit]
    except Exception as exc:  # noqa: BLE001 - 探测失败不应阻断规则生成
        LOGGER.warning("列名探测失败（将跳过列引用校验）：%s", type(exc).__name__)
        return []


def rule_summary(rule: Dict[str, Any]) -> Dict[str, Any]:
    """供 Web 概览展示的规则摘要。"""
    return {
        "version": rule.get("version"),
        "format": rule.get("source", {}).get("format"),
        "path": rule.get("source", {}).get("path"),
        "option_count": len(rule.get("options", {})),
        "step_count": len(rule.get("steps", [])),
        "ops": [s.get("op") for s in rule.get("steps", [])],
        "formats": rule.get("output", {}).get("formats", []),
        "report": rule.get("output", {}).get("report", True),
    }

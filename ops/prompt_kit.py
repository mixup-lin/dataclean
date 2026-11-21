# -*- coding: utf-8 -*-
"""提示词工程工具包。

目标：让模型「先分类、再填槽」，而不是自由组合算子。核心由四部分组成：

1. 任务路由表（intent → 唯一骨架 + task_type）
2. 按场景动态选取的少样本示例与负面示例
3. 真实列/真实取值的动态上下文（减少臆造列与错绑值）
4. 输出前自检清单 + 失败后的定点修复提示

与 ``ops.intent`` 的关系：本模块只提升**模型生成质量**；意图编译层仍是
最后的安全网。两者的口径保持一致，避免「修一个坏一个」。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ------------------------------------------------------------
# 任务族：触发词 → 唯一骨架 → 任务类型
# ------------------------------------------------------------
FAMILIES: Dict[str, Dict[str, Any]] = {
    "composite": {
        "title": "多步骤原语组合（按描述顺序自由编排，不绑定固定链路）",
        "trigger": (
            r"^(?=.*(?:去重|然后|接着|随后|最后|派生|公式|新增一列))"
            r"(?=.*(?:趋势|走势|分析|统计|汇总|占比|排名|透视|分组|条数|均值|求和|提取|筛选))"
        ),
        "task_type": None,
        "skeleton": {
            "steps": [
                {"op": "<按描述顺序选择原语>", "参数": "<见原语索引与已加载原语目录>"},
            ],
        },
        "anti": [
            "禁止只做前半段（去重/提取/清洗）而丢掉最后的分析目标；最终必须回答「最后要什么」。",
            "禁止把组合流程套进某一种固定顺序；steps 的顺序必须与描述的「然后/最后」一致。",
            "禁止因为数据里没有「季度列」就放弃按季度分析：用 date_part(parts=[\"year\",\"quarter\"], bucket=\"年季度\") 推导。",
            "禁止因为数据里没有公式列就忽略公式：用 expr 按 operands + expression 动态派生。",
        ],
        "check": [
            "描述里出现的每个动作都要有对应原语：去重→dedupe，提取/筛选→filter，",
            "  显式公式→expr，按季度/月/年→date_part，分析趋势/汇总→aggregate/count_rows。",
            "「A 或 B」类条件写进同一个 filter 的 conditions 并设 logic=or。",
        ],
    },
    "rate": {
        "title": "两类比率（成交率/未成交率/转化率/成功率/空值率/A与B之比）",
        "trigger": r"(成交率|未成交率|转化率|成功率|失败率|及格率|合格率|空值率|缺失率|两类之比|之比|命中率)",
        "task_type": "proportion",
        "skeleton": {
            "op": "rate", "column": "<判据列>",
            "condition": {"column": "<判据列>", "operator": "notna"},
            "true_label": "<命中类>", "false_label": "<未命中类>",
            "numerator": "hit|miss", "denominator": "total|hit|miss",
            "alias": "<目标率列名>",
        },
        "anti": [
            "禁止用 assign + count_rows 代替 rate：没有比率列，且填充缺失会抹掉两类差别。",
            "禁止把判据列的 isna/notna 写进 filter：分母会被截断成一类，比率恒为 0 或 100%。",
            "禁止 rate 之后再接 pct：rate 输出已是汇总表，不是逐行标签。",
        ],
        "check": [
            "目标率是「命中/总数」→ numerator=hit, denominator=total；「未命中/总数」→ miss/total；「未命中/命中」→ miss/hit。",
            "rate.condition 定义命中类；filter 只放需求明确给出的筛选，不放判空判据。",
        ],
    },
    "detail": {
        "title": "明细提取/清洗（提取、导出、列出、保留、查出、替换、置为）",
        "trigger": r"(提取|导出|列出|查出|筛选后输出|清洗|置为|置0|填0|填入|填为|替换为|填空|补零|补充为空|原样输出|保留明细|保留逐行)",
        "task_type": "detail",
        "skeleton": {
            "op": "filter", "conditions": [{"column": "<筛选列>", "operator": "eq", "value": "<值>"}], "logic": "and",
        },
        "anti": [
            "禁止出现 aggregate / count_rows / pivot / rate / pct / rank / group_rank / resample：结果必须是逐行明细。",
            "禁止写 task_type=summary；必须写 detail。",
            "「其他字段空值置为 null / 保留为空」时 options.fill_missing 必须写 \"none\"，不能写 median/zero/drop。",
        ],
        "check": [
            "「某列空值置为 0」→ steps.fill（method=value, value=0），不是 options.fill_missing。",
            "筛选用 steps.filter；只输出明细，不追加任何统计步骤。",
        ],
    },
    "count_group": {
        "title": "分组计数（每个 X 有几条/条数/数量）",
        "trigger": r"(每个[^，。；]{1,12}(有几条|多少条|的条数|数量|个数)|按[^，。；]{1,12}分组|分组统计|各组|各组别|分组计数|统计每个)",
        "task_type": "summary",
        "skeleton": {"op": "count_rows", "by": ["<分组列>"], "alias": "条数"},
        "anti": [
            "禁止只做筛选或只输出明细：最终表必须一行一个分组、一列是计数。",
            "禁止写成趋势（date_part + aggregate）或分布（bin + pct）；纯分组计数就是 summary。",
            "禁止为了筛选年份造 date_part 中间列：年份筛选用 date_filter。",
        ],
        "check": [
            "分组列来自需求里的「按 X / 每个 X」，必须是真实列。",
            "steps 必须包含 count_rows；需要对条数设门槛时追加 having（column 等于 count_rows.alias）。",
        ],
    },
    "date_filter": {
        "title": "日期筛选（某年/某季度/某月/区间/近 N 天）",
        "trigger": r"(结束时间|开始时间|日期|年份|某年|\d{4}年|季度|按月|区间|近\s*\d+\s*天|截止|之后|之前)",
        "task_type": None,
        "skeleton": {"op": "date_filter", "column": "<日期列>", "year": 2026},
        "anti": [
            "严禁对原始日期列用 eq/ne 搭配 4 位年份字符串：真实值是完整时间戳，全等恒假，结果 0 行。",
            "禁止为了筛选而先 date_part 造中间列；date_part 只用于真正按时间分组的趋势。",
        ],
        "check": [
            "「X 年在/某年」→ date_filter.year；「区间」→ start/end；「近 N 天」→ recent_days。",
            "日期筛选与业务筛选放进各自独立的步骤；不要把日期条件塞进 filter.eq。",
        ],
    },
    "proportion": {
        "title": "条件/构成/组内占比（不含两类比率）",
        "trigger": r"(占比|比例|百分比|构成|占全部|占总数的|累计占比)",
        "task_type": "proportion",
        "skeleton": {
            "op": "pct", "scope": "condition",
            "condition": {"column": "<列>", "operator": "notna"},
            "alias": "<占比列名>",
        },
        "anti": [
            "禁止只筛选不计算占比：需求要的是占比列，必须出现 pct。",
            "「两类之比/成交率/未成交率」属于 rate 族，不用 pct。",
        ],
        "check": [
            "条件占比 scope=condition；整体构成 scope=overall 且给 by；组内占比 scope=group 且给 by 与 within。",
        ],
    },
    "ranking": {
        "title": "排名/TopN（前三、最多、最少、排名）",
        "trigger": r"(排名|排行|top\s*\d+|前\s*\d+|最多的|最少的|最高的|最低的|降序|升序)",
        "task_type": "ranking",
        "skeleton": {"op": "rank", "by": "<数值列>", "into": "排名", "topn": 10},
        "anti": ["禁止只 sort 不 topn：TopN 需求最终结果必须只剩要求的行数。"],
        "check": ["先按真实指标聚合再排名；topn 与需求里的 N 一致。"],
    },
    "trend": {
        "title": "时间趋势（按月/逐年/走势/重采样）",
        "trigger": r"(趋势|走势|按月|逐月|每月|逐年|每年|月度|年度变化|重采样)",
        "task_type": "trend",
        "skeleton": {"op": "date_part", "column": "<日期列>", "parts": ["year", "month"]},
        "anti": ["「每个小区有几条」不是趋势；只有明确按时间维度分组才用 date_part/resample。"],
        "check": ["date_part 后接 count_rows/aggregate，分组列使用生成的时间列。"],
    },
    "pivot": {
        "title": "透视交叉表",
        "trigger": r"(透视|交叉表|矩阵表|pivot)",
        "task_type": "pivot",
        "skeleton": {"op": "pivot", "index": "<行维度>", "columns": "<列维度>", "values": "<数值列>", "aggfunc": "sum"},
        "anti": ["禁止用 aggregate 代替 pivot：结果必须是一张交叉表。"],
        "check": ["index/columns/values 都必须是真实列。"],
    },
    "dedupe": {
        "title": "去重（整行去重/按列去重/相似去重）",
        "trigger": r"(去重|重复行|去掉重复|相似重复|唯一值)",
        "task_type": "detail",
        "skeleton": {"op": "dedupe", "subset": ["<列>"], "keep": "first"},
        "anti": ["禁止用 drop_duplicates 全局选项代替有明确 subset 的 dedupe 步骤。"],
        "check": ["subset 列来自需求；没有指定列时留空表示整行去重。"],
    },
    "derive": {
        "title": "字段衍生/单位换算/文本规范化",
        "trigger": r"(换算|单位|万元|万元为单位|差值|差额|相差|同比增长|环比|规范化|标准化|拼接|拆分|新增一列|派生|公式|除以|乘以)",
        "task_type": "detail",
        "skeleton": {"op": "derive", "column": "<新列>", "operator": "ratio", "left": "<列>", "right": "<列或数字>"},
        "anti": [
            "禁止用 assign 做数值运算：数值计算用 derive/expr，分类打标才用 assign。",
            "禁止因为数据里没有公式列就忽略公式；显式公式必须用 expr 派生。",
        ],
        "check": [
            "简单二元运算用 derive；描述里给出「A = B/C」这类公式时用 expr：",
            "  expression 用 c0/c1 引用 operands（如 c0/c1），operands 写真实列名。",
        ],
    },
    "aggregate": {
        "title": "一般汇总统计（求和/均值/最大最小/分组指标）",
        "trigger": r"(求和|之和|总和|总额|平均值|均值|最大|最小|汇总|统计.*(金额|价格|面积|次数))",
        "task_type": "summary",
        "skeleton": {"op": "aggregate", "by": ["<分组列>"], "column": ["<数值列>"], "func": ["sum"], "alias": ["<结果列>"]},
        "anti": ["禁止只写 options.group_by：必须在 steps 里写 aggregate。"],
        "check": ["column/func/alias 三个数组长度一致；数值列必须真实存在。"],
    },
}

_FAMILY_ORDER = [
    "composite", "rate", "detail", "count_group", "date_filter", "proportion",
    "ranking", "trend", "pivot", "dedupe", "derive", "aggregate",
]

# 任务族 -> 本场景需要的算子（按需渲染算子目录，不把全量 53 个算子塞进提示词）
_FAMILY_CATALOG_OPS: Dict[str, List[str]] = {
    "rate": ["filter", "date_filter", "rate"],
    "detail": ["filter", "date_filter", "fill", "dedupe", "select", "cast", "string", "sort", "head"],
    "count_group": ["filter", "date_filter", "count_rows", "having", "sort", "aggregate"],
    "date_filter": ["filter", "date_filter", "count_rows"],
    "proportion": ["filter", "date_filter", "pct"],
    "ranking": ["filter", "date_filter", "count_rows", "aggregate", "sort", "rank", "head"],
    "trend": ["filter", "date_filter", "date_part", "aggregate", "count_rows", "resample", "sort"],
    "pivot": ["filter", "date_filter", "pivot"],
    "dedupe": ["dedupe", "dedupe_fuzzy", "filter"],
    "derive": ["derive", "expr", "convert_unit", "string", "text_normalize"],
    "aggregate": ["filter", "date_filter", "aggregate", "sort"],
    "composite": [
        "dedupe", "filter", "date_filter", "date_part", "expr", "derive",
        "fill", "aggregate", "count_rows", "sort", "head",
    ],
}


def select_ops(families: Sequence[str]) -> List[str]:
    """按命中任务族挑选算子目录条目；始终保留筛选与日期筛选两个基础设施。"""
    selected = ["filter", "date_filter"]
    for family in families:
        selected.extend(_FAMILY_CATALOG_OPS.get(family, []))
    return list(dict.fromkeys(selected))


# ------------------------------------------------------------
# Skill 风格的渐进式引用：索引（始终可见）→ 正文（按需展开）
# ------------------------------------------------------------
_FAMILY_DESCRIPTIONS: Dict[str, str] = {
    "composite": "多动作组合流程：按描述顺序把去重/筛选/派生/清洗/分析原语串成一条链",
    "rate": "两类比率：成交率、未成交率、转化率、空值率、A与B之比",
    "detail": "逐行明细：提取、导出、列出、置为、填充，结果保持逐行",
    "count_group": "分组计数：每个 X 有几条、按 X 分组统计条数",
    "date_filter": "日期筛选：某年、某季度、某月、区间、近 N 天",
    "proportion": "条件/构成/组内占比，不含两类比率",
    "ranking": "排名与 TopN：前 N、最多、最少、升降序",
    "trend": "时间趋势：按月/季度/年、走势、重采样",
    "pivot": "透视交叉表",
    "dedupe": "去重：整行或按指定列",
    "derive": "字段派生与单位换算",
    "aggregate": "一般汇总：求和、均值、极值、分组指标",
}


def build_skill_index(families: Sequence[str]) -> str:
    """先给轻量索引，再在下文只对命中的技能展开骨架、示例与算子正文。"""
    from ops.registry import get_op

    loaded = set(families)
    lines = [
        "【技能索引（渐进式引用）】",
        "使用方式：先看索引确定本需求要加载哪些技能，再只按下方「已加载」的正文与算子目录写规则；",
        "未标注「已加载」的技能在本需求中禁止使用，不要凭记忆补全它的算子。",
        "",
        "任务族技能：",
    ]
    for name in _FAMILY_ORDER:
        mark = "[已加载]" if name in loaded else ""
        lines.append(f"- {name}：{_FAMILY_DESCRIPTIONS.get(name, FAMILIES[name]['title'])} {mark}")
    lines.append("")
    lines.append("基础原语（组合流程可自由搭配，完整参数见下方「本需求可用算子目录」）：")
    for name in select_ops(families):
        spec = get_op(name)
        summary = (spec.summary or "") if spec else ""
        lines.append(f"- {name}：{summary}")
    lines.append("")
    lines.append("组合裁决：一句需求里出现多个动作时，按动作顺序各选一个原语；"
                 "没有对应原语的动作不允许悄悄丢掉。")
    return "\n".join(lines)


_REQUIRED_OPS: Dict[str, List[str]] = {
    "rate": ["rate"], "count_group": ["count_rows"], "date_filter": ["date_filter"],
    "proportion": ["pct"], "ranking": ["sort"], "trend": ["date_part"],
    "pivot": ["pivot"], "dedupe": ["dedupe"], "derive": ["derive"],
    "aggregate": ["aggregate"],
    # 组合流程不设必选算子：原语必须由描述决定，写死会造成新的场景错配。
    "composite": [],
}

# ------------------------------------------------------------
# 场景模板：示例中的列/值会按真实数据动态替换
# ------------------------------------------------------------
_EXAMPLES: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {
    "composite": [
        (
            "去重，然后提取出面积小于50或大于1000的数据，最后分析成交价的趋势",
            {
                "task_type": "trend",
                "steps": [
                    {"op": "dedupe", "subset": [], "keep": "first"},
                    {"op": "filter", "logic": "or", "conditions": [
                        {"column": "@面积列", "operator": "lt", "value": 50},
                        {"column": "@面积列", "operator": "gt", "value": 1000},
                    ]},
                    {"op": "date_part", "column": "@日期列", "parts": ["year", "month"],
                     "bucket": "年月"},
                    {"op": "aggregate", "by": ["@日期列_年月"], "column": ["@判空列"],
                     "func": ["mean"], "alias": ["成交价"]},
                    {"op": "sort", "by": ["@日期列_年月"], "ascending": [True]},
                ],
            },
        ),
        (
            "去重，然后提取出面积小于50或大于1000的数据，最后按结束时间按季度分析成交单价的趋势，"
            "成交单价 = （成交价/面积）",
            {
                "task_type": "trend",
                "steps": [
                    {"op": "dedupe", "subset": [], "keep": "first"},
                    {"op": "filter", "logic": "or", "conditions": [
                        {"column": "@面积列", "operator": "lt", "value": 50},
                        {"column": "@面积列", "operator": "gt", "value": 1000},
                    ]},
                    {"op": "expr", "column": "成交单价", "expression": "c0/c1",
                     "operands": ["@判空列", "@面积列"]},
                    {"op": "date_part", "column": "@日期列", "parts": ["year", "quarter"],
                     "bucket": "年季度"},
                    {"op": "aggregate", "by": ["@日期列_年季度"],
                     "column": ["成交单价", "@判空列", "@面积列"],
                     "func": ["mean", "sum", "sum"],
                     "alias": ["成交单价·逐条均值", "@判空列之和", "@面积列之和"]},
                    {"op": "expr", "column": "成交单价", "expression": "c0/c1",
                     "operands": ["@判空列之和", "@面积列之和"]},
                    {"op": "sort", "by": ["@日期列_年季度"], "ascending": [True]},
                ],
            },
        ),
    ],
    "rate": [
        (
            "筛选：区县：鼓楼区 判断：成交价为空-未成交，成交价不为空-成交 目标：统计成交率（成交/总数）",
            {
                "task_type": "proportion",
                "steps": [
                    {"op": "filter", "conditions": [{"column": "@区县", "operator": "eq", "value": "@区县值"}], "logic": "and"},
                    {"op": "rate", "column": "@判空列", "condition": {"column": "@判空列", "operator": "notna"},
                     "true_label": "成交", "false_label": "未成交", "numerator": "hit", "denominator": "total", "alias": "成交率"},
                ],
            },
        ),
        (
            "筛选：市：福州 判断：成交价为空-未成交，不为空-成交 目标：统计未成交率（未成交/总数）",
            {
                "task_type": "proportion",
                "steps": [
                    {"op": "filter", "conditions": [{"column": "@市", "operator": "eq", "value": "@市值"}], "logic": "and"},
                    {"op": "rate", "column": "@判空列", "condition": {"column": "@判空列", "operator": "notna"},
                     "true_label": "成交", "false_label": "未成交", "numerator": "miss", "denominator": "total", "alias": "未成交率"},
                ],
            },
        ),
        (
            "统计成交价为空与不为空的数据之比（未成交/成交）",
            {
                "task_type": "proportion",
                "steps": [
                    {"op": "rate", "column": "@判空列", "condition": {"column": "@判空列", "operator": "notna"},
                     "true_label": "成交", "false_label": "未成交", "numerator": "miss", "denominator": "hit", "alias": "未成交/成交"},
                ],
            },
        ),
        (
            "成交率（成交/未成交）",
            {
                "task_type": "proportion",
                "steps": [
                    {"op": "rate", "column": "@判空列", "condition": {"column": "@判空列", "operator": "notna"},
                     "true_label": "成交", "false_label": "未成交", "numerator": "hit", "denominator": "miss", "alias": "成交率"},
                ],
            },
        ),
    ],
    "detail": [
        (
            "提取区县为鼓楼区的数据，其中成交价空值置为0，其他字段空值置为null",
            {
                "task_type": "detail", "options": {"fill_missing": "none"},
                "steps": [
                    {"op": "filter", "conditions": [{"column": "@区县", "operator": "eq", "value": "@区县值"}], "logic": "and"},
                    {"op": "fill", "columns": ["@判空列"], "method": "value", "value": 0},
                ],
            },
        ),
        (
            "列出市为福州、结束时间在2026年的数据，不要做任何统计",
            {
                "task_type": "detail",
                "steps": [
                    {"op": "filter", "conditions": [{"column": "@市", "operator": "eq", "value": "@市值"}], "logic": "and"},
                    {"op": "date_filter", "column": "@日期列", "year": 2026},
                ],
            },
        ),
    ],
    "count_group": [
        (
            "筛选市是福州，结束时间在2026年，小区不为空；按小区分组，统计每个小区有几条数据",
            {
                "task_type": "summary",
                "steps": [
                    {"op": "filter", "conditions": [
                        {"column": "@市", "operator": "eq", "value": "@市值"},
                        {"column": "@分组列", "operator": "notna"},
                    ], "logic": "and"},
                    {"op": "date_filter", "column": "@日期列", "year": 2026},
                    {"op": "count_rows", "by": ["@分组列"], "alias": "条数"},
                ],
            },
        ),
        (
            "统计每个区县的数据条数，只要条数大于等于10的分组",
            {
                "task_type": "summary",
                "steps": [
                    {"op": "count_rows", "by": ["@区县"], "alias": "条数"},
                    {"op": "having", "column": "条数", "operator": "gte", "value": 10},
                ],
            },
        ),
    ],
    "date_filter": [
        (
            "统计结束时间在2026年的数据条数",
            {
                "task_type": "summary",
                "steps": [
                    {"op": "date_filter", "column": "@日期列", "year": 2026},
                    {"op": "count_rows", "alias": "条数"},
                ],
            },
        ),
    ],
    "proportion": [
        (
            "筛选市是福州，统计所在楼层和总层数都不为空的数据占总数的比例",
            {
                "task_type": "proportion",
                "steps": [
                    {"op": "filter", "conditions": [{"column": "@市", "operator": "eq", "value": "@市值"}], "logic": "and"},
                    {"op": "pct", "scope": "condition", "condition": {"conditions": [
                        {"column": "@楼层列A", "operator": "notna"},
                        {"column": "@楼层列B", "operator": "notna"},
                    ], "logic": "and"}, "alias": "非空占比"},
                ],
            },
        ),
    ],
    "ranking": [
        (
            "按区县统计条数，取条数最多的前5个区县",
            {
                "task_type": "ranking",
                "steps": [
                    {"op": "count_rows", "by": ["@区县"], "alias": "条数"},
                    {"op": "sort", "by": ["条数"], "ascending": [False]},
                    {"op": "head", "n": 5},
                ],
            },
        ),
    ],
    "trend": [
        (
            "统计2026年每月的成交数变化趋势",
            {
                "task_type": "trend",
                "steps": [
                    {"op": "date_part", "column": "@日期列", "parts": ["year", "month"]},
                    {"op": "count_rows", "by": ["@日期列_月"], "alias": "条数"},
                ],
            },
        ),
    ],
    "pivot": [
        (
            "按区县和物业类型做透视，统计成交价之和",
            {
                "task_type": "pivot",
                "steps": [{"op": "pivot", "index": "@区县", "columns": "@物业类型", "values": "@金额列", "aggfunc": "sum"}],
            },
        ),
    ],
    "dedupe": [
        (
            "按标的号去重，保留第一条",
            {"task_type": "detail", "steps": [{"op": "dedupe", "subset": ["@标的号"], "keep": "first"}]},
        ),
    ],
    "derive": [
        (
            "新增一列成交折扣，等于成交价除以评估价",
            {"task_type": "detail", "steps": [
                {"op": "derive", "column": "成交折扣", "operator": "ratio", "left": "@判空列", "right": "@评估价列"},
            ]},
        ),
        (
            "新增一列成交单价，成交单价 = 成交价/面积",
            {"task_type": "detail", "steps": [
                {"op": "expr", "column": "成交单价", "expression": "c0/c1",
                 "operands": ["@判空列", "@面积列"]},
            ]},
        ),
    ],
    "aggregate": [
        (
            "按区县汇总成交价总和",
            {"task_type": "summary", "steps": [
                {"op": "aggregate", "by": ["@区县"], "column": ["@判空列"], "func": ["sum"], "alias": ["成交总额"]},
            ]},
        ),
    ],
}

# 占位符 → 候选真实列（按优先级取第一个存在的）
_SLOTS: Dict[str, Sequence[str]] = {
    "@区县": ("区县", "区县名称", "区域", "地区"),
    "@市": ("市", "城市", "地区"),
    "@日期列": ("结束时间", "开始时间", "成交时间", "日期"),
    "@判空列": ("成交价(元)", "成交价", "成交金额"),
    "@评估价列": ("评估价(元)", "评估价", "评估金额"),
    "@金额列": ("成交价(元)", "成交价", "金额"),
    "@面积列": ("面积(㎡)", "面积", "建筑面积"),
    "@分组列": ("小区", "标的号", "街道/镇", "区县"),
    "@物业类型": ("物业类型", "类型", "类别"),
    "@标的号": ("标的号", "编号", "id"),
    "@楼层列A": ("所在楼层", "楼层"),
    "@楼层列B": ("总层数", "总楼层"),
}


def _pick(columns: Sequence[str], names: Sequence[str]) -> str:
    known = set(columns)
    for name in names:
        if name in known:
            return name
    return names[0]


def _ratio_token(text: str) -> Optional[str]:
    token = str(text or "").strip()
    if not token:
        return None
    if re.search(r"未成交|未命中|失败|空值|为空|缺失|未转化", token):
        return "miss"
    if re.search(r"总数|全部|所有|整体|总计|总体", token):
        return "total"
    if re.search(r"成交|命中|成功|及格|合格|转化", token):
        return "hit"
    return None


def _extract_hints(intent: str, families: Sequence[str]) -> Dict[str, Any]:
    """从描述中确定性地抽取口径提示，注入提示词末尾（近因效应）。"""
    hints: Dict[str, Any] = {}
    if "rate" in families:
        for match in re.finditer(r"[（(]\s*([^）)]{1,24}?)\s*[)）]", intent):
            inner = match.group(1)
            if "/" not in inner and "／" not in inner:
                continue
            left, right = re.split(r"[/／]", inner, maxsplit=1)
            numerator = _ratio_token(left)
            denominator = _ratio_token(right)
            if numerator and denominator:
                hints["ratio_override"] = {
                    "numerator": numerator, "denominator": denominator,
                    "from": inner.strip(),
                }
                break
    # 显式公式 + 平均/均价：必须在聚合后再求比值，避免逐条比值算术平均
    if (re.search(r"平均|均价|均值", intent)
            and re.search(r"(?:等于|=|＝)\s*[（(]?[^）)，,；;。]{1,40}[/／]", intent)):
        hints["forced_avg_ratio"] = True
    dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", intent)
    if len(dates) >= 2:
        hints["date_hint"] = {"start": dates[0], "end": dates[1]}
    else:
        year = re.search(r"(\d{4})\s*年", intent)
        if year:
            hints["date_hint"] = {"year": int(year.group(1))}
    return hints


def _sample(hints: Dict[str, List[str]], column: str, default: str) -> str:
    values = hints.get(column) or []
    return str(values[0]) if values else default


def _fill_placeholders(obj: Any, columns: Sequence[str], hints: Dict[str, List[str]],
                       defaults: Dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {k: _fill_placeholders(v, columns, hints, defaults) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill_placeholders(v, columns, hints, defaults) for v in obj]
    if isinstance(obj, str) and obj.startswith("@"):
        if obj == "@区县值":
            return _sample(hints, defaults.get("@区县", ""), "鼓楼区")
        if obj == "@市值":
            return _sample(hints, defaults.get("@市", ""), "福州")
        if obj.startswith("@日期列_"):
            suffix = obj[len("@日期列_"):]
            labels = {"month": "月", "quarter": "季度", "year": "年", "week": "周"}
            return defaults.get("@日期列", "结束时间") + "_" + labels.get(suffix, suffix)
        if obj.startswith("@") and obj.endswith("之和"):
            return defaults.get(obj[:-2], obj[:-2]) + "之和"
        return defaults.get(obj, obj)
    return obj


def _render_example(family: str, intent: str, rule: Dict[str, Any],
                    columns: Sequence[str], hints: Dict[str, List[str]]) -> Tuple[str, str]:
    defaults = {slot: _pick(columns, names) for slot, names in _SLOTS.items()}
    rendered = _fill_placeholders(rule, columns, hints, defaults)
    rendered_intent = _fill_placeholders(intent, columns, hints, defaults)
    return rendered_intent, json.dumps(rendered, ensure_ascii=False)


# ------------------------------------------------------------
# 任务路由
# ------------------------------------------------------------
def _normalize_intent(intent: str, aliases: Optional[Dict[str, str]]) -> str:
    text = str(intent or "")
    for label, column in sorted((aliases or {}).items(), key=lambda kv: -len(kv[0])):
        text = text.replace(label, str(column))
    return text


def detect_families(intent: str, columns: Optional[Sequence[str]] = None,
                    aliases: Optional[Dict[str, str]] = None) -> List[str]:
    """按触发词打分，返回优先级最高的任务族（最多 3 个）。"""
    text = _normalize_intent(intent, aliases)
    scored: List[Tuple[int, int, str]] = []
    for idx, name in enumerate(_FAMILY_ORDER):
        pattern = FAMILIES[name]["trigger"]
        hits = len(re.findall(pattern, text, flags=re.I))
        if hits:
            # 同分时按 _FAMILY_ORDER 的优先级：索引越小越靠前（composite/rate 优先）
            scored.append((-hits, idx, name))
    scored.sort()
    return [name for _, _, name in scored[:3]]


def _has_any(intent: str, words: Sequence[str]) -> bool:
    return any(w in intent for w in words)


def primary_family(intent: str, columns: Optional[Sequence[str]] = None,
                   aliases: Optional[Dict[str, str]] = None) -> str:
    """单个主任务族；对易混淆场景做显式裁决。"""
    text = _normalize_intent(intent, aliases)
    families = detect_families(text, columns, aliases)
    # 组合流程优先级最高：它不是单一任务族，必须按描述顺序串起多个算子
    if "composite" in families:
        return "composite"
    # 明细提取优先级最高：只要明确要求逐行输出，就不能被日期/分组词带偏
    if _has_any(text, ("提取", "导出", "列出", "查出", "保留明细", "保留逐行", "原样输出")):
        return "detail"
    # 两类比率优先于普通分组计数：「按区县统计未成交率」的分组只是维度，
    # 最终产物仍是率，不能被 count_group 的「按 X 分组」触发词误判。
    if "rate" in families and not re.search(r"条数|个数|几条|多少个|计数", text):
        return "rate"
    if "count_group" in families:
        return "count_group"
    # 两类比率：出现「率/之比」且带判空判据时，永远归 rate
    if "rate" in families and _has_any(text, ("为空", "不为空", "非空", "空值", "缺失", "成交", "未成交")):
        return "rate"
    if "rate" in families:
        return "rate"
    if "count_group" in families:
        return "count_group"
    if "pivot" in families:
        return "pivot"
    if "trend" in families:
        return "trend"
    if "proportion" in families:
        return "proportion"
    if "ranking" in families:
        return "ranking"
    if "dedupe" in families:
        return "dedupe"
    if "derive" in families:
        return "derive"
    if "detail" in families:
        return "detail"
    if "aggregate" in families:
        return "aggregate"
    return families[0] if families else "general"


# ------------------------------------------------------------
# 提示词段落
# ------------------------------------------------------------
def build_router_section() -> str:
    lines = [
        "【硬约束：违反任意一条即判为错误规则，输出前必须逐条核对】",
        "H1 任务族骨架是推荐配方，不是硬性模板：单族需求按骨架填槽；组合需求按描述的动作顺序自由编排原语。",
        "   最终 task_type 由最后一步的产物决定，steps 必须覆盖描述里的每一个动作。",
        "H2 options.group_by / options.agg_funcs 只影响报告展示，不会产出统计结果表；",
        "   凡「按 X 统计条数/求和/均值」必须在 steps 里写 count_rows 或 aggregate，steps 不能为空。",
        "H3 rate 的两类定义只写在 rate.condition；描述里的「判断：X为空-未成交，X不为空-成交」",
        "   是分类口径，不是筛选条件，严禁写进 filter（写了分母只剩一类，结果必错）。",
        "H4 年份/季度/月份/日期区间一律用 date_filter（year/quarter/month/start/end/recent_days）；",
        "   严禁在 filter 里用 eq/ne 匹配四位年份，或把 between/start/end 塞进 filter。",
        "H5 TopN/排名必须先按指标排序（sort descending）再用 head(n)，或用 rank.topn；只写 head 不算排名。",
        "H9 目标后面括号里的比值是最终口径，必须严格照它设置 numerator/denominator：",
        "   （成交/未成交）→ hit/miss；（未成交/成交）→ miss/hit；（成交/总数）→ hit/total；",
        "   （未成交/总数）→ miss/total。不要因为描述里出现了其它词就换成 total。",
        "H6 filter 中同一列不能出现两个不同的 eq 值；需求没写的筛选条件不要自己补充。",
        "H7 明细需求（提取/导出/列出/原样输出）最终必须是逐行数据，禁止追加任何汇总算子。",
        "H8 只有需求明确说「删除空值行」才可用 fill_missing=drop；其余情况一律不用。",
        "H10 steps 绝不能是空数组：只要需求包含筛选/统计/清洗/提取，就必须有对应步骤；",
        "    只有在用户明确说「不做任何处理」时才允许 steps=[]。",
        "H11 task_router.required_ops 列出的算子必须出现在 steps 中，这是本需求的必选算子。",
        "H12 date_filter 必须是 steps 里的独立步骤，绝不能塞进 filter.conditions 当成条件对象；",
        "    filter 的条件只写普通列比较（eq/ne/gt/lt/in/notna/isna）。",
        "H13 组合流程按「然后/接着/最后」逐段映射原语，顺序与描述一致；",
        "    最终产物必须回答最后的分析目标，不能停在中间的去重、提取或清洗。",
        "H14 数据里没有「季度列」不等于不能按季度分析：date_part(parts=[\"year\",\"quarter\"], bucket=\"年季度\")",
        "    从日期列推导组合时间桶；aggregate.by 必须用组合桶列，禁止只用「季度」——那会把不同年的同一季度混为一期。",
        "H15 「统计条数/有几条/数量」一律用 count_rows；禁止用 aggregate(column=[\"*\"], func=[\"count\"]) 代替。",
        "H16 描述给出比值公式（如 成交单价 = 成交价/面积）又要求「平均/均价」时，均价口径是 Σ分子/Σ分母：",
        "    先 expr 派生逐条比值，aggregate 对分子/分母分别 sum，再在 aggregate 之后用 expr 算出最终均价列；",
        "    逐条比值的均值只能作为对照列另起别名，不能冒充季度均价。",
        "H17 「按 X 去重」必须写 dedupe.subset=[X 的真实列]；只说「去重」时优先用业务主键（如 标的号），",
        "    不要留空 subset（整行去重通常等于没去重）。",
        "H18 未明确要求填充/剔除时 options.fill_missing=none、outlier_method=none；",
        "    要求剔除异常值必须显式写 outlier 步骤，不能只在 options 里声明。",
        "",
        "【任务路由表（最高优先级：先分类，再按骨架填槽）】",
        "先读需求确定主任务族与骨架；组合需求按 H1/H13 在骨架基础上编排多个原语，不得漏掉后半段分析。",
        "| 族 | 典型说法 | 必须出现的骨架 | task_type |",
        "| --- | --- | --- | --- |",
    ]
    for name in _FAMILY_ORDER:
        f = FAMILIES[name]
        op = f["skeleton"].get("op") or (
            "多步骤" if "steps" in f["skeleton"] else "—"
        )
        lines.append(
            f"| {name} | {f['title']} | {op} | {f['task_type'] or '由最终产物决定'} |"
        )
    lines.append("")
    lines.append("路由裁决规则：")
    lines.append("- 出现「提取/导出/列出/查出/保留/原样输出」→ detail，最终必须是逐行明细。")
    lines.append("- 出现「成交率/未成交率/转化率/成功率/空值率/A与B之比」→ rate，禁止 assign+count_rows、禁止 pct。")
    lines.append("- 出现「每个 X 有几条/按 X 分组统计条数」→ count_group（summary），禁止 trend/distribution。")
    lines.append("- 出现「某年/区间/近 N 天」→ date_filter，禁止 filter.eq 写四位年份。")
    lines.append("- 出现「占比/比例」但不是两类比率 → proportion（pct）。")
    return "\n".join(lines)


_NEGATIVE_EXAMPLES: Dict[str, List[Tuple[str, str]]] = {
    "composite": [
        (
            '{"op":"date_part","column":"@日期列","parts":["quarter"]},'
            '{"op":"aggregate","by":["@日期列_季度"],"column":["成交单价"],"func":["mean"]}',
            "错误：只按季度分桶会把 2025Q1 和 2026Q1 合并成一期；"
            "正确写法是 date_part(parts=[\"year\",\"quarter\"], bucket=\"年季度\") 并按 @日期列_年季度 分组。",
        ),
        (
            '{"op":"expr","column":"成交单价","expression":"c0/c1",...},'
            '{"op":"aggregate","by":["@日期列_年季度"],"column":["成交单价"],"func":["mean"]}',
            "错误：逐条比值再取均值不是「总成交价/总面积」的均价；"
            "正确写法是先 aggregate 分子分母各自 sum，再在 aggregate 之后用 expr 算 Σ分子/Σ分母。",
        ),
        (
            '{"op":"dedupe","subset":[],"keep":"first"}',
            "错误：subset=[] 是整行去重，实际几乎删不掉重复标的；"
            "正确写法是 subset=[\"标的号\"] 等业务主键。",
        ),
    ],
    "rate": [
        (
            '{"op":"filter","conditions":[{"column":"@判空列","operator":"notna"}]},'
            '{"op":"rate",...}',
            "错误：把判据列的 notna 放进 filter，会把未成交行全部删掉；"
            "正确写法是 filter 只放需求明确给出的筛选（区县/市），判空只出现在 rate.condition。",
        ),
    ],
    "count_group": [
        (
            '{"options":{"group_by":"@分组列"},"steps":[]}',
            "错误：只设置 options.group_by 不会产出统计表；"
            "正确写法是 steps:[{\"op\":\"count_rows\",\"by\":[\"@分组列\"],\"alias\":\"条数\"}]。",
        ),
        (
            '{"op":"aggregate","by":["@分组列"],"column":["*"],"func":["count"],"alias":["条数"]}',
            "错误：统计条数必须用 count_rows，aggregate 的 count 不是可靠的分组计数；"
            "正确写法是 {\"op\":\"count_rows\",\"by\":[\"@分组列\"],\"alias\":\"条数\"}。",
        ),
    ],
    "detail": [
        (
            '{"task_type":"summary","steps":[{"op":"filter",...},'
            '{"op":"count_rows","by":["@区县"],"alias":"条数"}]}',
            "错误：提取/列出需求被做成汇总；正确写法是只保留 filter/fill 等逐行步骤，task_type=detail。",
        ),
    ],
    "date_filter": [
        (
            '{"op":"filter","conditions":[{"column":"@日期列","operator":"eq","value":"2026"}]}',
            "错误：日期列与 2026 全等恒为 False；正确写法是 {\"op\":\"date_filter\",\"column\":\"@日期列\",\"year\":2026}。",
        ),
        (
            '{"op":"filter","conditions":[{"column":"@日期列","operator":"between","value":["2026-01-01","2026-06-30"]}]}',
            "错误：日期区间不能用 filter.between；正确写法是 {\"op\":\"date_filter\",\"column\":\"@日期列\",\"start\":\"2026-01-01\",\"end\":\"2026-06-30\"}。",
        ),
        (
            '{"op":"filter","conditions":[{"op":"date_filter","column":"@日期列","year":2026}]}',
            "错误：date_filter 不能作为 filter.conditions 的条件项；它必须是 steps 中与 filter 平级的独立步骤。",
        ),
    ],
    "ranking": [
        (
            '{"op":"head","n":5}',
            "错误：只 head 没有按指标排序；正确写法是 aggregate/count_rows → sort(by=指标,ascending=false) → head(n)。",
        ),
    ],
}


def _full_rule_example(rule_fragment: Dict[str, Any], source: Optional[Dict[str, Any]],
                       intent: str, columns: Sequence[str], hints: Dict[str, List[str]]) -> str:
    defaults = {slot: _pick(columns, names) for slot, names in _SLOTS.items()}
    fragment = _fill_placeholders(rule_fragment, columns, hints, defaults)
    shown_intent = _fill_placeholders(intent, columns, hints, defaults)
    full: Dict[str, Any] = {
        "version": "2.0",
        "intent": shown_intent,
        "source": source if isinstance(source, dict) and source else {"path": "<user 给定路径>", "format": "csv"},
        "steps": fragment.get("steps", []),
        "options": fragment.get("options", {"fill_missing": "none", "outlier_method": "none"}),
        "output": {"formats": ["csv"], "report": True},
    }
    if fragment.get("task_type"):
        full["task_type"] = fragment["task_type"]
    return json.dumps(full, ensure_ascii=False)


def build_family_section(families: Sequence[str], columns: Sequence[str],
                         hints: Dict[str, List[str]],
                         source: Optional[Dict[str, Any]] = None) -> str:
    blocks: List[str] = []
    for family in families:
        f = FAMILIES.get(family)
        if not f:
            continue
        lines = [
            f"【任务族：{family} — {f['title']}】",
            "骨架：" + json.dumps(f["skeleton"], ensure_ascii=False),
        ]
        if f["task_type"]:
            lines.append(f"task_type 必须写 \"{f['task_type']}\"。")
        examples = _EXAMPLES.get(family, [])[:4]
        for idx, (intent, rule) in enumerate(examples):
            shown_intent, shown_rule = _render_example(family, intent, rule, columns, hints)
            lines.append(f"需求示例：{shown_intent}")
            lines.append("正确规则片段：" + shown_rule)
            if (family == "composite" or
                    (idx == 0 and family in ("rate", "count_group", "detail", "date_filter"))):
                lines.append("完整正确规则（照此结构输出，source 用 user 里给定值）：")
                lines.append(_full_rule_example(rule, source, intent, columns, hints))
        for wrong, reason in _NEGATIVE_EXAMPLES.get(family, []):
            defaults = {slot: _pick(columns, names) for slot, names in _SLOTS.items()}
            lines.append("错误示例：" + _fill_placeholders(wrong, columns, hints, defaults))
            lines.append("错因与改法：" + _fill_placeholders(reason, columns, hints, defaults))
        for anti in f["anti"]:
            lines.append("禁止：" + anti)
        for chk in f["check"]:
            lines.append("口径：" + chk)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_context_section(columns: Sequence[str], hints: Dict[str, List[str]],
                          aliases: Optional[Dict[str, str]],
                          families: Sequence[str]) -> str:
    lines = ["【动态上下文：只能使用下列真实列名与真实取值】"]
    lines.append("真实列：" + "、".join(str(c) for c in columns[:200]))
    if hints:
        picks: List[str] = []
        for name in ("区县", "市", "物业类型", "状态", "小区"):
            if name in hints:
                picks.append(f"{name} = {' / '.join(hints[name][:5])}")
        if picks:
            lines.append("真实取值：" + "；".join(picks))
    if aliases:
        lines.append("列名映射（描述 → 真实列）：" + json.dumps(aliases, ensure_ascii=False))
    important = [name for name in ("rate", "detail", "count_group", "date_filter") if name in families]
    if important:
        lines.append("本需求命中的重点族：" + "、".join(important) + "（其骨架与禁令优先于通用规则）")
    return "\n".join(lines)


def build_checklist(families: Sequence[str]) -> str:
    checks = [
        "【输出前自检：逐条确认，不满足就改，再输出 JSON】",
        "1. 所有 column/into/by 引用的列名都出现在「真实列」里（别名映射用右侧真实列）。",
        "2. 任务族与最终产物一致：要明细就是逐行表，要计数就有计数列，要比率就有比率列。",
        "3. 年份/季度/月份筛选使用 date_filter，filter 里没有对日期列的 eq/ne 四位年份。",
        "4. 需求里的筛选条件都写进 filter，判空判据只写在 rate/pct.condition，不写进 filter。",
        "5. 没有无条件使用 options.fill_missing=drop；要求「其他字段为空」时写 none。",
        "6. 最终的 steps 能在数据上直接跑出用户要的列，没有中间结果当最终结果。",
        "7. 按季度/月/周看趋势时用了 year+细粒度的组合时间桶，结果表每一期都带年份。",
        "8. 比值指标的「均价」是 Σ分子/Σ分母，不是逐条比值的简单平均；两种口径列名分开。",
        "9. 去重键、时间列、筛选顺序与描述一致；空值/异常值口径未被隐式改变。",
    ]
    for family in families:
        f = FAMILIES.get(family)
        if not f:
            continue
        for idx, item in enumerate(f["check"], start=len(checks)):
            checks.append(f"{idx}. [{family}] {item}")
    return "\n".join(checks)


def build_prompt_sections(intent: str, columns: Sequence[str],
                          hints: Optional[Dict[str, List[str]]] = None,
                          aliases: Optional[Dict[str, str]] = None,
                          source: Optional[Dict[str, Any]] = None
                          ) -> Tuple[str, Dict[str, Any]]:
    """返回 (附加 system 文本, 供 user payload 使用的路由信息)。"""
    hints = hints or {}
    families = detect_families(intent, columns, aliases)
    primary = primary_family(intent, columns, aliases)
    if primary not in families:
        families = [primary] + families
    else:
        families = [primary] + [f for f in families if f != primary]
    families = families[:3]
    required_ops = list(_REQUIRED_OPS.get(primary, []))
    extracted = _extract_hints(intent, families)
    if primary == "count_group":
        group_match = re.search(
            r"(?:按|按照|根据|每个|各)\s*([^\s,，；;。]{1,16}?)\s*(?:分组|有几条|多少条|的条数|数量|个数)",
            intent,
        )
        if group_match:
            group_col = _pick(columns, (group_match.group(1),))
            if group_col in {str(c) for c in columns}:
                extracted["forced_count_step"] = {
                    "op": "count_rows", "by": [group_col], "alias": "条数",
                }
    if "date_hint" in extracted:
        if "date_filter" not in required_ops:
            required_ops.append("date_filter")
        if "date_filter" not in families:
            families.append("date_filter")
        date = extracted["date_hint"]
        date_col = _pick(columns, _SLOTS["@日期列"])
        extracted["forced_date_step"] = {"op": "date_filter", "column": date_col, **date}
    reminder_items = [
        f"本需求必选算子：{('、'.join(required_ops) if required_ops else '无')}；steps 不允许为空。",
    ]
    if "forced_count_step" in extracted:
        forced = json.dumps(extracted["forced_count_step"], ensure_ascii=False)
        reminder_items.append(
            f"必须把 task_router.forced_count_step（{forced}）原样作为 steps 的一项；"
            "禁止用 aggregate(column=[\"*\"], func=[\"count\"]) 代替 count_rows。"
        )
    if "ratio_override" in extracted:
        ratio = extracted["ratio_override"]
        reminder_items.append(
            f"括号口径 {ratio['from']} 覆盖目标列名：numerator=\"{ratio['numerator']}\", "
            f"denominator=\"{ratio['denominator']}\"，不得改成 total。"
        )
    if "date_hint" in extracted:
        forced = json.dumps(extracted["forced_date_step"], ensure_ascii=False)
        reminder_items.append(
            f"必须把 task_router.forced_date_step（{forced}）原样放进 steps："
            "简单筛选场景作为第一项；组合分析场景放在该分析阶段的 date_part 之前；"
            "filter 里不得再写任何日期条件。"
        )
    if "forced_avg_ratio" in extracted:
        value_col = _pick(columns, ("成交单价", "单价")) or "成交单价"
        num_col = _pick(columns, ("成交价", "金额")) or "<分子列>"
        den_col = _pick(columns, ("面积",)) or "<分母列>"
        num_alias, den_alias = f"{num_col}之和", f"{den_col}之和"
        forced = json.dumps([
            {"op": "aggregate", "by": ["<分组列或时间桶列>"],
             "column": [value_col, num_col, den_col], "func": ["mean", "sum", "sum"],
             "alias": [f"{value_col}·逐条均值", num_alias, den_alias]},
            {"op": "expr", "column": value_col, "expression": "c0/c1",
             "operands": [num_alias, den_alias]},
        ], ensure_ascii=False)
        reminder_items.append(
            "本需求是比值型均价，必须严格按下面两步输出（by 填实际分组列/时间桶列）：" + forced
            + "；禁止只对逐条比值取 mean 就结束。"
        )
    reminder_items.append("只输出 JSON 对象，不要解释。")
    reminder_lines = ["【输出前最后核对（最高优先级，逐条照做）】"] + [
        f"{index}. {item}" for index, item in enumerate(reminder_items, start=1)
    ]
    prefix = "\n\n".join([
        build_skill_index(families),
        build_router_section(),
        build_family_section(families, columns, hints, source),
        build_context_section(columns, hints, aliases, families),
        f"【本需求必选算子】{('、'.join(required_ops) if required_ops else '无强制算子')}"
        "（必选算子必须出现在 steps；ranking 可用 rank.topn 代替 sort+head）",
    ])
    suffix = "\n\n" + build_checklist(families)
    expected_type = FAMILIES.get(primary, {}).get("task_type")
    if primary == "composite":
        if re.search(r"趋势|走势|按\s*季度|按\s*月|按\s*年", intent):
            expected_type = "trend"
        elif re.search(r"条数|分组|计数", intent):
            expected_type = "summary"
        else:
            expected_type = "detail"
    info = {
        "primary_family": primary,
        "families": families,
        "required_ops": required_ops,
        "extracted_hints": extracted,
        "reminder": "\n".join(reminder_lines),
        "expected_task_type": expected_type,
        "sample_values": {
            k: hints[k][:5] for k in ("区县", "市", "物业类型", "状态") if k in hints
        },
    }
    return prefix + suffix, info


def build_repair_user_payload(original_user: Dict[str, Any], previous_rule: Dict[str, Any],
                              issues: Sequence[str]) -> str:
    """生成定点修复提示：只改被指出的问题，保持其余部分不变。"""
    payload = dict(original_user)
    payload["instruction"] = (
        "上一版规则语义校验未通过。请只修正下面列出的问题，保留其余正确部分，"
        "重新输出一份完整的规则 JSON（不要解释、不要代码围栏）。"
    )
    payload["previous_rule"] = previous_rule
    payload["issues_to_fix"] = list(issues)
    payload["repair_principles"] = [
        "先看 issues_to_fix 的 code，按任务族骨架修正，不要改用别的算子族。",
        "列引用只能来自 columns / column_aliases 的右侧真实列。",
        "修正后重新执行输出前自检清单。",
    ]
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "FAMILIES", "detect_families", "primary_family", "build_prompt_sections",
    "build_repair_user_payload", "build_router_section", "build_family_section",
    "build_checklist", "build_skill_index", "select_ops",
]

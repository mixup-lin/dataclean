# -*- coding: utf-8 -*-
"""规则语义评测器。

``validate_rule`` 只保证规则「结构上能跑」；本模块判断规则「是否回答了
用户的问题」。用途有两处：

1. 在线生成后的语义自检：发现问题立刻带 code 回灌模型定点修复；
2. 场景矩阵的准确率度量：统计「生成即正确」的比例。

评测口径与 ``ops.prompt_kit`` 的任务族骨架一一对应，避免提示词说一套、
评测用另一套。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ops.prompt_kit import _normalize_intent, primary_family

# 只有这些键里的内容是列引用；value/conditions 里的业务值不算
_COLUMN_KEYS = frozenset({
    "column", "columns", "by", "into", "index", "values", "within",
    "left", "right", "subset", "group_by",
})

# 产出新列的算子（其输出列允许出现在后续步骤）
_PRODUCERS = frozenset({
    "assign", "derive", "bin", "rank", "date_part", "count_rows",
    "aggregate", "pct", "rate", "pivot", "convert_unit", "string",
})

# 明细任务禁止的汇总算子
_SUMMARY_OPS = frozenset({
    "aggregate", "count_rows", "pivot", "rate", "pct", "rank",
    "group_rank", "resample", "corr", "describe",
})


@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# 阻塞性语义问题：这些问题意味着规则答非所问或结果必然错误，
# 确定性编译候选若解决了其中任意一项即可触发替换（见 rules.generate_rules）。
# 放在评测器内作为单一事实源，避免调用方再硬编码一份字符串集合而漂移。
BLOCKING_CODES: frozenset = frozenset({
    "empty_steps", "missing_rate_op", "missing_count", "missing_aggregate",
    "missing_trend", "missing_pivot", "missing_pct", "missing_fill",
    "composite_no_analysis", "composite_formula_missing",
    "detail_has_summary", "judge_in_filter", "date_filter_nested",
    "missing_date_filter", "date_in_filter", "date_range_in_filter",
    "date_eq_year", "contradictory_filter", "wrong_task_type",
    "avg_ratio_missing", "cross_year_bucket", "dedupe_after_filter",
    "avg_ratio_null_pair_missing",
})


def format_issues(issues: Sequence[Issue]) -> List[str]:
    return [str(i) for i in issues]


def _steps(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = rule.get("steps", []) if isinstance(rule, dict) else []
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _op_names(steps: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(s.get("op", "")) for s in steps]


def _walk_conditions(obj: Any) -> Iterable[Dict[str, Any]]:
    """递归取出所有条件对象（含 filter.conditions 与嵌套 conditions）。"""
    if isinstance(obj, dict):
        if "column" in obj and ("operator" in obj or "op" in obj):
            yield obj
        for value in obj.values():
            yield from _walk_conditions(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_conditions(value)


def _column_refs(obj: Any, produced: Set[str]) -> Set[str]:
    """递归收集列引用；数字型 right、业务值不收集。"""
    out: Set[str] = set()
    if isinstance(obj, dict):
        # join/union 的 right 是数据源别名，不是列引用；其余 right（如
        # cross_column_filter）仍是列引用，不能一刀切排除。
        join_like = str(obj.get("op", "")) in {
            "join", "union", "anti_join", "semi_join",
        }
        for key, value in obj.items():
            if join_like and key == "right":
                continue
            if key in _COLUMN_KEYS:
                candidates = value if isinstance(value, list) else [value]
                for item in candidates:
                    if not isinstance(item, str):
                        continue
                    if item in produced:
                        continue
                    out.add(item)
            else:
                out |= _column_refs(value, produced)
    elif isinstance(obj, list):
        for value in obj:
            out |= _column_refs(value, produced)
    return out


def _produced_names(steps: Sequence[Dict[str, Any]], columns: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for step in steps:
        op = str(step.get("op", ""))
        for key in ("column", "into", "alias"):
            value = step.get(key)
            if isinstance(value, str):
                out.add(value)
            elif isinstance(value, list):
                out.update(str(v) for v in value if isinstance(v, str))
        if op == "date_part":
            col = step.get("column")
            if isinstance(col, str):
                for part in step.get("parts", ["year", "month"]):
                    suffix = {"year": "年", "month": "月", "day": "日", "quarter": "季度"}.get(str(part), str(part))
                    out.add(f"{col}_{suffix}")
                bucket = step.get("bucket")
                if isinstance(bucket, str) and bucket:
                    out.add(f"{col}_{bucket}")
    return out


def _date_like(column: str) -> bool:
    return bool(re.search(r"(时间|日期|date|time|year|年份)", str(column), re.I))


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


def _expected_rate_direction(intent: str, alias: str) -> Optional[tuple]:
    text = f"{intent} {alias}"
    # 括号里显式写出的比值最权威，例如「未成交率（未成交/总数）」
    for match in re.finditer(r"[（(]\s*([^）)]{1,24}?)\s*[)）]", text):
        inner = match.group(1)
        if "/" not in inner and "／" not in inner:
            continue
        left, right = re.split(r"[/／]", inner, maxsplit=1)
        numerator = _ratio_token(left)
        denominator = _ratio_token(right)
        if numerator and denominator:
            return (numerator, denominator)
    # 目标列名次之（避免把「判断：为空-未成交」里的词误当成目标）
    alias_text = str(alias or "")
    if "未成交" in alias_text or "空值率" in alias_text or "缺失率" in alias_text:
        return ("miss", "total")
    if re.search(r"成交率|命中率|成功率|及格率|合格率|转化率", alias_text):
        return ("hit", "total")
    # 最后才看整段描述里的目标词
    if re.search(r"统计.{0,12}未成交率|目标.{0,12}未成交率", text):
        return ("miss", "total")
    if re.search(r"统计.{0,12}成交率|目标.{0,12}成交率", text):
        return ("hit", "total")
    return None


def _year_requested(intent: str) -> bool:
    """需求是否给出了具体时间范围。

    「按年/按季度/按月」只是分组粒度，不应该触发 date_filter；只有具体年份、
    日期区间或相对天数才需要独立筛选步骤。
    """
    return bool(re.search(
        r"((?:19|20)\d{2}\s*年|\d{4}[-/]\d{1,2}[-/]\d{1,2}|近\s*\d+\s*天)",
        intent,
    ))


def evaluate_rule(rule: Any, intent: str, columns: Sequence[str],
                  aliases: Optional[Dict[str, str]] = None) -> List[Issue]:
    """返回语义问题列表；空列表视为「生成即正确」。"""
    issues: List[Issue] = []
    if not isinstance(rule, dict):
        return [Issue("not_object", "规则必须是 JSON 对象")]
    steps = _steps(rule)
    ops = _op_names(steps)
    if not steps:
        return [Issue("empty_steps", "steps 为空，无法回答需求")]
    options = rule.get("options") if isinstance(rule.get("options"), dict) else {}
    text = _normalize_intent(intent, aliases)
    family = primary_family(text, columns, aliases)
    known = {str(c) for c in columns}

    # ---------- 通用检查 ----------
    filter_steps = [s for s in steps if s.get("op") == "filter"]
    produced = _produced_names(steps, columns)
    for column in sorted(_column_refs(steps, produced)):
        if column not in known and column != "*":
            issues.append(Issue("unknown_column", f"列 {column!r} 不在真实列清单中"))

    for filter_step in filter_steps:
        conditions = filter_step.get("conditions")
        if not isinstance(conditions, list):
            continue
        for cond in conditions:
            if not isinstance(cond, dict) or "column" not in cond:
                issues.append(Issue("invalid_condition", "filter.conditions 必须由含 column/operator 的条件对象组成"))
                continue
            if "operator" not in cond and "conditions" not in cond:
                issues.append(Issue(
                    "invalid_condition",
                    f"filter 条件 {cond!r} 缺少 operator；year/区间应改用 date_filter，不能塞进普通条件",
                ))

    # 同一列被筛成两个不同取值（典型 LLM 错误：先筛 A 再筛 B）
    eq_values: Dict[str, Set[str]] = {}
    for cond in _walk_conditions(steps):
        col = cond.get("column")
        operator = str(cond.get("operator", "")).lower()
        if isinstance(col, str) and operator in ("eq", "==", "=") and "value" in cond:
            eq_values.setdefault(col, set()).add(str(cond["value"]))
    for col, values in eq_values.items():
        if len(values) > 1:
            issues.append(Issue("contradictory_filter", f"列 {col!r} 被筛成多个互斥取值：{sorted(values)}"))

    # 同一列先判空再判非空（或相反）会把结果筛成 0 行。比率需求里这通常是
    # 把「两类标签」误写成两次 filter，必须由 rate 算子一次完成。
    null_ops: Dict[str, Set[str]] = {}
    for cond in _walk_conditions(filter_steps):
        col = cond.get("column")
        operator = str(cond.get("operator", "")).lower()
        if isinstance(col, str) and operator in {"isna", "notna"}:
            null_ops.setdefault(col, set()).add(operator)
    for col, operators in null_ops.items():
        if len(operators) > 1:
            issues.append(Issue(
                "contradictory_filter",
                f"列 {col!r} 同时被 isna 和 notna 筛选，结果必然为空；两类比率应使用 rate 算子",
            ))

    for filter_step in filter_steps:
        for cond in (filter_step.get("conditions") or []):
            if isinstance(cond, dict) and str(cond.get("op", "")) == "date_filter":
                issues.append(Issue(
                    "date_filter_nested",
                    "date_filter 被塞进 filter.conditions；必须是 steps 里的独立步骤",
                ))

    year_columns = {
        name
        for step in steps if step.get("op") == "date_part"
        for name in _produced_names([step], columns)
        if name.endswith("_年")
    }
    for cond in _walk_conditions(filter_steps):
        col = str(cond.get("column", ""))
        operator = str(cond.get("operator", "")).lower()
        if col in year_columns:
            continue
        if operator in ("eq", "ne", "==", "=", "!=", "between", "gt", "lt") and _date_like(col):
            value = str(cond.get("value", ""))
            if re.fullmatch(r"\d{4}", value):
                issues.append(Issue("date_eq_year", f"日期列 {col!r} 用 eq/ne 匹配年份 {value}，结果会为 0 行；应改用 date_filter"))
            if re.search(r"\d{4}-\d{1,2}-\d{1,2}", value):
                code = "date_in_filter" if operator in ("eq", "ne", "==", "=", "!=") else "date_range_in_filter"
                issues.append(Issue(code, f"日期条件写在 filter 的 {operator} 里；应改用独立的 date_filter 步骤"))

    has_null_semantics = any(
        str(cond.get("operator", "")).lower() in ("isna", "notna")
        for cond in _walk_conditions(steps)
    )
    if has_null_semantics and str(options.get("fill_missing", "")).lower() == "drop":
        issues.append(Issue("drop_destroys_null", "规则依赖判空语义，但 options.fill_missing=drop 会删除空值行"))

    # ---------- 跨场景语义：时间桶、均价口径、原语顺序 ----------
    date_part_steps = [s for s in steps if s.get("op") == "date_part"]
    date_filter_steps = [s for s in steps if s.get("op") == "date_filter"]
    # 单一具体年份筛选后，同期月份不会跨年合并，无需再强制 date_part 带 year；
    # 但年份区间、未限定年份的按月/按季度趋势仍必须用 year+细粒度组合桶。
    exact_year_filter = any(str(s.get("year") or "").strip().isdigit()
                            for s in date_filter_steps)
    for dp in date_part_steps:
        parts = [str(p) for p in (dp.get("parts") or [])]
        fine = [p for p in parts if p in ("quarter", "month", "week", "day")]
        if fine and "year" not in parts and not exact_year_filter:
            issues.append(Issue(
                "cross_year_bucket",
                f"date_part 只有 {fine} 没有 year，且未用 date_filter 锁定单一年份；"
                "跨年同期会被合并，应加 year 并用 bucket 组合时间桶",
            ))
    if (date_part_steps and re.search(r"每一?年|逐年|按年.*按季度|按季度.*按年", text)
            and not any("year" in [str(p) for p in (s.get("parts") or [])]
                        for s in date_part_steps)):
        issues.append(Issue("year_missing_in_trend", "需求要求按年+季度，但 date_part 没有 year 分量"))

    aggregate_idx = [i for i, s in enumerate(steps) if s.get("op") == "aggregate"]
    expr_idx = [i for i, s in enumerate(steps) if s.get("op") == "expr"]
    avg_ratio_requested = bool(
        aggregate_idx and re.search(r"平均|均价|均值", text)
        and re.search(r"/|除以|÷|×|\*", text)
    )
    if avg_ratio_requested and not any(i > max(aggregate_idx) for i in expr_idx):
        issues.append(Issue(
            "avg_ratio_missing",
            "显式比值指标的均价必须在 aggregate 之后再用 expr 计算 Σ分子/Σ分母，"
            "否则得到的只是逐条比值的算术平均",
        ))
    # Σ分子/Σ分母 要求分子分母成对有效；只丢掉分子空值会把分母留在合计里，
    # 均价被稀释。这里静态检查聚合前是否有成对的 notna 过滤。
    if avg_ratio_requested:
        row_ratio = next((
            s for s in steps[:min(aggregate_idx)] if s.get("op") == "expr"
            and "/" in str(s.get("expression", ""))
            and len(s.get("operands") or []) == 2
        ), None)
        if row_ratio:
            pair = {str(v) for v in row_ratio["operands"]}
            paired = False
            for index, step in enumerate(steps):
                if aggregate_idx and index >= min(aggregate_idx):
                    break
                if step.get("op") != "filter":
                    continue
                notna_cols = {
                    str(cond.get("column")) for cond in _walk_conditions([step])
                    if str(cond.get("operator", "")).lower() == "notna" and cond.get("column")
                }
                if pair <= notna_cols:
                    paired = True
                    break
            if not paired:
                issues.append(Issue(
                    "avg_ratio_null_pair_missing",
                    "比值型均价必须先按分子、分母成对 notna 过滤，避免空分子稀释 Σ分母",
                ))

    keyed_dedupe = re.search(r"按\s*([^\s,，；;。]{1,12})\s*去重", text)
    if keyed_dedupe:
        for step in steps:
            if step.get("op") == "dedupe" and not (step.get("subset") or []):
                issues.append(Issue("dedupe_key_lost", "需求明示了去重键，但 dedupe.subset 为空"))
                break
    if re.search(r"去重.{0,20}(?:然后|接着|再|最后).{0,20}(?:提取|筛选)", text):
        dedupe_at = next((i for i, s in enumerate(steps) if s.get("op") == "dedupe"), -1)
        filter_at = next((i for i, s in enumerate(steps) if s.get("op") == "filter"), -1)
        if dedupe_at >= 0 and filter_at >= 0 and dedupe_at > filter_at:
            issues.append(Issue("dedupe_after_filter", "描述要求先去重后筛选，但步骤顺序相反"))
    if re.search(r"(?:剔除|去掉|删除).{0,8}异常", text) and not ({"outlier", "clip"} & set(ops)):
        issues.append(Issue("outlier_not_handled", "要求剔除异常值但步骤中没有 outlier/clip"))

    if (_year_requested(text) and "date_filter" not in ops
            and not any(
                isinstance(cond.get("column"), str) and cond["column"] in year_columns
                for cond in _walk_conditions(filter_steps)
            )):
        issues.append(Issue("missing_date_filter", "需求含具体年份/区间，必须用 date_filter 做筛选"))

    # ---------- 任务族检查 ----------
    if family == "composite":
        # 组合场景不绑定固定链路：只要求动作覆盖完整、存在分析算子、task_type 一致。
        if len(ops) < 2:
            issues.append(Issue("composite_too_few_ops", "组合需求必须编排至少两个原语"))
        # 组合清洗/筛选的终点仍是明细，不应强制追加分析算子；但若描述明确要求
        # 分析/统计/趋势等结果，则无论 LLM 把 task_type 写成 detail 都要补齐分析算子。
        needs_analysis = bool(re.search(
            r"分析|统计|汇总|趋势|排名|占比|比例|率|条数|数量|每个|分组|透视|相关|均值|平均|总和|总计",
            text,
        ))
        if (needs_analysis
                and not ({"aggregate", "count_rows", "resample", "pct", "rank", "pivot"}
                         & set(ops))):
            issues.append(Issue("composite_no_analysis", "组合流程末尾缺少分析步骤，结果不再是分析产物"))
        if re.search(r"去重", text) and "dedupe" not in ops:
            issues.append(Issue("composite_stage_missing", "需求明确要求去重，但步骤里没有 dedupe"))
        if re.search(r"等于|=|＝|除以|乘以", text) and re.search(r"[+*/×÷]|\d", text) \
                and not ({"expr", "derive"} & set(ops)):
            issues.append(Issue("composite_formula_missing", "需求给出显式公式，必须用 expr/derive 派生新列"))
        if re.search(r"按\s*季度|每季度", text) and "date_part" not in ops \
                and "resample" not in ops and "date_filter" not in ops:
            issues.append(Issue("composite_quarter_missing", "按季度分析需要 date_part(quarter) 或 resample 推导"))
        expected = "trend" if ({"date_part", "resample"} & set(ops)) else rule.get("task_type")
        if expected and rule.get("task_type") not in (None, expected, "summary", "detail", "ranking", "proportion"):
            issues.append(Issue("wrong_task_type", "组合分析的 task_type 与最终产物不一致"))

    elif family == "rate":
        rate_steps = [s for s in steps if s.get("op") == "rate"]
        if not rate_steps:
            issues.append(Issue("missing_rate_op", "两类比率必须用 steps.rate，不能换成 assign/count_rows/pct"))
        else:
            rate = rate_steps[0]
            judge_col = str(rate.get("column", ""))
            for cond in _walk_conditions(filter_steps):
                if str(cond.get("column")) == judge_col and str(cond.get("operator", "")).lower() in ("isna", "notna"):
                    issues.append(Issue("judge_in_filter", f"判据列 {judge_col!r} 的判空条件不能写进 filter，会截断分母"))
                    break
            if "pct" in ops:
                issues.append(Issue("pct_after_rate", "rate 之后不能再接 pct，rate 输出已是汇总表"))
            alias = str(rate.get("alias", ""))
            expected = _expected_rate_direction(text, alias)
            if expected:
                # 命中类由 condition 决定：condition=isna 时「不成交」也可能是命中类。
                # 只要目标名与 true_label/false_label 明确一致，就以标签为准判断方向，
                # 避免把「空值=未成交，统计未成交率（hit/isna）」误判成 miss。
                target_label = re.sub(r"(率|占比|比例|百分比)$", "", alias).strip()
                true_label = str(rate.get("true_label") or "").strip()
                false_label = str(rate.get("false_label") or "").strip()
                if target_label and target_label == true_label:
                    expected = ("hit", expected[1])
                elif target_label and target_label == false_label:
                    expected = ("miss", expected[1])
                actual = (str(rate.get("numerator", "hit")), str(rate.get("denominator", "total")))
                if actual != expected:
                    issues.append(Issue(
                        "wrong_rate_direction",
                        f"比率方向应为 numerator={expected[0]}, denominator={expected[1]}，实际为 {actual}",
                    ))
            if rule.get("task_type") not in (None, "proportion"):
                issues.append(Issue("wrong_task_type", f"比率任务 task_type 应为 proportion，实际为 {rule.get('task_type')!r}"))

    elif family == "detail":
        for op in set(ops) & _SUMMARY_OPS:
            issues.append(Issue("detail_has_summary", f"明细任务禁止汇总算子 {op}"))
        if rule.get("task_type") not in (None, "detail"):
            issues.append(Issue("wrong_task_type", f"明细任务 task_type 应为 detail，实际为 {rule.get('task_type')!r}"))
        if re.search(r"(置为|填充|补零|填空)", text) and "fill" not in ops:
            issues.append(Issue("missing_fill", "需求要求把空值置为具体值，但 steps 里没有 fill"))
        if re.search(r"(其他字段|其余字段|其他列|其余列).{0,12}(null|空|不填充|保留)", text, re.I):
            if str(options.get("fill_missing", "")).lower() != "none":
                issues.append(Issue("fill_missing_not_none", "要求其他字段保持空值时 options.fill_missing 必须为 none"))

    elif family == "count_group":
        if not ({"count_rows"} & set(ops) or "aggregate" in ops):
            issues.append(Issue("missing_count", "分组计数必须用 count_rows（或 aggregate func=count），最终表要有计数列"))
        if rule.get("task_type") not in (None, "summary", "ranking"):
            issues.append(Issue("wrong_task_type", f"分组计数 task_type 应为 summary，实际为 {rule.get('task_type')!r}"))

    elif family == "date_filter":
        pass  # 日期筛选检查已在通用检查中完成

    elif family == "proportion":
        if "pct" not in ops and "rate" not in ops:
            issues.append(Issue("missing_pct", "占比需求必须出现 pct 或 rate，不能只筛选"))

    elif family == "ranking":
        if not ({"rank", "group_rank"} & set(ops) or ("sort" in ops and ({"head", "tail", "topn"} & set(ops)))):
            issues.append(Issue("missing_ranking", "排名/TopN 必须有 rank 或 sort + head/tail"))

    elif family == "trend":
        if not ({"date_part", "resample"} & set(ops)):
            issues.append(Issue("missing_trend", "趋势需求必须按时间维度分组（date_part 或 resample）"))

    elif family == "pivot":
        if "pivot" not in ops:
            issues.append(Issue("missing_pivot", "透视需求必须用 pivot 产出交叉表"))

    elif family == "dedupe":
        if "dedupe" not in ops and "dedupe_fuzzy" not in ops:
            issues.append(Issue("missing_dedupe", "去重需求必须用 dedupe / dedupe_fuzzy"))
        summary = set(ops) & _SUMMARY_OPS
        if summary:
            issues.append(Issue("dedupe_has_summary", f"去重明细不应包含汇总算子 {sorted(summary)}"))

    elif family == "derive":
        expected_ops = {"derive", "string", "convert_unit", "date_diff", "category_map", "text_normalize"}
        if not (expected_ops & set(ops)):
            issues.append(Issue("missing_derive", "字段衍生需求必须用 derive/string/convert_unit 等变换算子"))

    elif family == "aggregate":
        if "aggregate" not in ops:
            issues.append(Issue("missing_aggregate", "汇总统计必须用 aggregate，且 column/func/alias 对齐"))

    return issues


def correctness_score(issues: Sequence[Issue]) -> float:
    return 1.0 if not issues else 0.0


__all__ = ["Issue", "evaluate_rule", "format_issues", "correctness_score"]

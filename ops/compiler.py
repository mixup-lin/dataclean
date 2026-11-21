# -*- coding: utf-8 -*-
"""编译/降解层：把宽松的规则 JSON 编译为强类型可执行序列。

职责：
1. 列名对齐：剔除引用不存在列的步骤并给出可审计告警（含 date_part 短标签别名回退）；
2. 契约校验：按注册表逐步骤校验，错误一次性聚合；
3. 产出 ``ExecutablePlan``：执行器只消费编译产物。

LLM 的输出视为不可信输入，只有通过编译层白名单与列引用校验后才会被执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ops import contract
from ops.constants import DATE_PART_LABELS, as_list
from ops.errors import RuleValidationError
from ops.intent import judgement_columns, parse_rate_intent, target_from_intent
from ops.intent_schema import rate_spec_from_schema, target_from_schema
from ops.registry import get_op, op_names


# ------------------------------------------------------------
# 意图读取迁移：有 Schema 时只读结构化字段，旧规则仍走文本解析兜底
# ------------------------------------------------------------
def _rule_schema(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = rule.get("intent_schema")
    return payload if isinstance(payload, dict) else None


def _rate_target(rule: Dict[str, Any]) -> Optional[Dict[str, str]]:
    schema = _rule_schema(rule)
    if schema is not None:
        return target_from_schema(schema)
    return target_from_intent(rule.get("intent"))


def _rate_spec(rule: Dict[str, Any], columns: List[str]):
    schema = _rule_schema(rule)
    if schema is not None:
        return rate_spec_from_schema(schema, columns, rule.get("column_aliases"))
    return parse_rate_intent(rule.get("intent"), columns, rule.get("column_aliases"))


# ------------------------------------------------------------
# 列名别名回退（date_part 短标签「年」→「结束时间_年」）
# ------------------------------------------------------------
def _rewrite_alias(obj: Any, alias: Dict[str, str]) -> None:
    """原地改写**条件类列引用**的短标签别名。

    重要：别名只用于把「年」这类筛选口语还原成 date_part 派生的真实列名。
    它**不得**改写 ``aggregate.by`` / ``pivot.index`` / ``group_by`` 等分析维度键，
    否则会把分组维度静默替换成时间列（历史上曾把「按小区统计」变成趋势分析）。
    """
    # 仅 column 允许别名回退；by/index/group_by/within/left/right 一律保持原值
    col_keys = ("column",)
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in col_keys and isinstance(v, str) and v in alias:
                obj[k] = alias[v]
            elif k in ("columns",) and isinstance(v, list):
                obj[k] = [alias.get(x, x) if isinstance(x, str) else x for x in v]
            elif k in ("conditions", "rules", "condition") and isinstance(v, list):
                for item in v:
                    _rewrite_alias(item, alias)
            elif k in ("conditions", "condition") and isinstance(v, dict):
                _rewrite_alias(v, alias)
            else:
                _rewrite_alias(v, alias)
    elif isinstance(obj, list):
        for item in obj:
            _rewrite_alias(item, alias)


def reconcile_rule_columns(
    rule: Dict[str, Any], columns: List[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """把引用不存在列的 steps / 选项剔除，返回 (新规则, 警告列表)。"""
    rule = dict(rule)
    known = set(columns)
    warnings: List[str] = []
    alias: Dict[str, str] = {}
    if not known:
        return rule, warnings

    kept: List[Dict[str, Any]] = []
    for idx, step in enumerate(rule.get("steps", [])):
        if not isinstance(step, dict):
            warnings.append(f"steps[{idx}] 非对象，已剔除")
            continue
        _rewrite_alias(step, alias)
        refs = contract.step_refs(step)
        if refs is not None:
            missing = [str(c) for c in refs if isinstance(c, str) and c and c not in known]
            if missing:
                warnings.append(
                    f"steps[{idx}]({step.get('op')}) 引用不存在的列 {missing}，已剔除该步骤"
                )
                continue
        kept.append(step)
        known.update(contract.step_outputs(step))
        if step.get("op") == "date_part":
            base = step.get("column")
            for part in as_list(step.get("parts")):
                lab = DATE_PART_LABELS.get(str(part))
                if lab and isinstance(base, str) and base:
                    derived = f"{base}_{lab}"
                    if lab not in columns:
                        alias[lab] = derived
                    # 兼容英文分量写法（end_time_year → end_time_年），英文列名常见
                    en_name = f"{base}_{part}"
                    if str(part) != lab and en_name not in known:
                        alias[en_name] = derived
    rule["steps"] = kept

    options = dict(rule.get("options", {}))
    for key in ("group_by", "time_column", "pivot_index", "pivot_columns", "pivot_values"):
        val = options.get(key)
        # options 中的分析维度不允许被短标签别名改写（与 steps 同口径）
        if isinstance(val, str) and val not in known:
            warnings.append(f"options.{key} 引用不存在的列 {val!r}，已清除")
            options[key] = None
    rule["options"] = options
    return rule, warnings


# ------------------------------------------------------------
# 语义归一：把「assign(判空) + count_rows」纠偏为 rate
# ------------------------------------------------------------
def _binary_rate_candidate(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """识别「A 为空→未成交，A 非空→成交」这类互补二分类打标。"""
    if not isinstance(step, dict) or step.get("op") != "assign":
        return None
    target = step.get("column")
    if not isinstance(target, str) or not target:
        return None
    rules = step.get("rules")
    if not isinstance(rules, list) or len(rules) != 2:
        return None
    conds: Dict[str, Dict[str, Any]] = {}
    source_cols: set = set()
    for rule in rules:
        if not isinstance(rule, dict):
            return None
        cond = rule.get("condition")
        label = rule.get("value")
        if not isinstance(cond, dict) or not isinstance(label, str) or not label.strip():
            return None
        op = cond.get("operator")
        col = cond.get("column")
        if op not in ("isna", "notna") or not isinstance(col, str) or not col:
            return None
        if op in conds:  # 两个规则必须互补
            return None
        conds[op] = rule
        source_cols.add(col)
    if set(conds) != {"isna", "notna"} or len(source_cols) != 1:
        return None
    return {
        "target": target,
        "column": next(iter(source_cols)),
        "true_label": str(conds["notna"]["value"]).strip(),
        "false_label": str(conds["isna"]["value"]).strip(),
    }


def _condition_columns_of(node: Any) -> List[str]:
    """取条件节点引用的列（单条件或复合条件）。"""
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("column"), str) and value["column"]:
                found.append(str(value["column"]))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return list(dict.fromkeys(found))


def _filter_conditions(step: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    conds = step.get("conditions")
    if isinstance(conds, list):
        return [c for c in conds if isinstance(c, dict)]
    return None  # 顶层单条件形态不参与改写


def _normalize_binary_rate(
    steps: List[Dict[str, Any]], warnings: List[str]
) -> List[Dict[str, Any]]:
    """把「判空打标 + 分组计数 / 条件占比」纠正为直接产出比率的 ``rate``。

    LLM 常把「成交价为空=未成交，不为空=成交，统计成交率」写成
    ``assign`` + ``count_rows`` 或 ``assign`` + ``pct(condition)``：既没有
    正确的比率口径，又会在清洗填充缺失后把两类合并成一类。这里做**确定性**
    纠偏，不依赖模型是否遵守提示词，旧规则重跑也能得到正确结果。
    """
    for i, step in enumerate(steps):
        candidate = _binary_rate_candidate(step)
        if candidate is None:
            continue
        for j in range(i + 1, len(steps)):
            later = steps[j]
            if not isinstance(later, dict) or later.get("op") not in ("count_rows", "pct"):
                continue
            if later["op"] == "pct" and later.get("scope") != "condition":
                continue
            bys = [str(c) for c in as_list(later.get("by")) if str(c).strip()]
            if later["op"] == "count_rows":
                if candidate["target"] not in bys:
                    continue
                group_by = [c for c in bys if c != candidate["target"]]
                alias = f"{candidate['true_label']}率"
            else:
                if candidate["target"] not in _condition_columns_of(later.get("condition")):
                    continue
                group_by = []
                alias = str(later.get("alias") or f"{candidate['true_label']}率").strip()
            # 后续步骤若还引用打标列，则不改写，避免破坏用户自定义链路
            referenced = False
            for k in range(j + 1, len(steps)):
                refs = contract.step_refs(steps[k]) or []
                if candidate["target"] in [str(r) for r in refs]:
                    referenced = True
                    break
            if referenced:
                continue
            rate_step: Dict[str, Any] = {
                "op": "rate",
                "column": candidate["column"],
                "condition": {"column": candidate["column"], "operator": "notna"},
                "true_label": candidate["true_label"],
                "false_label": candidate["false_label"],
                "alias": alias,
            }
            if alias == f"{candidate['false_label']}率":
                rate_step["numerator"] = "miss"
            if group_by:
                rate_step["by"] = group_by
            warnings.append(
                f"steps[{i}](assign)+steps[{j}]({later['op']}) 已归一为 rate："
                f"按 {candidate['column']} 判空分「{candidate['true_label']}/"
                f"{candidate['false_label']}」并直接产出比率与两类条数，"
                "避免清洗填充缺失把两类合并"
            )
            kept = [s for idx, s in enumerate(steps) if idx not in (i, j)]
            kept.insert(i, rate_step)   # i < j，移除 j 不影响 i 之前的顺序
            return kept
    return steps


def _strip_judgement_filters(
    steps: List[Dict[str, Any]], warnings: List[str]
) -> List[Dict[str, Any]]:
    """删除把「分类判据」误当筛选条件的判空条件，保证分母保留两类样本。

    两类危险形态（实测 LLM 均产生过）：
    1. ``filter(成交价 isna)`` 出现在 ``rate(成交价)`` 之前 → 分母被截断，
       命中类恒为 0；
    2. 两个互相矛盾的 ``filter``（同列 isna + notna）→ 结果必然 0 行。
    只移除「列 == 判据列」的判空条件；「小区不为空」这类与判据无关的
    真实筛选条件完整保留。
    """
    protected = set(judgement_columns(steps))
    polarity: Dict[str, set] = {}
    for step in steps:
        if not isinstance(step, dict) or step.get("op") != "filter":
            continue
        for cond in _filter_conditions(step) or []:
            if cond.get("operator") in ("isna", "notna") and cond.get("column"):
                polarity.setdefault(str(cond["column"]), set()).add(cond["operator"])
    protected |= {col for col, ops in polarity.items() if len(ops) > 1}
    if not protected:
        return steps

    out: List[Dict[str, Any]] = []
    removed = 0
    for step in steps:
        if isinstance(step, dict) and step.get("op") == "filter":
            conds = _filter_conditions(step)
            if conds is None:
                out.append(step)
                continue
            kept = [
                c for c in conds
                if not (str(c.get("column")) in protected
                        and c.get("operator") in ("isna", "notna"))
            ]
            if len(kept) == len(conds):
                out.append(step)
                continue
            removed += len(conds) - len(kept)
            if kept:
                new_step = dict(step)
                new_step["conditions"] = kept
                out.append(new_step)
        else:
            out.append(step)
    if removed:
        warnings.append(
            f"已移除 {removed} 个针对判据列的判空筛选条件（"
            f"{', '.join(sorted(protected))}）：分类判据不是筛选条件，"
            "保留两类样本才能算出正确分母"
        )
    # 矛盾 filter 去掉判空条件后常剩下完全相同的筛选条件，合并去重
    merged: List[Dict[str, Any]] = []
    for step in out:
        if (merged and isinstance(step, dict) and step.get("op") == "filter"
                and isinstance(merged[-1], dict) and merged[-1].get("op") == "filter"
                and merged[-1].get("conditions") == step.get("conditions")
                and merged[-1].get("logic", "and") == step.get("logic", "and")):
            continue
        merged.append(step)
    return merged


def _fold_rate_tail_pct(
    steps: List[Dict[str, Any]], warnings: List[str]
) -> List[Dict[str, Any]]:
    """折叠 ``rate`` 之后误加的 ``pct(condition)``。

    LLM 会把 rate 的 alias 当成「状态列」，再对它做 pct，得到「0/1 = 0%」。
    rate 的结果本就是汇总表，其后不应再对率列做条件占比；我们把 pct 的
    alias 提升为 rate 的目标率名，并按标签修正方向（未成交率 → miss）。
    """
    rate_idx = next(
        (i for i, s in enumerate(steps)
         if isinstance(s, dict) and s.get("op") == "rate"), None
    )
    if rate_idx is None:
        return steps
    rate = steps[rate_idx]
    t = str(rate.get("true_label") or "命中")
    f = str(rate.get("false_label") or "未命中")
    fixed_cols = {f"{t}数", f"{f}数", "总数", f"{t}{f}比"}
    outputs = fixed_cols | {str(rate.get("alias") or f"{t}率")}
    drop: set = set()
    new_alias = ""
    for i in range(rate_idx + 1, len(steps)):
        s = steps[i]
        if not isinstance(s, dict) or s.get("op") != "pct" or s.get("scope") != "condition":
            continue
        cols = _condition_columns_of(s.get("condition"))
        if cols and all(c in outputs for c in cols):
            drop.add(i)
            cand = str(s.get("alias") or "").strip()
            if cand and cand not in fixed_cols:
                new_alias = cand
    if not drop:
        return steps
    updated = dict(rate)
    if new_alias:
        updated["alias"] = new_alias
        if f and f in new_alias:
            updated["numerator"] = "miss"
        elif t and t in new_alias:
            updated["numerator"] = "hit"
    warnings.append(
        f"已折叠 rate 之后的 {len(drop)} 个 pct(condition)：rate 的结果表已是"
        "比率汇总，不能再对率列做条件占比；目标率名已提升为 "
        f"{updated.get('alias', '')}"
    )
    return [updated if i == rate_idx else s
            for i, s in enumerate(steps) if i not in drop]


_RESULT_OPS = frozenset({"rate", "pct", "count_rows", "aggregate", "pivot"})


_NEGATIVE_HINTS = ("未", "无", "不", "非", "流拍", "失败", "缺失", "空", "弃", "退")


def _fix_rate_direction_only(
    steps: List[Dict[str, Any]], rule: Dict[str, Any],
    warnings: List[str], original: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """意图不足以编译骨架、但目标率明确时的兜底。

    - 已有 rate：只修方向（numerator / alias），不猜判据列；
    - 明显错误链路（矛盾判空 filter / 判空筛选但完全没有结果算子）：
      用规则自身出现过的判据列补一个 rate，避免产出 0 行或只有明细。
    """
    target = _rate_target(rule)
    if target is None:
        return steps
    work = list(steps)
    rate_positions = [i for i, s in enumerate(work)
                      if isinstance(s, dict) and s.get("op") == "rate"]
    if rate_positions:
        for i in rate_positions:
            step = work[i]
            t = str(step.get("true_label") or "命中")
            f = str(step.get("false_label") or "未命中")
            alias = target["alias"]
            if alias in {f"{t}数", f"{f}数", "总数", f"{t}{f}比"}:
                continue
            label = target["target_label"]
            num = "miss" if label == f else ("hit" if label == t else None)
            if num is None:
                continue
            if (str(step.get("alias") or "") == alias
                    and str(step.get("numerator") or "hit") == num):
                continue
            updated = dict(step)
            updated["alias"] = alias
            updated["numerator"] = num
            warnings.append(
                f"rate 方向按需求「{alias}」修正为 {num}（未改动判据列与筛选条件）"
            )
            work[i] = updated
        return work

    # 救援：从「原始规则」里找判据列与矛盾形态（清洗后的 steps 已移除判空条件）
    polarity: Dict[str, set] = {}
    judge_cols = set(judgement_columns(original))
    has_result = any(isinstance(s, dict) and str(s.get("op")) in _RESULT_OPS
                     for s in original)
    for step in original:
        if not isinstance(step, dict) or step.get("op") != "filter":
            continue
        for cond in _filter_conditions(step) or []:
            if cond.get("operator") in ("isna", "notna") and cond.get("column"):
                col = str(cond["column"])
                judge_cols.add(col)
                polarity.setdefault(col, set()).add(cond["operator"])
    contradictory = {c for c, ops in polarity.items() if len(ops) > 1}
    if not judge_cols or (not contradictory and has_result):
        return work

    labels: Dict[str, str] = {}
    for step in original:
        if not isinstance(step, dict) or step.get("op") != "assign":
            continue
        for item in step.get("rules") or []:
            if not isinstance(item, dict):
                continue
            cond = item.get("condition") or {}
            if cond.get("operator") in ("isna", "notna") and isinstance(item.get("value"), str):
                labels[str(cond["operator"])] = item["value"]
    t = labels.get("notna") or "命中"
    f = labels.get("isna") or "未命中"
    if t == f:
        f = f"非{t}"
    col = sorted(contradictory or judge_cols)[0]
    alias = target["alias"]
    if alias in {f"{t}数", f"{f}数", "总数", f"{t}{f}比"}:
        return work
    label = target["target_label"]
    if label == f:
        num = "miss"
    elif label == t:
        num = "hit"
    else:
        num = "miss" if label.startswith(_NEGATIVE_HINTS) else "hit"
    rate_step = {
        "op": "rate", "column": col,
        "condition": {"column": col, "operator": "notna"},
        "true_label": t, "false_label": f,
        "alias": alias, "numerator": num, "denominator": "total",
    }
    warnings.append(
        f"比率需求「{alias}」缺少结果算子，已按规则中的判据列 {col} 补全 rate"
        f"（{num}/total）"
    )
    return work + [rate_step]


def _apply_rate_intent(
    steps: List[Dict[str, Any]], rule: Dict[str, Any],
    columns: List[str], warnings: List[str],
    original: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """用确定性意图修正比率链路的方向与缺失算子（编译层兜底）。

    与 ``rules.apply_intent_plan`` 的分工：规则生成层直接替换 LLM 的分析链路；
    这里只处理「绕过生成器」的规则（手填 JSON、规则库旧规则、直接 /execute），
    且仅在检测到明显缺口时动手：缺结果算子、方向与目标不符、pct 应升级为 rate。
    骨架本身已是正确形态时保持不动，保证幂等。
    """
    spec = _rate_spec(rule, columns)
    if spec is None:
        return _fix_rate_direction_only(
            steps, rule, warnings, original if original is not None else steps
        )
    work = [dict(s) if isinstance(s, dict) else s for s in steps]
    has_result = any(isinstance(s, dict) and str(s.get("op")) in _RESULT_OPS for s in work)
    if not has_result:
        warnings.append(
            f"比率需求未产出结果算子，已按意图补全 rate（{spec.alias}）"
        )
        work.append(spec.rate_step())
        return work

    rate_positions = [i for i, s in enumerate(work)
                      if isinstance(s, dict) and s.get("op") == "rate"]
    if not rate_positions:
        for i, s in enumerate(work):
            if (isinstance(s, dict) and s.get("op") == "pct"
                    and s.get("scope") == "condition"
                    and spec.judge_column in _condition_columns_of(s.get("condition"))):
                warnings.append(
                    f"条件占比已升级为 rate：补齐两类条数并按「{spec.alias}」口径计算"
                )
                work[i] = spec.rate_step()
                rate_positions = [i]
                break

    for i in rate_positions:
        cur = work[i]
        by_cols = [c for c in spec.by if c in columns]
        cur_by = [c for c in as_list(cur.get("by")) if isinstance(c, str)]
        if (str(cur.get("alias") or "") == spec.alias
                and str(cur.get("numerator") or "hit") == spec.numerator
                and str(cur.get("column") or "") == spec.judge_column
                and (not by_cols or cur_by == by_cols)):
            continue
        updated = dict(cur)
        updated.update({
            "column": spec.judge_column,
            "condition": dict(spec.condition),
            "true_label": spec.true_label,
            "false_label": spec.false_label,
            "alias": spec.alias,
            "numerator": spec.numerator,
            "denominator": spec.denominator,
        })
        if by_cols:
            updated["by"] = by_cols
        warnings.append(
            f"rate 已按需求方向修正为「{spec.alias}」"
            f"（{spec.numerator}/{spec.denominator}）"
        )
        work[i] = updated
    return work


def normalize_rate_pipeline(
    steps: List[Dict[str, Any]], rule: Dict[str, Any],
    columns: List[str], warnings: List[str]
) -> List[Dict[str, Any]]:
    """比率链路归一的统一入口（顺序固定，结果幂等）。

    ``original`` 保留清洗前的步骤，供低置信度意图的救援分支识别判据列
    与「矛盾判空 filter」形态（这些条件在后几步会被移除）。
    """
    original = [dict(s) if isinstance(s, dict) else s for s in steps]
    work = _normalize_binary_rate(steps, warnings)
    work = _strip_judgement_filters(work, warnings)
    work = _fold_rate_tail_pct(work, warnings)
    work = _apply_rate_intent(work, rule, columns, warnings, original)
    return work


# ------------------------------------------------------------
# 校验（按注册表，错误聚合）
# ------------------------------------------------------------
def validate_steps(steps: Any, columns: List[str]) -> List[str]:
    """逐步骤按契约校验，返回全部错误（空列表表示通过）。"""
    errors: List[str] = []
    if not isinstance(steps, list):
        return ["steps 必须是数组"]
    # 累积已知列：派生列、聚合结果、日期分量等可被后续步骤引用
    known_sets: List[set] = [set(columns)]
    for step in steps:
        cur = set(known_sets[-1])
        if isinstance(step, dict):
            cur.update(contract.step_outputs(step))
        known_sets.append(cur)
    for idx, step in enumerate(steps):
        tag = f"steps[{idx}]"
        if not isinstance(step, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        op = step.get("op")
        if get_op(str(op)) is None:
            errors.append(f"{tag}.op 非法算子 {op!r}，可选 {op_names()}")
            continue
        contract.validate_step(step, known_sets[idx], tag, errors)
    return errors


# ------------------------------------------------------------
# 编译产物
# ------------------------------------------------------------
@dataclass
class ExecutablePlan:
    version: str
    steps: List[Dict[str, Any]]
    columns_in: List[str]
    columns_out: List[str]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "steps": self.steps,
            "columns_in": self.columns_in,
            "columns_out": self.columns_out,
            "warnings": self.warnings,
        }


def compile_rule(rule: Dict[str, Any], columns: List[str]) -> ExecutablePlan:
    """把宽松规则编译为可执行计划（列对齐 + 契约校验）。"""
    working = dict(rule)
    working, warnings = reconcile_rule_columns(working, columns)
    steps = working.get("steps") or []
    if isinstance(steps, list) and all(isinstance(s, dict) for s in steps):
        steps = normalize_rate_pipeline(steps, working, columns, warnings)
        working["steps"] = steps
    errors = validate_steps(steps, columns)
    if errors:
        raise RuleValidationError(errors)
    columns_out = list(columns)
    for step in steps:
        if isinstance(step, dict):
            columns_out.extend(contract.step_outputs(step))
    # 去重保序
    seen: set = set()
    out_cols = [c for c in columns_out if not (c in seen or seen.add(c))]
    return ExecutablePlan(
        version=str(working.get("version", "2.0")),
        steps=steps,
        columns_in=list(columns),
        columns_out=out_cols,
        warnings=warnings,
    )


__all__ = [
    "ExecutablePlan",
    "compile_rule",
    "normalize_rate_pipeline",
    "reconcile_rule_columns",
    "validate_steps",
]

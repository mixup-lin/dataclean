# -*- coding: utf-8 -*-
"""意图编译层：把「比率类」「明细/清洗类」需求编译为确定性规则骨架。

背景
----
LLM 输出是不稳定的：同一个「统计未成交率」描述，实测会生成四种互不相同的
规则（正确的 ``rate``、两个相互矛盾的 ``filter``、``filter(isna)+rate+pct``、
``assign+count_rows``）；「提取区县为鼓楼区的数据，成交价空值置为0，其他字段
空值置为null」又会被写成 ``summary + count_rows``。提示词只是**软约束**，
不能作为正确性的唯一保障。

本模块把两类语义提升为程序可判定的一等公民：

* 比率：判据列、两类标签、目标方向、分母口径、筛选/日期/分组 → ``rate`` 骨架；
* 明细/清洗：逐行结果形态、筛选条件、按列空值填充、保留原始空值 →
  ``filter`` + 显式 ``fill`` 骨架，并关闭会把空值提前填掉的全局填充。

「LLM 生成规则」因此降级为一条可替换的输入，而不再是结果的决定者。

设计原则
--------
1. **不臆造列名**：所有列引用必须能在真实列清单或使用者提供的别名映射里解析到；
   解析不到的条件直接丢弃并记 note，绝不猜测。
2. **高置信度才触发**：命中分析词时优先比率骨架，命中明细动词且无分析词时
   使用明细骨架；信息不足返回 ``None``，完整保留原有规则。
3. **可审计**：替换 / 纠偏的每一步都产生 warning，写入生成 meta 与执行摘要。
4. **零循环依赖**：只依赖标准库，可被 ``ops.compiler`` 与 ``rules`` 共同引用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ops.constants import DATE_PART_LABELS

# ------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------
_RATE_SUFFIX = ("率", "占比", "比例", "百分比")
_RATE_WORDS = ("率", "占比", "比例", "百分比", "之比", "比值")

# 生成规则时允许保留的「纯清洗 / 整理」步骤；所有会塑造结果语义的步骤
# （filter / date_filter / rate / pct / count_rows / assign / aggregate / pivot ...）
# 一律由意图骨架统一生成，避免 LLM 的错误链路混进来。
_PRESERVED_OPS = frozenset({
    "dedupe", "dedupe_fuzzy", "sort", "select", "head", "tail", "sample",
    "mask_pii", "text_normalize", "convert_unit", "category_map", "cast",
    "fill", "string", "split", "concat", "assert", "profile", "dropna",
    "replace", "clip", "outlier", "normalize", "date_parse", "describe",
    "drop_columns", "rename", "bin", "date_part",
})


@dataclass
class RateIntent:
    """一个高置信度的比率意图，可直接编译成规则步骤。"""

    judge_column: str
    condition: Dict[str, Any]
    true_label: str
    false_label: str
    target_label: str
    alias: str
    numerator: str = "hit"           # hit | miss
    denominator: str = "total"       # total | hit | miss
    filters: List[Dict[str, Any]] = field(default_factory=list)
    date_filters: List[Dict[str, Any]] = field(default_factory=list)
    by: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_intent: str = ""

    def rate_step(self) -> Dict[str, Any]:
        step: Dict[str, Any] = {
            "op": "rate",
            "column": self.judge_column,
            "condition": dict(self.condition),
            "true_label": self.true_label,
            "false_label": self.false_label,
            "alias": self.alias,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }
        if self.by:
            step["by"] = list(self.by)
        return step

    def steps(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if self.filters:
            out.append({"op": "filter", "conditions": list(self.filters), "logic": "and"})
        out.extend(dict(s) for s in self.date_filters)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "judge_column": self.judge_column,
            "condition": self.condition,
            "true_label": self.true_label,
            "false_label": self.false_label,
            "target_label": self.target_label,
            "alias": self.alias,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "filters": self.filters,
            "date_filters": self.date_filters,
            "by": self.by,
            "notes": self.notes,
        }


# ------------------------------------------------------------
# 文本与列名工具
# ------------------------------------------------------------
def normalize_text(text: Any) -> str:
    """压平换行/全角空白，避免 LLM 回显的 intent 出现断字（如「目 标」）。"""
    raw = str(text or "").replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", raw).strip()


_SECTION_RE = re.compile(r"(筛选|判断|判定|目标|描述|条件|统计需求|口径)\s*[:：]")


def _sections(text: str) -> Tuple[Dict[str, List[str]], str]:
    marks = list(_SECTION_RE.finditer(text))
    result: Dict[str, List[str]] = {}
    for idx, mark in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip(" ，,；;。.、")
        if body:
            result.setdefault(mark.group(1), []).append(body)
    prefix = text[:marks[0].start()] if marks else text
    return result, prefix.strip(" ，,；;。.、")


def _clean_label(text: str) -> str:
    value = re.sub(r"^[为是=:：\-—>\s]+|[，,；;。.、\s]+$", "", str(text or "")).strip()
    # 「为空算未成交 / 为空记为流拍」这类口语连接词不是标签的一部分
    return re.sub(r"^(?:算作|算|记为|记做|记作|视为|判为|判定为|即|则|为|是)+", "", value).strip() or value


def _plain(name: str) -> str:
    """去掉括号/单位/空白后用于列名比较。"""
    return re.sub(r"[\s\(\)（）\[\]【】·・/\\_-]+", "", str(name or "")).lower()


def resolve_column(
    word: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
) -> str:
    """把描述里的词解析为真实列名；解析不唯一时返回空串（绝不猜测）。

    优先级：精确 > 使用者别名映射 > 归一化精确 > 唯一前缀/包含 > 最短候选。
    """
    token = str(word or "").strip()
    if not token:
        return ""
    known = [str(c) for c in columns]
    if token in known:
        return token
    if aliases and token in aliases and str(aliases[token]) in known:
        return str(aliases[token])
    norm = _plain(token)
    if not norm:
        return ""
    exact = [c for c in known if _plain(c) == norm]
    if len(exact) == 1:
        return exact[0]
    prefix = [c for c in known if _plain(c).startswith(norm)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        shortest = min(prefix, key=lambda c: len(_plain(c)))
        # 只有「描述词 + 单位/后缀」这种明显形态才接受最短候选
        if len(_plain(shortest)) <= len(norm) + 4:
            return shortest
        return ""
    contains = [c for c in known if norm in _plain(c)]
    if len(contains) == 1:
        return contains[0]
    return ""


# ------------------------------------------------------------
# 各段解析
# ------------------------------------------------------------
# 「成交价为空-未成交」「A不为空=B」「状态为null→流拍」
_JUDGE_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:的值)?\s*"
    r"(?P<state>不为空|非空|不为空值|不为null|不缺失|有值|为空|空值|为null|缺失|没值|无值)\s*"
    r"[-—=:：>为\s]*?(?P<label>[^\s,，；;：:（）()\-—=>]+)"
)

# 目标：「统计未成交率（未成交/总数）」「计算成交占比」「求空值率」
_TARGET_RE = re.compile(
    r"(?:统计|计算|求|得到|得出|输出|分析|看|展示)?\s*"
    r"(?P<alias>[\u4e00-\u9fa5A-Za-z0-9_]{1,12}?(?:率|占比|比例|百分比))"
)

_DENOMINATOR_RE = re.compile(r"[（(]\s*([^）)]{1,30}?)\s*[)）]")

_YEAR_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:在|于|是|为)?\s*"
    r"(?P<year>(?:19|20)\d{2})\s*年"
)
_DATE_RANGE_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:在|从|介于)?\s*"
    r"(?P<start>(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})\s*"
    r"(?:至|到|~|—|-|－)\s*(?P<end>(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})"
)
_PAIR_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:[:：]|==|=|等于|是|为)\s*"
    r"(?P<val>[^\s,，；;：:（）()]+)"
)
_NOTNA_RE = re.compile(r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:不为空|非空|有值)")
_ISNA_RE = re.compile(r"(?P<col>[^\s,，；;：:（）()]+?)\s*(?:为空|空值|缺失|无值)")
_GROUP_RE = re.compile(
    r"(?:按|按照|根据|每个|各)\s*(?P<col>[^\s,，；;：:（）()]+?)"
    r"(?=\s*(?:分组|统计|计算|维度|的|，|,|；|;|$))"
)


_STOP_WORDS = ("的数据", "数据", "占比", "率", "统计", "筛选", "按", "分组", "判断", "描述")

# 「承前省略列名」：成交价为空算未成交，不为空算成交 —— 第二分支不再重复列名
_BARE_JUDGE_RE = re.compile(
    r"(?P<state>不为空|非空|不为空值|有值|为空|空值|缺失|没值|无值)\s*"
    r"[-—=:：>为算记视为\s]*?(?P<label>[^\s,，；;：:（）()\-—=>]+)"
)


def _state_key(state: str) -> str:
    return "isna" if state in ("为空", "空值", "为null", "缺失", "没值", "无值") else "notna"


def _parse_judge(
    section: str, require_pair: bool = False
) -> Tuple[str, Dict[str, str]]:
    """返回 (判据列词, {isna|notna: 标签})。

    ``require_pair`` 用于「从全文扫描」的场景：必须同时出现为空 / 不为空两条
    互补判据才采纳，避免把「小区不为空」这类普通筛选误当作分类判据；同时兼容
    中文常见的承前省略（第二分支不重复列名）。
    """
    labels: Dict[str, str] = {}
    col_word = ""
    tagged = list(_JUDGE_RE.finditer(section))
    if require_pair:
        # 全文扫描：「不为空」若被 col 吃掉一个「不」字就会解析成「为空」，
        # 因此标签一律由不带列名的 _BARE_JUDGE_RE 判定，_JUDGE_RE 只提供列名。
        for match in _BARE_JUDGE_RE.finditer(section):
            label = _clean_label(match.group("label"))
            key = _state_key(match.group("state"))
            if label and not any(w in label for w in _STOP_WORDS):
                labels.setdefault(key, label)
        for match in tagged:
            col = _clean_label(match.group("col"))
            if col and not col.endswith(("不", "非", "没", "无")):
                col_word = col
                break
    else:
        for match in tagged:
            col = _clean_label(match.group("col"))
            label = _clean_label(match.group("label"))
            if not col or not label or any(w in label for w in _STOP_WORDS):
                continue
            col_word = col_word or col
            labels[_state_key(match.group("state"))] = label
        for match in _BARE_JUDGE_RE.finditer(section):
            key = _state_key(match.group("state"))
            if key in labels:
                continue
            label = _clean_label(match.group("label"))
            if label and not any(w in label for w in _STOP_WORDS):
                labels[key] = label
    if require_pair and set(labels) != {"isna", "notna"}:
        return "", {}
    return col_word, labels


def _split_conditions(section: str) -> List[str]:
    return [
        part.strip(" ，,；;。.、")
        for part in re.split(r"[,，、;；]|并且|且|以及|同时", section)
        if part.strip(" ，,；;。.、")
    ]


_LEAD_VERBS_RE = re.compile(
    r"^(?:统计|计算|求|筛选|提取|取出|获取|查询|查出|找出|选取|选出|"
    r"列出|导出|保留|清洗|整理|描述|分析|其中|并且|且|同时|以及)+"
)


def _bare_value_column(
    chunk: str,
    columns: Sequence[str],
    value_hints: Optional[Dict[str, Sequence[str]]],
) -> Tuple[str, str]:
    """裸词精确命中唯一一列的采样值时，返回 (列名, 裸词)，否则 ("", 裸词)。"""
    stripped = _LEAD_VERBS_RE.sub("", chunk).strip(" ，,；;。.、")
    bare = re.sub(r"(的数据|数据|的记录|记录|的)$", "", stripped).strip(" ，,。.、")
    if not bare or len(bare) > 20 or any(ch in bare for ch in "=:：为是率占比置设填"):
        return "", bare
    hints = value_hints or {}
    hits = [c for c in columns if bare in [str(v).strip() for v in hints.get(c, ())]]
    return (hits[0], bare) if len(hits) == 1 else ("", bare)


def _parse_filters(
    sections: Dict[str, List[str]],
    prefix: str,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]],
    notes: List[str],
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """解析筛选条件 / 日期条件 / 分组维度。"""
    chunks: List[str] = []
    for key in ("筛选", "条件"):
        for body in sections.get(key, []):
            chunks.extend(_split_conditions(body))
    # 无「筛选」标签时，前缀里的「市是福州」「鼓楼区数据（按采样值绑定）」也纳入
    for body in _split_conditions(prefix):
        hint_col, _ = _bare_value_column(body, columns, value_hints)
        if (hint_col or _PAIR_RE.search(body) or _YEAR_RE.search(body)
                or _DATE_RANGE_RE.search(body)
                or _NOTNA_RE.fullmatch(_LEAD_VERBS_RE.sub("", body).strip())
                or _ISNA_RE.fullmatch(_LEAD_VERBS_RE.sub("", body).strip())):
            chunks.append(body)

    filters: List[Dict[str, Any]] = []
    dates: List[Dict[str, Any]] = []
    by: List[str] = []
    for chunk in chunks:
        chunk = _LEAD_VERBS_RE.sub("", chunk).strip(" ，,；;。.、")
        if not chunk:
            continue
        # 目标语句（「统计成交价为空率」）不是筛选条件，不参与列名解析
        if any(word in chunk for word in ("率", "占比", "比例", "百分比")):
            continue
        # 独立的「小区不为空 / 某列为空」才是筛选；带标签的判据由判断段处理
        standalone_empty = bool(_NOTNA_RE.fullmatch(chunk) or _ISNA_RE.fullmatch(chunk))
        if not standalone_empty and any(
            key in chunk for key in (
                "为空", "不为空", "非空", "空值", "缺失", "算", "记为", "视为",
                "置为", "设为", "填为", "置空", "填充",
            )
        ):
            continue
        rng = _DATE_RANGE_RE.search(chunk)
        if rng:
            col = resolve_column(rng.group("col"), columns, aliases)
            if col:
                dates.append({"op": "date_filter", "column": col,
                              "start": rng.group("start"), "end": rng.group("end")})
            else:
                notes.append(f"日期区间条件「{chunk}」未匹配到真实列，已忽略")
            continue
        year = _YEAR_RE.search(chunk)
        if year:
            col = resolve_column(year.group("col"), columns, aliases)
            if col:
                dates.append({"op": "date_filter", "column": col,
                              "year": int(year.group("year"))})
            else:
                notes.append(f"年份条件「{chunk}」未匹配到真实列，已忽略")
            continue

        group = _GROUP_RE.search(chunk)
        if group:
            col = resolve_column(group.group("col"), columns, aliases)
            if col and col not in by:
                by.append(col)
            continue

        notna = _NOTNA_RE.fullmatch(chunk)
        isna = _ISNA_RE.fullmatch(chunk) if not notna else None
        if notna or isna:
            m = notna or isna
            col = resolve_column(m.group("col"), columns, aliases)
            if col:
                filters.append({"column": col,
                                "operator": "notna" if notna else "isna"})
            else:
                notes.append(f"判空条件「{chunk}」未匹配到真实列，已忽略")
            continue

        pair = _PAIR_RE.search(chunk)
        if pair:
            col = resolve_column(pair.group("col"), columns, aliases)
            value = re.sub(r"(的数据|数据|的记录|记录|的)$", "",
                           _clean_label(pair.group("val"))).strip()
            if col and value:
                filters.append({"column": col, "operator": "eq", "value": value})
            else:
                notes.append(f"筛选条件「{chunk}」未匹配到真实列或取值，已忽略")
            continue
        # 「提取鼓楼区数据」这类省略列名的写法：只有当裸词精确命中**唯一一列**
        # 的采样值时才绑定，多列命中直接放弃，绝不靠语义猜测。
        hint_col, bare = _bare_value_column(chunk, columns, value_hints)
        if hint_col:
            filters.append({"column": hint_col, "operator": "eq", "value": bare})
            notes.append(f"筛选词「{bare}」按采样值绑定到列 {hint_col}")
            continue
        notes.append(f"未识别的筛选片段「{chunk}」，已忽略")

    # 目标段里也可能出现「按 X 分组统计」
    for body in sections.get("目标", []) + sections.get("统计需求", []):
        for m in _GROUP_RE.finditer(body):
            col = resolve_column(m.group("col"), columns, aliases)
            if col and col not in by:
                by.append(col)
    return filters, dates, by


def _target_alias(sections: Dict[str, List[str]], full_text: str) -> Tuple[str, str]:
    """返回 (目标列名, 含括号口径的原文)。

    优先取「目标 / 统计需求」段；没有段标签时在全文中找，兼容
    「筛选市：福州，统计成交率」这类简写。
    """
    text = ""
    for key in ("目标", "统计需求"):
        if sections.get(key):
            text = " ".join(sections[key])
            break

    def strip_group_prefix(value: str) -> str:
        # 「按区县分组统计未成交率」→「统计未成交率」：分组前缀不是率名，
        # 若不剥掉会把整句当成 alias，进而误判目标方向（未成交→命中）。
        return re.sub(
            r"(?:按|按照|根据|每个|各)\s*[^\s,，；;：:]{1,12}?"
            r"(?:分组|维度)?(?=(?:统计|计算|求|分析|的))",
            "", value,
        )

    match = _TARGET_RE.search(strip_group_prefix(text)) if text else None
    if not match:
        text = strip_group_prefix(full_text)
        match = _TARGET_RE.search(text)
    if not match:
        return "", text
    return match.group("alias"), text


def parse_rate_intent(
    intent: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Optional[RateIntent]:
    """解析比率意图；非比率需求或信息不足时返回 ``None``。

    高置信度条件：描述里出现「率/占比/比例」目标，且判据列能解析到真实列。
    """
    text = normalize_text(intent)
    if not text or not any(word in text for word in _RATE_WORDS):
        return None
    sections, prefix = _sections(text)
    alias, goal_text = _target_alias(sections, text)
    if not alias:
        return None
    notes: List[str] = []

    # 判据段：优先「判断 / 判定 / 口径」段；无段标签时从全文找「X为空-Y」
    judge_text = " ".join(
        sections.get("判断", []) + sections.get("判定", []) + sections.get("口径", [])
    )
    col_word, labels = _parse_judge(judge_text) if judge_text else _parse_judge(text, True)
    condition = {"column": "", "operator": "notna"}
    if not col_word:
        # 「成交价为空率 / 成交价缺失率 / 成交价非空占比」
        m = re.search(
            r"(?P<col>[^\s,，；;：:（）()]+?)\s*"
            r"(?P<state>为空|空值|缺失|不为空|非空|有值)\s*(?:率|占比|比例|百分比)",
            goal_text,
        )
        if m:
            col_word = _clean_label(
                re.sub(r"^(?:统计|计算|求|筛选|描述|目标|得出|得到|输出|分析)+", "",
                       m.group("col"))
            )
            state = m.group("state")
            isna = state in ("为空", "空值", "缺失")
            condition["operator"] = "isna" if isna else "notna"
            label = re.sub(r"(率|占比|比例|百分比)$", "", alias).strip() or "命中"
            if len(label) > 4:  # 「成交价不为空」是目标描述，不适合当结果列名
                label = "命中"
            labels.setdefault("isna" if isna else "notna", label)
    if not col_word:
        return None  # 低置信度：交给 LLM / 编译层形态兜底，绝不猜测列

    judge_column = resolve_column(col_word, columns, aliases)
    if not judge_column:
        return None
    condition["column"] = judge_column

    # 「命中类」= 满足 condition 的那一类；condition 既可能是 notna，也可能是 isna
    hit_key = "isna" if condition["operator"] == "isna" else "notna"
    miss_key = "notna" if hit_key == "isna" else "isna"
    true_label = _clean_label(labels.get(hit_key) or "") or "命中"
    false_label = _clean_label(labels.get(miss_key) or "") or "未命中"
    if true_label == false_label:
        false_label = f"非{true_label}"
    target_label = re.sub(r"(率|占比|比例|百分比)$", "", alias).strip() or true_label

    numerator = "miss" if target_label == false_label else "hit"
    denominator = "total"
    den_match = _DENOMINATOR_RE.search(goal_text)
    denominator_text = den_match.group(1) if den_match else ""
    if denominator_text:
        if any(w in denominator_text for w in ("总数", "全部", "所有", "总样本", "总体")):
            denominator = "total"
        elif true_label and true_label in denominator_text:
            # 「A/B」形式：主率仍按总数，两类比由 rate 的比值列给出
            notes.append(
                f"口径「{denominator_text}」按「{alias} = {target_label}数 / 总数」解释，"
                f"两类之比另由 {true_label}{false_label}比 列给出"
            )
        elif false_label and false_label in denominator_text:
            notes.append(
                f"口径「{denominator_text}」按「{alias} = {target_label}数 / 总数」解释，"
                f"两类之比另由 {true_label}{false_label}比 列给出"
            )
    if numerator == "miss":
        notes.append(
            f"目标方向为「{false_label}」：率 = {false_label}数 / 总数，"
            f"判空列 {judge_column} 的原始空值在执行前会还原"
        )

    filters, dates, by = _parse_filters(
        sections, prefix, columns, aliases, notes, value_hints
    )
    # 判据列不能同时出现在筛选条件里，否则分母会被截断成 0 或只剩一类
    removed = [f for f in filters
               if f.get("column") == judge_column and f.get("operator") in ("isna", "notna")]
    if removed:
        filters = [f for f in filters if f not in removed]
        notes.append(
            f"已从筛选条件中移除 {len(removed)} 个针对判据列 {judge_column} 的判空条件，"
            "保证分母保留全部两类样本"
        )
    return RateIntent(
        judge_column=judge_column,
        condition=condition,
        true_label=true_label,
        false_label=false_label,
        target_label=target_label,
        alias=alias,
        numerator=numerator,
        denominator=denominator,
        filters=filters,
        date_filters=dates,
        by=by,
        notes=notes,
        raw_intent=text,
    )


# ------------------------------------------------------------
# 明细 / 清洗意图：确定性替换 LLM 的汇总链路
# ------------------------------------------------------------
# 明细类动词；出现它们且没有分析类词时，结果必须是逐行明细。
_DETAIL_VERBS = (
    "提取", "取出", "获取", "查出", "查询", "找出", "列出", "导出",
    "选取", "选出", "保留", "清洗", "整理", "明细", "置为", "设为", "填充",
)
# 分析类词：出现任一个就交给比率 / 统计意图处理，明细骨架不介入。
_ANALYSIS_WORDS = (
    "率", "占比", "比例", "百分比", "统计", "汇总", "分组", "趋势", "排名",
    "透视", "相关", "分布", "比值", "之比", "条数", "几条", "多少个", "多少",
    "数量", "个数", "总计", "合计", "均值", "平均", "求和", "最大值", "最小值",
    "同比", "环比", "帕累托", "累计",
)

# 「成交价空值置为0」「其他字段空值置为null」
_FILL_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*"
    r"(?P<state>为空|空值|为null|为NULL|缺失|没值|无值)\s*"
    r"(?:置为|设为|填为|改为|替换为|赋值为|设置为|填充为|填|置|=|→|->)\s*"
    r"(?P<val>[^\s,，；;。]+)"
)
_BARE_NULL_RE = re.compile(
    r"(?:其他|其余|剩下|所有)?\s*(?:字段|列)?\s*"
    r"(?:空值|为空|缺失)\s*(?:置为|设为|填为|保留为|保持为)\s*"
    r"(?P<val>null|NULL|None|none|空|缺失)"
)
_NULL_WORDS = frozenset({"null", "none", "nan", "空", "缺失", "空值", ""})

_OTHER_COL_WORDS = ("其他字段", "其余字段", "其他列", "其余列", "其他", "其余")


def _fill_value(raw: str) -> Any:
    """把描述里的值转成合适的 Python 标量。"""
    text = str(raw or "").strip().strip("\"'“”")
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?", text):
        return float(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


@dataclass
class DetailIntent:
    """一个高置信度的明细 / 清洗意图，可直接编译成规则步骤。"""

    filters: List[Dict[str, Any]] = field(default_factory=list)
    date_filters: List[Dict[str, Any]] = field(default_factory=list)
    fills: List[Dict[str, Any]] = field(default_factory=list)
    preserve_nulls: bool = False
    notes: List[str] = field(default_factory=list)
    raw_intent: str = ""

    def steps(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if self.filters:
            out.append({"op": "filter", "conditions": list(self.filters), "logic": "and"})
        out.extend(dict(s) for s in self.date_filters)
        out.extend(dict(s) for s in self.fills)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filters": self.filters, "date_filters": self.date_filters,
            "fills": self.fills, "preserve_nulls": self.preserve_nulls,
            "notes": self.notes,
        }


def parse_detail_intent(
    intent: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Optional[DetailIntent]:
    """解析明细 / 清洗意图；分析类需求或信息不足时返回 ``None``。

    高置信度条件：出现明细类动词、没有分析类词，且至少能解析出一项
    筛选 / 日期 / 空值填充 / 保留空值的要求。
    """
    text = normalize_text(intent)
    if not text or not any(v in text for v in _DETAIL_VERBS):
        return None
    if any(w in text for w in _ANALYSIS_WORDS):
        return None

    notes: List[str] = []
    sections, prefix = _sections(text)
    filters, dates, _by = _parse_filters(
        sections, prefix, columns, aliases, notes, value_hints
    )

    fills: List[Dict[str, Any]] = []
    explicit: set = set()
    preserve_nulls = False
    for match in _FILL_RE.finditer(text):
        word = _clean_label(match.group("col"))
        value_text = match.group("val").strip()
        # 「其他字段空值置为null」不是针对某一列的具体填充
        if word and any(word == w or word.startswith(w) for w in _OTHER_COL_WORDS):
            if value_text.lower() in _NULL_WORDS:
                preserve_nulls = True
            else:
                others = [c for c in columns if c not in explicit]
                if others:
                    fills.append({"op": "fill", "columns": others,
                                  "method": "value", "value": _fill_value(value_text)})
                    notes.append(f"其余 {len(others)} 列空值置为 {value_text}")
            continue
        # 口语前缀不是列名：「其中成交价空值置为0」「然后把成交价空值填0」
        word = re.sub(
            r"^(?:其中|然后|接着|另外|并且|同时|以及|把|将|对|对于)+", "", word
        ).strip()
        col = resolve_column(word, columns, aliases) if word else ""
        if not col:
            notes.append(f"空值填充「{match.group(0)}」未匹配到真实列，已忽略")
            continue
        explicit.add(col)
        if value_text.lower() in _NULL_WORDS:
            preserve_nulls = True
            notes.append(f"{col} 空值保持为 null，未生成填充步骤")
        else:
            fills.append({"op": "fill", "columns": [col],
                          "method": "value", "value": _fill_value(value_text)})

    if _BARE_NULL_RE.search(text):
        preserve_nulls = True

    if not (filters or dates or fills or preserve_nulls):
        return None
    if preserve_nulls:
        notes.append(
            "需求要求保留原始空值：options.fill_missing 已设为 none，"
            "避免全局填充提前吃掉空值"
        )
    if fills:
        notes.append(
            "空值填充改为显式 steps.fill 表达，先关闭全局填充再按列填充，口径可审计"
        )
    return DetailIntent(
        filters=filters, date_filters=dates, fills=fills,
        preserve_nulls=preserve_nulls, notes=notes, raw_intent=text,
    )


# ------------------------------------------------------------
# 组合流程意图：去重 → 条件提取 → 时间趋势分析
# ------------------------------------------------------------
_COMPOSITE_RE = re.compile(
    r"去重.{0,60}(?:提取|筛选|选出).{0,120}(?:趋势|走势|分析|统计|汇总)",
    re.S,
)
_NUM_COND_RE = re.compile(
    r"(?P<col>[^\s,，；;：:（）()]+?)\s*"
    r"(?P<op>小于等于|不小于|不大于|大于等于|小于|低于|大于|高于|<=|>=|<|>)\s*"
    r"(?P<val>\d+(?:\.\d+)?)"
)
_NUM_OPERATORS = {
    "小于": "lt", "低于": "lt", "<": "lt",
    "大于": "gt", "高于": "gt", ">": "gt",
    "小于等于": "lte", "不大于": "lte", "<=": "lte",
    "大于等于": "gte", "不小于": "gte", ">=": "gte",
}


@dataclass
class CompositeIntent:
    """高置信度的多步骤组合意图（顺序由描述里的「然后/最后」决定）。"""

    dedupe_subset: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    logic: str = "or"
    date_column: str = ""
    value_column: str = ""
    parts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_intent: str = ""

    def steps(self) -> List[Dict[str, Any]]:
        period = f"{self.date_column}_月" if "month" in self.parts else f"{self.date_column}_年"
        out: List[Dict[str, Any]] = [
            {"op": "dedupe", "subset": list(self.dedupe_subset), "keep": "first"},
        ]
        if self.filters:
            out.append({
                "op": "filter", "conditions": list(self.filters), "logic": self.logic,
            })
        out.append({"op": "date_part", "column": self.date_column, "parts": list(self.parts)})
        out.append({
            "op": "aggregate", "by": [period], "column": [self.value_column],
            "func": ["mean"], "alias": [self.value_column],
        })
        out.append({"op": "sort", "by": [period], "ascending": [True]})
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dedupe_subset": self.dedupe_subset, "filters": self.filters,
            "logic": self.logic, "date_column": self.date_column,
            "value_column": self.value_column, "parts": self.parts,
            "notes": self.notes,
        }


def parse_composite_intent(
    intent: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Optional[CompositeIntent]:
    """解析「去重 → 提取 → 趋势」类组合需求；缺任一阶段就返回 None。"""
    text = normalize_text(intent)
    if not text or not _COMPOSITE_RE.search(text):
        return None
    notes: List[str] = []

    # 阶段 1：去重键（没写键就整行去重）
    subset: List[str] = []
    dedupe_key = re.search(r"按\s*([^\s,，；;。]{1,12})\s*去重", text)
    if dedupe_key:
        col = resolve_column(dedupe_key.group(1), columns, aliases)
        if col:
            subset = [col]
        else:
            notes.append(f"去重键「{dedupe_key.group(1)}」未匹配到真实列，改为整行去重")

    # 阶段 2：数值条件（「面积小于50或大于1000」→ 同一列 or 复合条件）。
    # 直接拿真实列名/别名去文本里定位，避免「提取出面积」这类前缀被吞进列名。
    filters: List[Dict[str, Any]] = []
    seen_conditions: Set[Tuple[str, str, Any]] = set()
    terms: List[Tuple[str, str]] = [(str(c), str(c)) for c in columns]
    for column in columns:
        # 「面积(㎡)」在口语里通常只写「面积」；补一个不带单位的别名项
        short = re.split(r"[（(]", str(column), maxsplit=1)[0].strip()
        if short and short != str(column):
            terms.append((short, str(column)))
    terms.extend(
        (str(label), str(target)) for label, target in (aliases or {}).items()
        if str(target) in {str(c) for c in columns}
    )
    for term, col in sorted(terms, key=lambda pair: -len(pair[0])):
        pattern = re.compile(
            re.escape(term)
            + r"\s*(?P<op>小于等于|不小于|不大于|大于等于|小于|低于|大于|高于|<=|>=|<|>)\s*"
            + r"(?P<val>\d+(?:\.\d+)?)"
        )
        for match in pattern.finditer(text):
            operator = _NUM_OPERATORS.get(match.group("op"))
            if not operator:
                continue
            raw = match.group("val")
            value: Any = float(raw) if "." in raw else int(raw)
            key = (col, operator, value)
            if key in seen_conditions:
                continue
            seen_conditions.add(key)
            filters.append({"column": col, "operator": operator, "value": value})

    # 「面积小于50或大于1000」后半句承前省略列名：补上同一列的第二个条件
    if filters:
        implied_col = filters[-1]["column"]
        for match in re.finditer(
            r"(?:或|或者|且|并且|and)\s*"
            r"(?P<op>小于等于|不小于|不大于|大于等于|小于|低于|大于|高于|<=|>=|<|>)\s*"
            r"(?P<val>\d+(?:\.\d+)?)",
            text,
            re.I,
        ):
            operator = _NUM_OPERATORS.get(match.group("op").lower())
            if not operator:
                continue
            raw = match.group("val")
            value: Any = float(raw) if "." in raw else int(raw)
            key = (implied_col, operator, value)
            if key in seen_conditions:
                continue
            seen_conditions.add(key)
            filters.append({"column": implied_col, "operator": operator, "value": value})

    if not filters:
        return None
    logic = "or" if len(filters) > 1 and any(
        word in text for word in ("或", "或者", "or", "OR")
    ) else "and"

    # 阶段 3：趋势目标与日期维度
    target = re.search(r"(?:分析|统计|看)\s*(?:一下\s*)?([^\s,，；;。]{1,20}?)\s*的\s*(?:趋势|走势)", text)
    value_column = ""
    if target:
        phrase = re.sub(r"^(?:的|中|数据|里的)+", "", target.group(1))
        value_column = resolve_column(phrase, columns, aliases)
    if not value_column:
        candidates = [
            c for c in columns
            if any(word in c for word in ("成交价", "成交金额", "金额"))
        ]
        if len(candidates) == 1:
            value_column = candidates[0]
    if not value_column:
        return None

    date_column = ""
    for name in ("结束时间", "开始时间", "成交时间", "日期"):
        if name in columns:
            date_column = name
            break
    if not date_column:
        found = [c for c in columns if "时间" in c or "日期" in c]
        if len(found) == 1:
            date_column = found[0]
    if not date_column:
        return None

    if re.search(r"按月|每月|月度", text):
        parts = ["month"]
    elif re.search(r"逐年|每年|年度", text):
        parts = ["year"]
    else:
        parts = ["year", "month"]

    notes.append(
        f"组合流程：整行去重（{len(subset)} 个键）→ {len(filters)} 个条件 {logic} "
        f"→ 按 {date_column} 分析 {value_column} 趋势"
    )
    return CompositeIntent(
        dedupe_subset=subset, filters=filters, logic=logic,
        date_column=date_column, value_column=value_column,
        parts=parts, notes=notes, raw_intent=text,
    )


def _apply_composite_plan(
    rule: Dict[str, Any], spec: CompositeIntent
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """用确定性多步骤骨架替换 LLM 的截断/错误链路。"""
    old_steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    removed = len(old_steps)
    out = dict(rule)
    out["steps"] = spec.steps()
    out["task_type"] = "trend"
    warnings = list(spec.notes)
    warnings.insert(
        0,
        f"意图编译：识别到组合流程（去重 → 条件提取 → 趋势），"
        f"已用确定性骨架替换 {removed} 个 LLM 步骤，保证三个阶段都执行",
    )
    info = {
        "applied": True,
        "spec": spec.to_dict(),
        "replaced_steps": removed,
        "kept_steps": 0,
        "warnings": warnings,
    }
    return out, warnings, info


# ------------------------------------------------------------
# 通用工作流意图：小原语按描述顺序自由编排（不绑定某一种固定链路）
# ------------------------------------------------------------
# 只在强连接词处切句；「或/或者」保留在同一句内表示同一 filter 的 or 逻辑。
_PHASE_SPLIT_RE = re.compile(r"然后|接着|随后|之后|最后|(?:^|[^再])再(?=[提筛按去重分统计算])|；|;")
_FORMULA_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9_]{1,20})\s*(?:等于|=|＝)\s*"
    r"[（(]?\s*(?P<expr>[^）)，,；;。]+?)\s*[）)]?(?=[\s，,；;。]|$)"
)
_GRANULARITY_RULES = (
    (r"按\s*季度|每季度|季度(?:的)?(?:趋势|走势|分析|统计|均值|单价)", "quarter"),
    (r"按\s*月|每月|逐月|月度", "month"),
    (r"按\s*年|每年|逐年|年度", "year"),
    (r"按\s*周|每周", "week"),
    (r"按\s*日|每天", "day"),
)
_ANALYSIS_SIGNALS = (
    "趋势", "走势", "分析", "统计", "汇总", "求和", "总和", "均值", "平均",
    "最大", "最小", "条数", "数量", "个数", "占比", "透视", "排名",
)
_WORKFLOW_COMPLEX_OPS = frozenset({"dedupe", "expr", "date_part", "pivot", "resample", "outlier"})
_WORKFLOW_ANALYSIS_OPS = frozenset({"aggregate", "count_rows", "pct", "rank", "pivot", "resample"})


@dataclass
class WorkflowIntent:
    """按描述顺序编排的通用原语链；不假定「去重→筛选→趋势」这一种形态。"""

    steps: List[Dict[str, Any]] = field(default_factory=list)
    task_type: str = "detail"
    derived: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_intent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": list(self.steps), "task_type": self.task_type,
            "derived": list(self.derived), "notes": list(self.notes),
        }


def _formula_steps(
    text: str, columns: Sequence[str], aliases: Optional[Dict[str, str]],
    notes: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把「成交单价 = （成交价/面积）」这类显式公式编译成安全的 expr 步骤。"""
    from ops.builtin.value_ops import parse_expression

    steps: List[Dict[str, Any]] = []
    names: List[str] = []
    for match in _FORMULA_RE.finditer(text):
        name = match.group("name").strip()
        expression = match.group("expr").strip()
        expression = expression.replace("（", "(").replace("）", ")")
        for zh, symbol in (("除以", "/"), ("乘以", "*"), ("加上", "+"), ("减去", "-"),
                           ("×", "*"), ("÷", "/")):
            expression = expression.replace(zh, symbol)
        if not re.search(r"[+\-*/]", expression):
            continue  # 普通赋值（市=福州）不是公式，交给筛选解析
        tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_.]+|[()+\-*/]", expression)
        operands: List[str] = []
        rendered: List[str] = []
        broken = False
        for token in tokens:
            if re.fullmatch(r"[()+\-*/]", token):
                rendered.append(token)
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?", token):
                operands.append(token)
                rendered.append(f"c{len(operands) - 1}")
                continue
            column = resolve_column(token, columns, aliases)
            if not column:
                notes.append(f"公式「{name}」里的「{token}」未匹配到真实列，已跳过该公式")
                broken = True
                break
            operands.append(column)
            rendered.append(f"c{len(operands) - 1}")
        if broken:
            continue
        candidate = "".join(rendered)
        try:
            parse_expression(candidate)
        except (ValueError, SyntaxError):
            notes.append(f"公式「{expression}」不是可安全计算的算术表达式，已跳过")
            continue
        steps.append({"op": "expr", "column": name,
                      "expression": candidate, "operands": operands})
        names.append(name)
    return steps, names


def _ratio_formulas(
    formula_steps: Sequence[Dict[str, Any]]
) -> Dict[str, Tuple[str, str]]:
    """从 expr 步骤中找出「比值型」公式：新列 → (分子列, 分母列)。"""
    out: Dict[str, Tuple[str, str]] = {}
    for step in formula_steps:
        expression = str(step.get("expression", ""))
        operands = [str(o) for o in (step.get("operands") or [])]
        if "/" not in expression or len(operands) != 2:
            continue
        if any(re.fullmatch(r"-?\d+(?:\.\d+)?", o) for o in operands):
            continue
        out[str(step.get("column"))] = (operands[0], operands[1])
    return out


def _infer_dedupe_key(columns: Sequence[str]) -> str:
    """裸写「去重」时优先选稳定的业务主键，而不是按整行去重。"""
    for column in columns:
        if str(column) == "标的号":
            return str(column)
    for column in columns:
        name = str(column).lower()
        if any(hint in name for hint in ("标的号", "编号", "序号", "唯一", "主键", "id")):
            return str(column)
    return ""


def _numeric_conditions(phase: str, columns: Sequence[str],
                        aliases: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    """扫描「面积小于50或大于1000」形式的条件，支持承前省略列名。"""
    terms: List[Tuple[str, str]] = [(str(c), str(c)) for c in columns]
    for column in columns:
        short = re.split(r"[（(]", str(column), maxsplit=1)[0].strip()
        if short and short != str(column):
            terms.append((short, str(column)))
    terms.extend(
        (str(label), str(target)) for label, target in (aliases or {}).items()
        if str(target) in {str(c) for c in columns}
    )
    conditions: List[Dict[str, Any]] = []
    seen: set = set()
    for term, column in sorted(terms, key=lambda pair: -len(pair[0])):
        pattern = re.compile(
            re.escape(term) + r"\s*(?P<op>小于等于|不小于|不大于|大于等于|小于|低于|大于|高于|<=|>=|<|>)\s*"
            r"(?P<val>\d+(?:\.\d+)?)"
        )
        for match in pattern.finditer(phase):
            operator = _NUM_OPERATORS.get(match.group("op"))
            if not operator:
                continue
            raw = match.group("val")
            value: Any = float(raw) if "." in raw else int(raw)
            key = (column, operator, value)
            if key in seen:
                continue
            seen.add(key)
            conditions.append({"column": column, "operator": operator, "value": value})
    if conditions:
        implied = conditions[-1]["column"]
        for match in re.finditer(
            r"(?:或|或者|且|并且|and)\s*"
            r"(?P<op>小于等于|不小于|不大于|大于等于|小于|低于|大于|高于|<=|>=|<|>)\s*"
            r"(?P<val>\d+(?:\.\d+)?)", phase, re.I,
        ):
            operator = _NUM_OPERATORS.get(match.group("op").lower())
            if not operator:
                continue
            raw = match.group("val")
            value: Any = float(raw) if "." in raw else int(raw)
            key = (implied, operator, value)
            if key not in seen:
                seen.add(key)
                conditions.append({"column": implied, "operator": operator, "value": value})
    return conditions


def _workflow_date_column(
    phase: str,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
) -> str:
    """绑定描述里的日期列：显式别名 > 完整列名 > 通用兜底。

    不能只靠「列名里含时间」的数量判断：字段重命名后常只剩一个中文时间列
    （如 ``shipped_at`` + ``签收时间``），会把「按发货时间趋势」错绑到签收时间。
    """
    for label, target in (aliases or {}).items():
        label_text, target_text = str(label), str(target)
        if label_text in phase and target_text in columns:
            if any(w in label_text or w in target_text for w in ("时间", "日期")):
                return target_text
    for name in ("结束时间", "开始时间", "成交时间", "日期", "时间"):
        if name in columns and name in phase:
            return name
    for column in columns:
        text = str(column)
        if text in phase and ("时间" in text or "日期" in text):
            return text
    found = [c for c in columns if "时间" in str(c) or "日期" in str(c)]
    if len(found) == 1 and str(found[0]) in phase:
        return str(found[0])
    for name in ("结束时间", "开始时间", "成交时间", "日期"):
        if name in columns:
            return name
    return found[0] if len(found) == 1 else ""


_WORKFLOW_BUCKETS = {
    ("year", "quarter"): "年季度",
    ("year", "month"): "年月",
    ("year", "week"): "年周",
    ("year", "month", "day"): "年月日",
}


def _workflow_date_filter(phase: str, date_column: str) -> Optional[Dict[str, Any]]:
    """从分析从句里提取显式时间范围，编译为独立 date_filter 原语。

    「按月/按季度」是分组粒度，不是筛选；只有出现具体年份、日期区间或
    「近 N 天」这类限定范围的说法时才生成筛选步骤，避免把趋势分析误删成
    单一年份，也避免把无年份的跨年趋势丢掉。
    """
    if not date_column:
        return None
    dates = re.findall(r"(?P<d>(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})", phase)
    if len(dates) >= 2:
        return {"op": "date_filter", "column": date_column,
                "start": dates[0], "end": dates[1]}
    years = [int(y) for y in re.findall(r"((?:19|20)\d{2})\s*年", phase)]
    if len(years) >= 2:
        return {"op": "date_filter", "column": date_column,
                "start": f"{min(years)}-01-01", "end": f"{max(years)}-12-31"}
    if years:
        return {"op": "date_filter", "column": date_column, "year": years[0]}
    recent = re.search(r"近\s*(\d+)\s*天", phase)
    if recent:
        return {"op": "date_filter", "column": date_column,
                "recent_days": int(recent.group(1))}
    return None


def _workflow_date_parts(text: str) -> List[str]:
    """解析时间粒度；细粒度一律带上 year，避免跨年同季被合并。

    「每一年按季度」需要 year+quarter 两个分量并组合成时间桶；
    只写「按季度」也默认带 year，因为「按季度看趋势」同样不该把
    2025Q1 与 2026Q1 当成同一期。
    """
    explicit_year = bool(re.search(r"按\s*年|每一?年|逐年|年度", text))
    fine = ""
    for pattern, part in _GRANULARITY_RULES:
        if part == "year":
            continue
        if re.search(pattern, text):
            fine = part
            break
    if fine == "quarter":
        return ["year", "quarter"]
    if fine == "month":
        return ["year", "month"]
    if fine == "week":
        return ["year", "week"]
    if fine == "day":
        return ["year", "month", "day"]
    if explicit_year:
        return ["year"]
    if re.search(r"趋势|走势", text):
        return ["year", "month"]
    return []


def _workflow_bucket(parts: Sequence[str]) -> str:
    """多分量时返回组合时间桶名，单分量时返回空串。"""
    return _WORKFLOW_BUCKETS.get(tuple(parts), "")


def _workflow_target(
    phase: str, columns: Sequence[str], aliases: Optional[Dict[str, str]],
    derived: Sequence[str],
) -> str:
    flat = _plain(phase)
    for name in derived:
        if _plain(name) in flat:
            return name
    patterns = (
        r"(?:分析|统计|看|求|计算|汇总)\s*(?:一下\s*)?([^\s,，；;。]{1,20}?)\s*的\s*"
        r"(?:趋势|走势|均值|平均|总和|求和|总额|最大值|最小值)",
        r"(?:分析|统计|看|求|计算|汇总)\s*(?:一下\s*)?([^\s,，；;。]{1,20}?)\s*"
        r"(?:趋势|走势|均值|平均|总和|求和)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, phase):
            phrase = re.sub(r"^(?:的|中|数据|里的)+", "", match.group(1))
            column = resolve_column(phrase, columns, aliases)
            if column:
                return column
    for name in derived:
        if name in columns or name:
            return name
    for word in ("成交单价", "成交价", "成交金额", "金额", "数量", "面积"):
        column = resolve_column(word, columns, aliases)
        if column:
            return column
    return ""


def _workflow_analysis(
    phase: str, columns: Sequence[str], aliases: Optional[Dict[str, str]],
    derived: Sequence[str], value_hints: Optional[Dict[str, Sequence[str]]],
    notes: List[str], ratio_formulas: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """分析阶段：时间粒度是常识推导，不需要数据里预先存在季度列。

    时间桶默认带年份；比值型指标的「平均」显式编译为 Σ分子/Σ分母，
    同时保留逐条均值，避免把两种口径混为一谈。
    """
    steps: List[Dict[str, Any]] = []
    parts = _workflow_date_parts(phase)
    date_column = _workflow_date_column(phase, columns, aliases)
    date_filter = _workflow_date_filter(phase, date_column)
    bucket = _workflow_bucket(parts)
    if date_filter and not (parts and date_column):
        steps.append(date_filter)
        notes.append(f"时间口径：先用 date_filter 限定 {date_column}，时间分桶仅由需求里的粒度决定")
    if parts and date_column:
        if date_filter:
            steps.append(date_filter)
            notes.append(
                f"时间口径：分析前先用 date_filter 限定 {date_column}，"
                "不能用 date_part 的年份分量代替筛选"
            )
        part_step: Dict[str, Any] = {"op": "date_part", "column": date_column, "parts": list(parts)}
        if bucket:
            part_step["bucket"] = bucket
        steps.append(part_step)
        by = ([f"{date_column}_{bucket}"] if bucket
              else [f"{date_column}_{DATE_PART_LABELS.get(parts[0], parts[0])}"])
        steps.append({"op": "dropna", "subset": [date_column], "how": "any"})
        if bucket:
            notes.append(f"时间口径：按 {date_column} 的 {bucket} 分桶，跨年同期次不会被合并；")
        notes.append(f"时间口径：{date_column} 为空或无法解析的记录不进入趋势期次")
    else:
        groups: List[str] = []
        for match in _GROUP_RE.finditer(phase):
            column = resolve_column(match.group("col"), columns, aliases)
            if column and column not in groups:
                groups.append(column)
        by = groups
    if not by:
        notes.append(f"分析阶段「{phase}」没有可分组的时间或业务维度，已跳过该阶段")
        return steps, "detail"

    if re.search(r"条数|数量|个数|几条|多少个|计数", phase):
        steps.append({"op": "count_rows", "by": by, "alias": "条数"})
        if parts:
            steps.append({"op": "sort", "by": by, "ascending": [True]})
        return steps, "summary" if not parts else "trend"
    value_column = _workflow_target(phase, columns, aliases, derived)
    if not value_column:
        notes.append(f"分析阶段「{phase}」未识别到可统计的指标列，已跳过")
        return steps, "detail"

    if re.search(r"求和|总额|总和", phase):
        func = "sum"
    elif re.search(r"最大|最高", phase):
        func = "max"
    elif re.search(r"最小|最低", phase):
        func = "min"
    else:
        func = "mean"
    ratio = (ratio_formulas or {}).get(value_column)
    if func == "mean" and re.search(r"平均|均价|均值", phase) and ratio:
        numerator, denominator = ratio
        num_alias, den_alias = f"{numerator}之和", f"{denominator}之和"
        row_alias = f"{value_column}·逐条均值"
        # 分子或分母为空的行会让 Σ分母 被稀释而 Σ分子 不变，必须成对剔除
        steps.append({"op": "filter", "logic": "and", "conditions": [
            {"column": numerator, "operator": "notna"},
            {"column": denominator, "operator": "notna"},
        ]})
        notes.append(f"空值口径：{numerator} 或 {denominator} 为空的记录不参与均价计算")
        steps.append({
            "op": "aggregate", "by": by,
            "column": [value_column, numerator, denominator],
            "func": ["mean", "sum", "sum"],
            "alias": [row_alias, num_alias, den_alias],
        })
        steps.append({"op": "expr", "column": value_column,
                      "expression": "c0/c1", "operands": [num_alias, den_alias]})
        notes.append(
            f"均价口径：{value_column} = Σ{numerator} / Σ{denominator}（总价除以总面积）；"
            f"同时输出 {row_alias} 供对照"
        )
    else:
        steps.append({"op": "aggregate", "by": by, "column": [value_column],
                      "func": [func], "alias": [value_column]})
    if parts:
        steps.append({"op": "sort", "by": by, "ascending": [True]})
    return steps, "trend" if parts else "summary"


def parse_workflow_intent(
    intent: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Optional[WorkflowIntent]:
    """把多步骤描述编译为按原语自由组合的执行链。

    触发条件刻意保守：至少出现一个「复杂原语」（去重/公式/日期粒度/透视/重采样）
    且存在明确分析阶段；简单单族需求继续交给 rate/detail/count_group 编译器。
    """
    text = normalize_text(intent)
    if not text or ("率" in text and ("判断" in text or "之比" in text)):
        return None
    notes: List[str] = []
    formula_steps, derived = _formula_steps(text, columns, aliases, notes)
    ratio_map = _ratio_formulas(formula_steps)
    phases = [p.strip(" ，,。.、") for p in _PHASE_SPLIT_RE.split(text) if p.strip(" ，,。.、")]
    if len(phases) < 2 and not formula_steps:
        return None

    steps: List[Dict[str, Any]] = []
    formula_inserted = False
    kinds: set = set()
    last_type = "detail"
    for phase in phases:
        # 公式已整体编译；从句里剔除公式文本后再解析筛选/分析步骤
        phase = _FORMULA_RE.sub("", phase).strip(" ，,。.、")
        if not phase:
            continue
        is_analysis = any(word in phase for word in _ANALYSIS_SIGNALS)
        if re.search(r"去重|去掉重复|重复行", phase):
            subset: List[str] = []
            match = re.search(r"(?:按|根据|依据)\s*([^\s,，；;。]{1,12})\s*去重", phase)
            if match:
                column = resolve_column(match.group(1), columns, aliases)
                if column:
                    subset = [column]
                else:
                    notes.append(f"去重键「{match.group(1)}」未匹配到真实列，尝试推断业务主键")
            if not subset:
                inferred_key = _infer_dedupe_key(columns)
                if inferred_key:
                    subset = [inferred_key]
                    notes.append(f"去重键未明示，按业务主键 {inferred_key} 去重")
                else:
                    notes.append("去重键未明示且无业务主键，退化为整行去重")
            steps.append({"op": "dedupe", "subset": subset, "keep": "first"})
            kinds.add("dedupe")
        conditions = _numeric_conditions(phase, columns, aliases)
        if conditions:
            logic = "or" if len(conditions) > 1 and re.search(
                r"或|或者|or", phase, re.I
            ) else "and"
            steps.append({"op": "filter", "conditions": conditions, "logic": logic})
            kinds.add("filter")
        # 「剔除异常价格」是显式原语，不能只在 options 里声明口径
        if re.search(r"(剔除|去掉|排除|删除).{0,10}(异常|离群)", phase):
            outlier_column = ""
            match = re.search(r"(?:异常|离群)(?:的)?([\u4e00-\u9fffA-Za-z0-9_（）()]{1,16})", phase)
            if match:
                outlier_column = resolve_column(match.group(1), columns, aliases)
            if not outlier_column:
                for word in ("成交价", "价格", "金额"):
                    outlier_column = resolve_column(word, columns, aliases)
                    if outlier_column:
                        break
            if outlier_column:
                steps.append({"op": "outlier", "column": outlier_column,
                              "method": "iqr", "action": "remove"})
                kinds.add("outlier")
                notes.append(f"异常值口径：按 IQR 剔除 {outlier_column} 的异常记录")
        # 「某列空值置为 0」属于清洗原语，可与筛选/分析任意顺序组合。
        # 用真实列名/别名做锚点，避免正则把「成交价空值」整体当成列名。
        fill_terms: List[Tuple[str, str]] = [(str(c), str(c)) for c in columns]
        for column in columns:
            short = re.split(r"[（(]", str(column), maxsplit=1)[0].strip()
            if short and short != str(column):
                fill_terms.append((short, str(column)))
        fill_terms.extend(
            (str(label), str(target)) for label, target in (aliases or {}).items()
            if str(target) in {str(c) for c in columns}
        )
        for term, column in sorted(fill_terms, key=lambda pair: -len(pair[0])):
            pattern = re.compile(
                re.escape(term) + r"\s*(?:空值|为空)?\s*(?:置为|设为|填为)\s*"
                r"(?P<val>-?\d+(?:\.\d+)?)"
            )
            for match in pattern.finditer(phase):
                raw = match.group("val")
                value: Any = float(raw) if "." in raw else int(raw)
                steps.append({"op": "fill", "columns": [column],
                              "method": "value", "value": value})
                kinds.add("fill")
                notes.append(f"空值口径：{column} 空值填为 {value}")
            if pattern.search(phase):
                break
        if not is_analysis:
            local_notes: List[str] = []
            filters, dates, _ = _parse_filters(
                {"筛选": [phase]}, "", columns, aliases, local_notes, value_hints
            )
            # 数字条件/去重已单独处理，只保留确实新增了筛选或日期时的诊断
            if filters or dates:
                notes.extend(local_notes)
            if filters:
                steps.append({"op": "filter", "conditions": filters, "logic": "and"})
                kinds.add("filter")
            for step in dates:
                steps.append(step)
                kinds.add("date_filter")
        if is_analysis:
            if formula_steps and not formula_inserted:
                steps.extend(formula_steps)
                formula_inserted = True
                kinds.add("expr")
            analysis_steps, last_type = _workflow_analysis(
                phase, columns, aliases, derived, value_hints, notes, ratio_map
            )
            for step in analysis_steps:
                steps.append(step)
                kinds.add(str(step.get("op")))

    if not (_WORKFLOW_COMPLEX_OPS & kinds) or not (_WORKFLOW_ANALYSIS_OPS & kinds):
        return None
    if formula_steps and not formula_inserted:
        steps.extend(formula_steps)
        kinds.add("expr")
    # 去除完全重复的步骤（例如同一数值条件被两个分句各识别一次）
    unique: List[Dict[str, Any]] = []
    for step in steps:
        if step not in unique:
            unique.append(step)
    notes.append(
        f"通用工作流：按描述顺序编排 {' → '.join(s['op'] for s in unique)}"
        + (f"；公式派生 {len(derived)} 列" if derived else "")
    )
    return WorkflowIntent(steps=unique, task_type=last_type,
                          derived=list(derived), notes=notes, raw_intent=text)


def _apply_workflow_plan(
    rule: Dict[str, Any], spec: WorkflowIntent
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    old_steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    removed = len(old_steps)
    out = dict(rule)
    out["steps"] = spec.steps
    out["task_type"] = spec.task_type
    # 组合场景默认保留原始空值与异常值，避免 median 填充把判空/均价语义污染。
    opts = dict(out.get("options") or {})
    opts.setdefault("fill_missing", "none")
    opts.setdefault("outlier_method", "none")
    out["options"] = opts
    warnings = list(spec.notes)
    warnings.append("空值/异常值口径：默认不填充、不剔除；需在描述中明确才改变")
    warnings.insert(
        0,
        f"意图编译：识别到多步骤原语组合，已用确定性编排替换 {removed} 个 LLM 步骤"
        f"（{' → '.join(s['op'] for s in spec.steps)}）",
    )
    info = {
        "applied": True, "spec": spec.to_dict(),
        "replaced_steps": removed, "kept_steps": 0, "warnings": warnings,
    }
    return out, warnings, info


# ------------------------------------------------------------
# 规则生成层：确定性替换 LLM 的分析链路
# ------------------------------------------------------------
def _apply_rate_plan(
    rule: Dict[str, Any], spec: RateIntent
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """把 LLM 生成的比率规则替换为确定性骨架。"""
    old_steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    kept = [s for s in old_steps if str(s.get("op")) in _PRESERVED_OPS]
    removed = len(old_steps) - len(kept)
    steps = spec.steps()
    steps.extend(kept)
    steps.append(spec.rate_step())

    out = dict(rule)
    out["steps"] = steps
    out["task_type"] = "proportion"
    options = dict(out.get("options") or {})
    warnings: List[str] = list(spec.notes)
    if str(options.get("fill_missing", "")) == "drop":
        options["fill_missing"] = "median"
        warnings.append(
            "比率需求依赖原始空/非空语义，options.fill_missing 已从 drop 改为 median"
        )
    out["options"] = options

    warnings.insert(
        0,
        f"意图编译：识别到「{spec.alias}」（{spec.judge_column} 判空分 "
        f"{spec.true_label}/{spec.false_label}），已用确定性骨架替换 {removed} 个"
        f" LLM 分析步骤，保留 {len(kept)} 个清洗步骤；结果不受模型输出波动影响",
    )
    info = {
        "applied": True,
        "spec": spec.to_dict(),
        "replaced_steps": removed,
        "kept_steps": len(kept),
        "warnings": warnings,
    }
    return out, warnings, info


# 明细意图下允许保留的 LLM 步骤：只做整理、不改变「逐行明细」的结果形态。
# 筛选 / 日期 / 空值填充由骨架生成；汇总、比率、透视、关联一律剔除。
_DETAIL_KEEP_OPS = frozenset({
    "sort", "head", "tail", "sample", "dedupe", "dedupe_fuzzy",
    "mask_pii", "text_normalize", "convert_unit", "category_map", "cast",
    "assert", "profile", "replace", "clip", "outlier", "normalize",
    "date_parse", "bin", "date_part",
})


def _apply_detail_plan(
    rule: Dict[str, Any], spec: DetailIntent
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """把 LLM 生成的明细规则替换为确定性骨架。"""
    old_steps = [s for s in (rule.get("steps") or []) if isinstance(s, dict)]
    kept = [s for s in old_steps if str(s.get("op")) in _DETAIL_KEEP_OPS]
    removed = len(old_steps) - len(kept)

    out = dict(rule)
    out["steps"] = spec.steps() + kept
    out["task_type"] = "detail"
    options = dict(out.get("options") or {})
    if spec.preserve_nulls or spec.fills:
        options["fill_missing"] = "none"
    out["options"] = options

    warnings: List[str] = list(spec.notes)
    warnings.insert(
        0,
        f"意图编译：识别到明细/清洗需求（{len(spec.filters)} 个筛选、"
        f"{len(spec.fills)} 个空值填充），已用确定性骨架替换 {removed} 个 LLM 步骤，"
        "结果保持逐行明细，不受模型输出波动影响",
    )
    if removed:
        warnings.append(
            f"已剔除 {removed} 个会改变结果形态的 LLM 步骤（汇总/统计/比率/筛选/填充），"
            "如需这些操作请在描述中明确写出统计类需求"
        )
    info = {
        "applied": True,
        "spec": spec.to_dict(),
        "replaced_steps": removed,
        "kept_steps": len(kept),
        "warnings": warnings,
    }
    return out, warnings, info


def apply_intent_plan(
    rule: Dict[str, Any],
    intent: Any,
    columns: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
    value_hints: Optional[Dict[str, Sequence[str]]] = None,
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """把 LLM 生成的结果替换为确定性骨架，返回 (rule, warnings, info)。

    通用工作流优先（原语自由编排），其后是比率与明细/清洗编译器；都不命中
    时原样返回。LLM 结果只保留不破坏目标形态的整理步骤。
    """
    workflow = parse_workflow_intent(intent, columns, aliases, value_hints)
    if workflow is not None:
        return _apply_workflow_plan(rule, workflow)
    composite = parse_composite_intent(intent, columns, aliases, value_hints)
    if composite is not None:
        return _apply_composite_plan(rule, composite)
    spec = parse_rate_intent(intent, columns, aliases, value_hints)
    if spec is not None:
        return _apply_rate_plan(rule, spec)
    detail = parse_detail_intent(intent, columns, aliases, value_hints)
    if detail is not None:
        return _apply_detail_plan(rule, detail)
    return rule, [], {}


# ------------------------------------------------------------
# 编译层兜底：识别「明显错误」的比率链路
# ------------------------------------------------------------
def judgement_columns(steps: Sequence[Dict[str, Any]]) -> set:
    """收集「二分类判据」列（rate.column / assign.rules 的判空列）。

    刻意**不收集** ``pct(scope=condition)`` 的条件列：条件占比是「分子 / 全部」
    语义，用户可能真的要求先按该列筛掉空值再算占比；把 pct 条件列也当作
    分类判据会导致编译层误删合法的筛选条件。
    """
    found: set = set()

    def walk_conditions(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("operator"), str) and node["operator"] in ("isna", "notna"):
                col = node.get("column")
                if isinstance(col, str) and col:
                    found.add(col)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk_conditions(child)
        elif isinstance(node, list):
            for child in node:
                walk_conditions(child)

    for step in steps:
        if not isinstance(step, dict):
            continue
        op = str(step.get("op", ""))
        if op == "rate" and isinstance(step.get("column"), str) and step["column"]:
            found.add(step["column"])
        if op == "assign":
            walk_conditions(step.get("rules"))
    return found


def target_from_intent(intent: Any) -> Optional[Dict[str, str]]:
    """给编译层用：只提取「要统计哪个率」，不要求判据列可解析。

    返回 ``{"alias", "target_label"}`` 或 None。简写描述（如「筛选市：福州，
    统计未成交率」）虽然没有判据段，但目标方向明确，仍可用来修正已有
    ``rate`` 步骤的 numerator 与 alias。
    """
    text = normalize_text(intent)
    if not text or not any(w in text for w in _RATE_WORDS):
        return None
    sections, _ = _sections(text)
    alias, _ = _target_alias(sections, text)
    if not alias:
        return None
    target_label = re.sub(r"(率|占比|比例|百分比)$", "", alias).strip()
    if not target_label:
        return None
    return {"alias": alias, "target_label": target_label}


__all__ = [
    "RateIntent", "CompositeIntent", "WorkflowIntent",
    "PRESERVED_OPS",
    "apply_intent_plan",
    "judgement_columns",
    "normalize_text",
    "parse_composite_intent",
    "parse_workflow_intent",
    "parse_rate_intent",
    "resolve_column",
    "target_from_intent",
]

PRESERVED_OPS = _PRESERVED_OPS

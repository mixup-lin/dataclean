# -*- coding: utf-8 -*-
"""多行业基准数据集与扰动变体。

覆盖电商 / 金融 / 医疗 / 物流 / 教育 / 制造六类常见结构。每个数据集都内置：
正常样本、边界值、异常值、缺失值、枚举同义词与日期/金额格式差异。
变体只改「输入形态」不改业务口径：字段重命名、取值替换、行顺序调整、
格式波动、规模放大及组合压力，用来验证规则不依赖单一数据样本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pandas as pd


@dataclass(frozen=True)
class IndustryDataset:
    id: str
    industry: str
    title: str
    columns: Tuple[str, ...]
    key: str
    rows: Tuple[Dict[str, Any], ...]
    aliases: Dict[str, str]
    rename_map: Dict[str, str] = field(default_factory=dict)
    replacements: Dict[str, Dict[str, str]] = field(default_factory=dict)
    category_columns: Tuple[str, ...] = ()
    numeric_columns: Tuple[str, ...] = ()
    date_columns: Tuple[str, ...] = ()
    sample_notes: str = ""

    def canonical_to_actual(self, variant: str) -> Dict[str, str]:
        mapping = {c: c for c in self.columns}
        if "renamed" in _tokens(variant):
            mapping.update(self.rename_map)
        return mapping

    def aliases_for(self, variant: str) -> Dict[str, str]:
        mapping = self.canonical_to_actual(variant)
        return {label: mapping.get(col, col) for label, col in self.aliases.items()}


def _tokens(variant: str) -> List[str]:
    return [t.strip() for t in str(variant or "baseline").split("+") if t.strip()]


def dataset_by_id(dataset_id: str) -> IndustryDataset:
    return DATASETS[dataset_id]


DATASETS: Dict[str, IndustryDataset] = {}


def _register(ds: IndustryDataset) -> IndustryDataset:
    DATASETS[ds.id] = ds
    return ds


# ---------------------------------------------------------------------------
# 电商：订单粒度，含支付状态同义词、金额单位混用、空日期与极端异常金额
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="ecom_orders",
    industry="电商",
    title="订单明细",
    columns=("订单号", "用户ID", "品类", "订单金额", "支付状态", "优惠金额", "下单时间", "支付方式"),
    key="订单号",
    rows=(
        {"订单号": "O1001", "用户ID": "U01", "品类": "数码", "订单金额": 1299.0, "支付状态": "已支付", "优惠金额": 50.0, "下单时间": "2025-01-08", "支付方式": "支付宝"},
        {"订单号": "O1002", "用户ID": "U02", "品类": "家居", "订单金额": 299.0, "支付状态": "未支付", "优惠金额": 0.0, "下单时间": "2025-02-11", "支付方式": "微信"},
        {"订单号": "O1003", "用户ID": "U03", "品类": "数码", "订单金额": 0.0, "支付状态": "已支付", "优惠金额": 0.0, "下单时间": "2026-01-03", "支付方式": "银行卡"},
        {"订单号": "O1004", "用户ID": "U04", "品类": "服饰", "订单金额": 99.9, "支付状态": "支付成功", "优惠金额": 10.0, "下单时间": "2026-02-14", "支付方式": "支付宝"},
        {"订单号": "O1005", "用户ID": "U05", "品类": "数码", "订单金额": 12800.0, "支付状态": "PAID", "优惠金额": 200.0, "下单时间": "2026/04/18", "支付方式": "微信"},
        {"订单号": "O1006", "用户ID": "U06", "品类": "家居", "订单金额": None, "支付状态": "已支付", "优惠金额": 20.0, "下单时间": "2026-05-20", "支付方式": "微信"},
        {"订单号": "O1007", "用户ID": "U07", "品类": "数码", "订单金额": 999999999.0, "支付状态": "未支付", "优惠金额": 0.0, "下单时间": "2025-12-31", "支付方式": "银行卡"},
        {"订单号": "O1008", "用户ID": "U08", "品类": "服饰", "订单金额": 59.9, "支付状态": "PAID", "优惠金额": 0.0, "下单时间": "2026/07/01", "支付方式": "支付宝"},
        {"订单号": "O1009", "用户ID": "U09", "品类": "家居", "订单金额": 199.0, "支付状态": "待支付", "优惠金额": None, "下单时间": "2025-06-15", "支付方式": "银联"},
        {"订单号": "O1010", "用户ID": "U10", "品类": "数码", "订单金额": 399.0, "支付状态": "已支付", "优惠金额": 0.0, "下单时间": None, "支付方式": "微信"},
    ),
    aliases={"品类": "品类", "金额": "订单金额", "支付状态": "支付状态", "日期": "下单时间"},
    rename_map={"品类": "category", "订单金额": "amount", "支付状态": "pay_status", "下单时间": "order_date"},
    replacements={"支付状态": {"已支付": "PAID", "支付成功": "PAID", "未支付": "UNPAID", "待支付": "UNPAID"}},
    category_columns=("品类", "支付状态", "支付方式"),
    numeric_columns=("订单金额", "优惠金额"),
    date_columns=("下单时间",),
    sample_notes="0 元边界、¥/万格式、NULL 缺失、1e9 异常金额、已支付/PAID/支付成功同义词、空日期",
))

# ---------------------------------------------------------------------------
# 金融：客户授信粒度，含分行、逾期天数、违约标记与负/零/极端余额
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="fin_loans",
    industry="金融",
    title="信贷客户明细",
    columns=("客户号", "分行", "产品", "贷款余额", "逾期天数", "风险等级", "放款日期", "违约标记"),
    key="客户号",
    rows=(
        {"客户号": "C001", "分行": "鼓楼支行", "产品": "房贷", "贷款余额": 1000000.0, "逾期天数": 0, "风险等级": "低", "放款日期": "2025-01-05", "违约标记": "否"},
        {"客户号": "C002", "分行": "鼓楼支行", "产品": "消费贷", "贷款余额": 50000.0, "逾期天数": 31, "风险等级": "中", "放款日期": "2026-02-08", "违约标记": "是"},
        {"客户号": "C003", "分行": "台江支行", "产品": "房贷", "贷款余额": 0.0, "逾期天数": 0, "风险等级": "低", "放款日期": "2026-03-11", "违约标记": "否"},
        {"客户号": "C004", "分行": "鼓楼支行", "产品": "消费贷", "贷款余额": 8000.0, "逾期天数": 90, "风险等级": "高", "放款日期": "2025-11-20", "违约标记": "是"},
        {"客户号": "C005", "分行": "台江支行", "产品": "经营贷", "贷款余额": None, "逾期天数": 5, "风险等级": "中", "放款日期": "2026/01/02", "违约标记": "否"},
        {"客户号": "C006", "分行": "鼓楼支行", "产品": "房贷", "贷款余额": 2000000.0, "逾期天数": 180, "风险等级": "高", "放款日期": "2024-12-31", "违约标记": "是"},
        {"客户号": "C007", "分行": "台江支行", "产品": "消费贷", "贷款余额": 12000.0, "逾期天数": -1, "风险等级": "低", "放款日期": "2026-05-06", "违约标记": "否"},
        {"客户号": "C008", "分行": "鼓楼支行", "产品": "经营贷", "贷款余额": 300000.0, "逾期天数": None, "风险等级": "中", "放款日期": "2026年06月15日", "违约标记": "否"},
        {"客户号": "C009", "分行": "台江支行", "产品": "房贷", "贷款余额": 999999999.0, "逾期天数": 365, "风险等级": "高", "放款日期": "2025-08-09", "违约标记": "是"},
        {"客户号": "C010", "分行": "鼓楼支行", "产品": "消费贷", "贷款余额": 6000.0, "逾期天数": 0, "风险等级": "低", "放款日期": None, "违约标记": "否"},
    ),
    aliases={"分行": "分行", "余额": "贷款余额", "逾期天数": "逾期天数", "日期": "放款日期"},
    rename_map={"分行": "branch", "贷款余额": "balance", "逾期天数": "overdue_days", "放款日期": "disbursed_at"},
    replacements={"违约标记": {"是": "Y", "否": "N"}},
    category_columns=("分行", "产品", "风险等级", "违约标记"),
    numeric_columns=("贷款余额", "逾期天数"),
    date_columns=("放款日期",),
    sample_notes="0/负逾期天数、缺失余额与日期、1e9 异常余额、斜杠与中文日期格式",
))

# ---------------------------------------------------------------------------
# 医疗：就诊记录粒度，含定性与定量检验、住院天数与结果同义词
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="med_visits",
    industry="医疗",
    title="门诊/住院就诊记录",
    columns=("就诊号", "科室", "病种", "检验值", "住院天数", "入院日期", "出院日期", "结果判定"),
    key="就诊号",
    rows=(
        {"就诊号": "M001", "科室": "心内科", "病种": "高血压", "检验值": 60.0, "住院天数": 3, "入院日期": "2025-01-12", "出院日期": "2025-01-15", "结果判定": "阴性"},
        {"就诊号": "M002", "科室": "心内科", "病种": "冠心病", "检验值": 85.0, "住院天数": 7, "入院日期": "2026-02-03", "出院日期": "2026-02-10", "结果判定": "阳性"},
        {"就诊号": "M003", "科室": "心内科", "病种": "高血压", "检验值": None, "住院天数": 0, "入院日期": "2026-02-18", "出院日期": "2026-02-18", "结果判定": None},
        {"就诊号": "M004", "科室": "心内科", "病种": "心律失常", "检验值": 9999.0, "住院天数": 12, "入院日期": "2026/05/06", "出院日期": "2026/05/18", "结果判定": "POS"},
        {"就诊号": "M005", "科室": "心内科", "病种": "冠心病", "检验值": 72.5, "住院天数": 5, "入院日期": "2026-06-11", "出院日期": None, "结果判定": "阴性"},
        {"就诊号": "M006", "科室": "呼吸科", "病种": "肺炎", "检验值": 55.0, "住院天数": 6, "入院日期": "2025-03-01", "出院日期": "2025-03-07", "结果判定": "阳性"},
        {"就诊号": "M007", "科室": "呼吸科", "病种": "哮喘", "检验值": 78.0, "住院天数": 2, "入院日期": "2026-04-09", "出院日期": "2026-04-11", "结果判定": "NEG"},
        {"就诊号": "M008", "科室": "儿科", "病种": "发热", "检验值": 38.5, "住院天数": 1, "入院日期": "2026年07月02日", "出院日期": "2026-07-03", "结果判定": "阴性"},
        {"就诊号": "M009", "科室": "儿科", "病种": "腹泻", "检验值": None, "住院天数": None, "入院日期": "2025-08-20", "出院日期": "2025-08-21", "结果判定": "阳性"},
        {"就诊号": "M010", "科室": "心内科", "病种": "高血压", "检验值": 66.0, "住院天数": 4, "入院日期": None, "出院日期": None, "结果判定": None},
    ),
    aliases={"科室": "科室", "检验值": "检验值", "住院天数": "住院天数", "入院日期": "入院日期"},
    rename_map={"科室": "dept", "检验值": "lab_value", "住院天数": "stay_days", "入院日期": "admitted_at"},
    replacements={"结果判定": {"阳性": "POS", "阴性": "NEG"}},
    category_columns=("科室", "病种", "结果判定"),
    numeric_columns=("检验值", "住院天数"),
    date_columns=("入院日期", "出院日期"),
    sample_notes="检验值 9999 异常、0 天住院边界、缺失检验值/出院日期、POS/NEG 同义词、斜杠与中文日期",
))

# ---------------------------------------------------------------------------
# 物流：运单粒度，含重复单号、里程边界、运费异常与签收状态同义词
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="log_waybills",
    industry="物流",
    title="运单明细",
    columns=("运单号", "承运商", "起点", "终点", "运费", "里程", "签收状态", "发货时间", "签收时间"),
    key="运单号",
    rows=(
        {"运单号": "W01", "承运商": "顺丰", "起点": "福州", "终点": "厦门", "运费": 120.0, "里程": 30.0, "签收状态": "已签收", "发货时间": "2025-01-09", "签收时间": "2025-01-10"},
        {"运单号": "W02", "承运商": "京东", "起点": "福州", "终点": "上海", "运费": 2600.0, "里程": 1200.0, "签收状态": "SIGNED", "发货时间": "2025-05-11", "签收时间": "2025-05-13"},
        {"运单号": "W03", "承运商": "顺丰", "起点": "厦门", "终点": "泉州", "运费": 45.0, "里程": 80.0, "签收状态": "未签收", "发货时间": "2026-01-15", "签收时间": None},
        {"运单号": "W04", "承运商": "中通", "起点": "福州", "终点": "北京", "运费": 3600.0, "里程": 1800.0, "签收状态": "签收", "发货时间": "2026-02-20", "签收时间": "2026-02-24"},
        {"运单号": "W05", "承运商": "京东", "起点": "泉州", "终点": "福州", "运费": 0.0, "里程": 0.0, "签收状态": "已签收", "发货时间": "2026-04-02", "签收时间": "2026-04-02"},
        {"运单号": "W06", "承运商": "顺丰", "起点": "福州", "终点": "深圳", "运费": None, "里程": 1500.0, "签收状态": "已签收", "发货时间": "2026-05-18", "签收时间": "2026-05-20"},
        {"运单号": "W07", "承运商": "中通", "起点": "厦门", "终点": "广州", "运费": 800000000.0, "里程": 25.0, "签收状态": "未签收", "发货时间": "2026/06/01", "签收时间": None},
        {"运单号": "W08", "承运商": "顺丰", "起点": "福州", "终点": "宁德", "运费": 90.0, "里程": None, "签收状态": "已签收", "发货时间": "2025-08-08", "签收时间": "2025-08-09"},
        {"运单号": "W01", "承运商": "顺丰", "起点": "福州", "终点": "厦门", "运费": 999.0, "里程": 30.0, "签收状态": "已签收", "发货时间": "2025-01-09", "签收时间": "2025-01-10"},
        {"运单号": "W10", "承运商": "京东", "起点": "泉州", "终点": "厦门", "运费": 150.0, "里程": 1000.0, "签收状态": "已签收", "发货时间": None, "签收时间": None},
    ),
    aliases={"承运商": "承运商", "运费": "运费", "里程": "里程", "发货时间": "发货时间", "运单号": "运单号"},
    rename_map={"承运商": "carrier", "运费": "freight", "里程": "distance", "发货时间": "shipped_at", "运单号": "waybill_no"},
    replacements={"签收状态": {"已签收": "SIGNED", "签收": "SIGNED", "未签收": "UNSIGNED"}},
    category_columns=("承运商", "起点", "终点", "签收状态"),
    numeric_columns=("运费", "里程"),
    date_columns=("发货时间", "签收时间"),
    sample_notes="重复运单号、里程 0/1000 边界、8e8 异常运费、缺失运费/里程/日期、SIGNED/签收同义词",
))

# ---------------------------------------------------------------------------
# 教育：考试/报名粒度，含 60 分及格边界、满分与超范围分、学期差异
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="edu_scores",
    industry="教育",
    title="学生成绩明细",
    columns=("学号", "班级", "课程", "分数", "报名日期", "学期", "出勤率", "补考标记"),
    key="学号",
    rows=(
        {"学号": "S001", "班级": "一班", "课程": "数学", "分数": 90.0, "报名日期": "2025-09-01", "学期": "2025秋", "出勤率": 0.95, "补考标记": "否"},
        {"学号": "S002", "班级": "一班", "课程": "数学", "分数": 60.0, "报名日期": "2025-09-01", "学期": "2025秋", "出勤率": 0.80, "补考标记": "否"},
        {"学号": "S003", "班级": "一班", "课程": "数学", "分数": 59.0, "报名日期": "2025/09/02", "学期": "2025秋", "出勤率": 0.75, "补考标记": "是"},
        {"学号": "S004", "班级": "一班", "课程": "数学", "分数": None, "报名日期": "2026-02-20", "学期": "2026春", "出勤率": 0.60, "补考标记": "是"},
        {"学号": "S005", "班级": "一班", "课程": "数学", "分数": 100.0, "报名日期": "2026-02-21", "学期": "2026春", "出勤率": 1.00, "补考标记": "否"},
        {"学号": "S006", "班级": "二班", "课程": "数学", "分数": 0.0, "报名日期": "2026-03-01", "学期": "2026春", "出勤率": 0.30, "补考标记": "是"},
        {"学号": "S007", "班级": "二班", "课程": "数学", "分数": 88.5, "报名日期": "2026-03-02", "学期": "2026春", "出勤率": 0.90, "补考标记": "否"},
        {"学号": "S008", "班级": "二班", "课程": "数学", "分数": 105.0, "报名日期": "2026年03月03日", "学期": "2026春", "出勤率": 0.88, "补考标记": "否"},
        {"学号": "S009", "班级": "二班", "课程": "数学", "分数": 72.0, "报名日期": "2025-10-11", "学期": "2025秋", "出勤率": None, "补考标记": "否"},
        {"学号": "S010", "班级": "二班", "课程": "数学", "分数": 61.0, "报名日期": None, "学期": "2026春", "出勤率": 0.70, "补考标记": "否"},
    ),
    aliases={"班级": "班级", "分数": "分数", "课程": "课程", "日期": "报名日期"},
    rename_map={"班级": "class_name", "分数": "score", "报名日期": "enrolled_at"},
    replacements={"补考标记": {"是": "Y", "否": "N"}},
    category_columns=("班级", "课程", "学期", "补考标记"),
    numeric_columns=("分数", "出勤率"),
    date_columns=("报名日期",),
    sample_notes="0/59/60/100 边界、105 超范围异常、缺失分数、斜杠与中文日期、补考 Y/N 同义词",
))

# ---------------------------------------------------------------------------
# 制造：批次粒度，含产线、质检结果同义词、产量与工时异常
# ---------------------------------------------------------------------------
_register(IndustryDataset(
    id="mfg_batches",
    industry="制造",
    title="生产批次明细",
    columns=("批次号", "产线", "工序", "质检结果", "产量", "工时", "生产日期", "设备编号"),
    key="批次号",
    rows=(
        {"批次号": "B01", "产线": "A线", "工序": "冲压", "质检结果": "合格", "产量": 1000, "工时": 8.0, "生产日期": "2025-01-06", "设备编号": "EQ-01"},
        {"批次号": "B02", "产线": "A线", "工序": "焊接", "质检结果": "PASS", "产量": 850, "工时": 7.5, "生产日期": "2026-02-09", "设备编号": "EQ-02"},
        {"批次号": "B03", "产线": "A线", "工序": "喷涂", "质检结果": "不合格", "产量": 0, "工时": 1.0, "生产日期": "2026-03-12", "设备编号": "EQ-03"},
        {"批次号": "B04", "产线": "A线", "工序": "总装", "质检结果": "合格", "产量": 1200, "工时": None, "生产日期": "2026/04/15", "设备编号": "EQ-04"},
        {"批次号": "B05", "产线": "B线", "工序": "冲压", "质检结果": "NG", "产量": 500, "工时": 6.0, "生产日期": "2026-05-18", "设备编号": "EQ-05"},
        {"批次号": "B06", "产线": "B线", "工序": "焊接", "质检结果": "合格", "产量": 700, "工时": 5.5, "生产日期": "2025-11-21", "设备编号": "EQ-06"},
        {"批次号": "B07", "产线": "B线", "工序": "喷涂", "质检结果": None, "产量": None, "工时": 99999.0, "生产日期": "2026年06月24日", "设备编号": "EQ-07"},
        {"批次号": "B08", "产线": "C线", "工序": "总装", "质检结果": "PASS", "产量": 300, "工时": 0.1, "生产日期": "2026-07-28", "设备编号": "EQ-08"},
        {"批次号": "B09", "产线": "C线", "工序": "冲压", "质检结果": "合格", "产量": 450, "工时": 4.0, "生产日期": None, "设备编号": "EQ-09"},
        {"批次号": "B01", "产线": "A线", "工序": "冲压", "质检结果": "不合格", "产量": 1000, "工时": 8.0, "生产日期": "2025-01-06", "设备编号": "EQ-01"},
    ),
    aliases={"产线": "产线", "质检结果": "质检结果", "产量": "产量", "生产日期": "生产日期", "批次号": "批次号"},
    rename_map={"产线": "line", "质检结果": "qc_result", "产量": "output_qty", "生产日期": "produced_at"},
    replacements={"质检结果": {"合格": "PASS", "不合格": "NG"}},
    category_columns=("产线", "工序", "质检结果"),
    numeric_columns=("产量", "工时"),
    date_columns=("生产日期",),
    sample_notes="重复批次号、0 产量、99999 异常工时、缺失质检/产量/日期、PASS/NG 同义词",
))

# 电商商品维表：用于多源关联场景，品类唯一，不造成 join 膨胀。
PRODUCT_DIM_ROWS: Tuple[Dict[str, Any], ...] = (
    {"品类": "数码", "负责人": "张敏", "类目编码": "CAT-DIG"},
    {"品类": "家居", "负责人": "李强", "类目编码": "CAT-HOME"},
    {"品类": "服饰", "负责人": "王芳", "类目编码": "CAT-FASHION"},
)


@dataclass
class DatasetVariant:
    """一次场景运行所用的具体数据形态。"""
    dataset: IndustryDataset
    variant: str
    scale: int = 1
    seed: int = 20261117

    @property
    def frame(self) -> pd.DataFrame:
        return build_frame(self.dataset, self.variant, self.scale, self.seed)

    @property
    def columns(self) -> List[str]:
        return list(self.frame.columns)

    @property
    def aliases(self) -> Dict[str, str]:
        return self.dataset.aliases_for(self.variant)

    @property
    def colmap(self) -> Dict[str, str]:
        return self.dataset.canonical_to_actual(self.variant)

    def col(self, canonical: str) -> str:
        return self.colmap.get(canonical, canonical)


def build_frame(
    dataset: IndustryDataset,
    variant: str = "baseline",
    scale: int = 1,
    seed: int = 20261117,
) -> pd.DataFrame:
    """按变体构造 DataFrame。tokens 可组合，如 renamed+replaced+shuffled。"""
    tokens = _tokens(variant)
    frame = pd.DataFrame([dict(r) for r in dataset.rows], columns=list(dataset.columns))

    if scale > 1:
        blocks = []
        for idx in range(scale):
            block = frame.copy()
            block[dataset.key] = block[dataset.key].astype(str) + f"-S{idx:03d}"
            blocks.append(block)
        frame = pd.concat(blocks, ignore_index=True)

    if "replaced" in tokens:
        for column, mapping in dataset.replacements.items():
            if column in frame.columns:
                frame[column] = frame[column].replace(mapping)

    if "format_mix" in tokens:
        frame = _apply_format_mix(frame, dataset)

    if "shuffled" in tokens:
        frame = frame.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    else:
        frame = frame.reset_index(drop=True)

    if "renamed" in tokens:
        frame = frame.rename(columns=dataset.rename_map)
    return frame


def _apply_format_mix(frame: pd.DataFrame, dataset: IndustryDataset) -> pd.DataFrame:
    """金额加 ¥/千分位/万、日期混用斜杠与中文、类别加全角空格、空值用 NULL/—。"""
    out = frame.copy()
    null_tokens = ["NULL", "—", ""]
    for idx, column in enumerate(dataset.numeric_columns):
        if column not in out.columns:
            continue
        values: List[Any] = []
        for row_idx, value in enumerate(out[column].tolist()):
            if pd.isna(value):
                values.append(null_tokens[(row_idx + idx) % len(null_tokens)])
            elif isinstance(value, (int, float)) and abs(value) >= 10000 and row_idx % 3 == 1:
                values.append(f"{value / 10000:.2f}万")
            else:
                values.append(f"¥{float(value):,.2f}" if row_idx % 2 == 0 else str(value))
        out[column] = values
    # 类别列刻意不加空格：字段值同义词由 replaced 变体单独覆盖，
    # 避免把「格式波动」与「筛选键被污染」混成同一个失败原因。
    for idx, column in enumerate(dataset.date_columns):
        if column not in out.columns:
            continue
        converted: List[Any] = []
        for row_idx, value in enumerate(out[column].tolist()):
            if pd.isna(value) or str(value).strip() in {"NULL", "—", ""}:
                converted.append(null_tokens[row_idx % len(null_tokens)])
                continue
            text = str(value).replace("年", "-").replace("月", "-").replace("日", "")
            parts = [p for p in text.replace("/", "-").split("-") if p]
            if len(parts) >= 3:
                y, m, d = parts[:3]
                if row_idx % 3 == 0:
                    converted.append(f"{y}/{int(m)}/{int(d)}")
                elif row_idx % 3 == 1:
                    converted.append(f"{y}年{int(m)}月{int(d)}日")
                else:
                    converted.append(f"{y}-{int(m):02d}-{int(d):02d}")
            else:
                converted.append(value)
        out[column] = converted
    return out


def write_sample(frame: pd.DataFrame, path: Any) -> str:
    """写出 UTF-8 BOM CSV，保持中文列名在 Excel/浏览器直接可读。"""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    return str(target.resolve())


__all__ = [
    "DATASETS", "PRODUCT_DIM_ROWS", "DatasetVariant", "IndustryDataset",
    "build_frame", "dataset_by_id", "write_sample",
]

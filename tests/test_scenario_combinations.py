# -*- coding: utf-8 -*-
"""组合场景回归：严格走前端 /plan → /execute → /result。

场景定义见 tests/scenario_combinations.py；逐条执行证据与报告由
verify_scenario_matrix.py 生成。本文件保证任一场景回退都会在 pytest 中失败。
"""
from __future__ import annotations

import pytest

from tests.scenario_combinations import SCENARIOS
from verify_scenario_matrix import _check, _run


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_end_to_end(scenario, tmp_path):
    record = _run(scenario, tmp_path)
    assert _check(scenario, record) == [], record.get("problems")
    assert record["plan_status"] == 200
    assert record["execute_status"] == 302
    assert record["result_page_status"] == 200

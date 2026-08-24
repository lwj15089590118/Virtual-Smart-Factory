# -*- coding: utf-8 -*-
"""
tests/test_plant_selftest.py —— pytest 薄壳：把 selftest.py 既有用例接入行业标准框架
====================================================================================================
设计原则（薄壳纪律）：
    1. 零重复实现——直接调用 selftest.py 的用例函数；用例内部自带行为断言，
       抛出 AssertionError 即本测试失败。两套入口永远测同一份逻辑，
       不存在"pytest 全绿但 selftest 挂了"的分裂；
    2. 本文件不写任何业务断言，只负责 转接 / 标记 / 展示顺序；
    3. B1 系统级冒烟（600 仿真秒加速联跑）打 smoke 标记：
        pytest                    # 全量（含冒烟，CI 用）
        pytest -m "not smoke"     # 快速单元层（日常开发）
运行前提：
    pip install -r requirements-dev.txt   （pytest/pytest-cov/ruff）
"""

import pytest

import selftest

# 与 selftest.main() 完全一致的执行顺序（A 模块级 → B Web/AGV → C 算法/MES/EMS）
_CASES = [
    ("A1", selftest.case_clock),
    ("A2", selftest.case_bus),
    ("A3", selftest.case_device),
    ("A4", selftest.case_injector),
    ("A5", selftest.case_assembly_logic),
    ("A6", selftest.case_vision_rule),
    ("A7", selftest.case_palletizing_pattern),
    ("A8", selftest.case_warehouse_structs),
    ("A9", selftest.case_finite_feeder),
    ("B2", selftest.case_web_api),
    ("B3", selftest.case_agv_loop),
    ("B4", selftest.case_agv_recharge),
    ("C1", selftest.case_vision_algo),
    ("C2", selftest.case_mes_trace),
    ("C3", selftest.case_ems_energy_health),
    ("C4", selftest.case_mes_order_lifecycle),
]


@pytest.mark.parametrize("fn", [c[1] for c in _CASES], ids=[c[0] for c in _CASES])
def test_selftest_case(fn):
    """转接一个自检用例：函数自带断言，返回的详情字符串仅用于人工阅读。"""
    detail = fn()
    assert isinstance(detail, str) and detail, "用例应返回非空详情文本"


@pytest.mark.smoke
def test_b1_full_plant_smoke():
    """B1 系统级冒烟：600 仿真秒加速联跑 + 物料守恒等七项不变量。"""
    detail = selftest.smoke_full_plant()
    assert isinstance(detail, str) and detail

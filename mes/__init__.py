# -*- coding: utf-8 -*-
"""
mes/ —— 班次3新增：制造执行系统（MES）
========================================
组成：
    order_model.py   工单/批次数据模型 + 产品→托盘→库位全链路追溯索引
    mes_engine.py    MES 引擎（订阅事件总线自动报工 + 追溯反查 + OEE 近似口径）
    jsonl_replay.py  事件总线 JSONL 回放器（离线重建 MES 台账；覆盖全部轮转段+回放对账）

假设记录：
    - 本包只用标准库，不引入额外依赖。
"""


def _bind():
    """把项目根加入 sys.path，保证直接运行子模块时导入可用。"""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bind()

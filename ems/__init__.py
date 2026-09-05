# -*- coding: utf-8 -*-
"""
ems/ —— 阶段3新增：能源管理与健康管理（EMS）
==============================================
组成：
    energy_model.py   设备能耗模型（状态功率曲线 × 时长 → kWh，仿真验证值）
    health_monitor.py 设备健康评分（滚动窗口特征提取 + 维护建议）

假设记录：
    - 本包只用标准库+numpy（numpy 仅用于数值舍入辅助，可整体脱依赖）。
"""


def _bind():
    """把项目根加入 sys.path，保证直接运行子模块时导入可用。"""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bind()

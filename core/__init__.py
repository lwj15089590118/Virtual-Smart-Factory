# -*- coding: utf-8 -*-
"""
core 包 —— 仿真内核（时钟 / 事件总线 / 设备基类 / 故障注入器）
=============================================================
阶段1交付。后续 SCADA、MES、EMS、健康管理模块均建立在 core 之上，
只允许通过本包暴露的公开接口访问内核能力。
"""

from core.sim_clock import SimClock        # noqa: F401  仿真时钟引擎
from core.event_bus import EventBus, EventTypes  # noqa: F401  事件总线
from core.device_base import DeviceBase, DeviceState, IOPoint  # noqa: F401  设备基类
from core.fault_injector import FaultInjector  # noqa: F401  故障注入器

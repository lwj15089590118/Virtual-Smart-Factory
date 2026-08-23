# -*- coding: utf-8 -*-
"""
lines 包 —— 产线层单元（装配 / 视觉质检 / 码垛 / 立体库）
=========================================================
班次1交付。单元之间只通过"产品对象队列 + 事件总线"衔接，
班次2 的 AGV 调度、班次3 的视觉算法都挂接在这些接缝上。
"""

from lines.product import Product, PalletRecord  # noqa: F401  物流数据结构
from lines.unit_assembly import UnitAssembly     # noqa: F401  装配单元
from lines.unit_vision import UnitVision         # noqa: F401  视觉质检单元
from lines.unit_palletizing import UnitPalletizing  # noqa: F401  码垛单元
from lines.warehouse import Warehouse            # noqa: F401  立体库

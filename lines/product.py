# -*- coding: utf-8 -*-
"""
lines/product.py —— 物流数据结构（产品 / 托盘记录）
====================================================
定义在单元之间流动的纯数据对象。全部字段可 JSON 序列化，
阶段2 的 Web 3D 垛型展示、MES 报工直接复用。

假设记录：
    - 产品只携带"身份证"与质检结论，不带工艺过程数据（过程数据走事件总线）。
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Product:
    """一件在产线上流动的产品（仿真对象）。"""
    product_id: str                     # 全局唯一 ID，如 P00000001
    born_at: float                      # 装配完成时刻（仿真秒）
    source_unit: str                    # 出生单元（装配单元号）
    qc_result: Optional[str] = None     # "OK" / "NG"；质检前为 None
    qc_dim: Optional[float] = None      # 关键尺寸测量值 mm（仿真验证值）
    rework: bool = False                # 是否已进入返修道
    pallet_id: Optional[str] = None     # 所在托盘号（码垛后回填）

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id, "born_at": self.born_at,
            "source_unit": self.source_unit, "qc_result": self.qc_result,
            "qc_dim": self.qc_dim, "rework": self.rework,
            "pallet_id": self.pallet_id,
        }


@dataclass
class PalletRecord:
    """一个满托的完整档案（垛满后随 AGV 呼叫事件流转）。"""
    pallet_id: str                      # 托盘号，如 PLT000001
    boxes: List[dict] = field(default_factory=list)  # 每箱 {seq,x,y,z,px_mm,py_mm,pz_mm,product_id}
    completed_at: float = 0.0           # 垛满时刻（仿真秒）
    location: str = "PAL-OUT"           # 当前位置：PAL-OUT(码垛出口) → WH 库位号

    @property
    def box_count(self) -> int:
        return len(self.boxes)

    def to_dict(self) -> dict:
        return {"pallet_id": self.pallet_id, "box_count": self.box_count,
                "completed_at": self.completed_at, "location": self.location,
                "boxes": self.boxes}

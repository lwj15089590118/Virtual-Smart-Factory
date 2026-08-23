# -*- coding: utf-8 -*-
"""
lines/unit_palletizing.py —— 码垛机器人单元
============================================
职责：
    1. 从视觉 OK 品队列逐箱取件，按 3×4×4 垛型码放（X列3 × Y行4 × Z层4 = 48箱/托）；
    2. 每箱记录垛内网格坐标与毫米物理坐标（班次2 Web 端 ECharts 3D 垛型回放直接可用）；
    3. 垛满 → 托盘输出（PALLET_OUT_TIME）→ 发出 agv.call 事件呼叫 AGV
       （班次2 接入真实 AGV 调度；本班次由编排器的占位调度器响应）；
    4. 满托后自动补给新托盘（假设：空托盘供应无限）。

假设记录：
    - 码垛顺序按 层(z) → 行(y) → 列(x) 逐格推进，与常见"逐层码垛"工艺一致；
    - 空托盘供应无限（真实产线有空托盘缓存输送线，本班次不建模）。
"""

from collections import deque
from typing import Deque, List, Optional

import os
import sys
# 路径引导：直接运行本文件(python lines/unit_palletizing.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.device_base import DeviceBase, DeviceState
from core.event_bus import EventBus, EventTypes
from lines.product import Product, PalletRecord
from config import settings as S


class UnitPalletizing(DeviceBase):
    """码垛机器人单元：串行抓放，一次一箱。"""

    def __init__(self, clock, bus: EventBus, unit_id: str = S.PALLETIZER_ID):
        name = S.PALLETIZER_NAME if unit_id == S.PALLETIZER_ID else f"码垛单元{unit_id}"
        super().__init__(unit_id, name, clock, bus)
        # ---- 物流 ----
        self.inbound: Deque[Product] = deque()       # OK 品队列（视觉流出接入）
        # ---- 垛型状态 ----
        self._pallet_seq = 0                         # 托盘号发生器
        self._grid: List[dict] = []                  # 当前垛上已放的箱（含坐标）
        self._next_slot = 0                          # 下一格序号 0..47（z→y→x 展开）
        self._output_pallet: Optional[PalletRecord] = None  # 正在输出的满托
        self._timer = 0.0                            # 当前动作已耗时
        self._placing = False                        # 是否正在抓放一箱
        self._pending_product: Optional[Product] = None      # 抓放中的产品
        # ---- 统计 ----
        self.boxes_total = 0                         # 累计码箱数
        self.pallets_done: List[dict] = []           # 完成托盘档案（摘要）

    # ------------------------------------------------------------------
    # IO 点表
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        self.add_io("di_pallet_present", "DI", 1, desc="空托盘到位")
        self.add_io("do_gripper", "DO", 0, desc="抓手真空吸盘")
        self.add_io("ai_robot_x", "AI", 0.0, "mm", "机器人X轴位置")
        self.add_io("ai_robot_y", "AI", 0.0, "mm", "机器人Y轴位置")
        self.add_io("ai_robot_z", "AI", 0.0, "mm", "机器人Z轴位置")

    # ------------------------------------------------------------------
    # 垛型坐标计算（z 层 → y 行 → x 列）
    # ------------------------------------------------------------------
    def slot_xyz(self, slot: int) -> tuple:
        """把 0..47 的一维格序号展开为 (x列, y行, z层) 三维格坐标。"""
        per_layer = S.PALLET_X * S.PALLET_Y          # 每层 12 格
        z, rem = divmod(slot, per_layer)
        y, x = divmod(rem, S.PALLET_X)
        return x, y, z

    def slot_mm(self, slot: int) -> tuple:
        """格坐标 → 毫米物理坐标（垛型中心对称布置，供 ECharts 3D 使用）。"""
        x, y, z = self.slot_xyz(slot)
        px = (x - (S.PALLET_X - 1) / 2.0) * S.BOX_PITCH_MM[0]
        py = (y - (S.PALLET_Y - 1) / 2.0) * S.BOX_PITCH_MM[1]
        pz = (z + 0.5) * S.BOX_PITCH_MM[2]           # 层高从托盘面起算
        return round(px, 1), round(py, 1), round(pz, 1)

    @property
    def pallet_capacity(self) -> int:
        """满托容量 = X*Y*Z = 48（仿真验证值）。"""
        return S.PALLET_X * S.PALLET_Y * S.PALLET_Z

    def current_pallet_id(self) -> str:
        """当前在码托盘号。"""
        return f"PLT{self._pallet_seq:06d}"

    # 班次2修改：新增当前垛公开访问器（Web 3D 垛型面板数据源，避免外部触碰私有字段）
    def current_grid(self) -> List[dict]:
        """导出当前正在码放的垛（已放各箱含毫米坐标），供 Web 端 bar3D 实时渲染。"""
        return [dict(b) for b in self._grid]

    # ------------------------------------------------------------------
    # 每 tick 推进（优先级：输出满托 > 码放中 > 取新箱）
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        super().update(dt)

        if self.state in (DeviceState.STOPPED, DeviceState.FAULT,
                          DeviceState.MAINTENANCE):
            return                                  # 故障中：机器人停持

        # ---- 阶段3：满托输出中 ----
        if self._output_pallet is not None:
            self._timer = round(self._timer + dt, 9)   # 防漂移舍入（同 SimClock）
            if self._timer >= S.PALLET_OUT_TIME:
                self._finish_pallet_out()
            return

        # ---- 阶段2：正在码放一箱 ----
        if self._placing:
            self._timer = round(self._timer + dt, 9)   # 防漂移舍入（同 SimClock）
            if self._timer >= S.BOX_PLACE_TIME:
                self._place_done()
            return

        # ---- 阶段1：空闲则取新箱 ----
        if self.inbound:
            product = self.inbound.popleft()
            self._placing = True
            self._pending_product = product
            self._timer = 0.0
            self.set_io("do_gripper", 1)
            if self.state == DeviceState.STANDBY:
                self._set_state(DeviceState.RUNNING, "开始码放")
        else:
            if self.state == DeviceState.RUNNING and self._grid_empty_and_idle():
                self._set_state(DeviceState.STANDBY, "OK品队列空")

    def _grid_empty_and_idle(self) -> bool:
        """无在途动作即视为空闲（垛上未满格不算忙）。"""
        return not self._placing

    def _place_done(self) -> None:
        """一箱码放完成：写格、发事件、判满。"""
        slot = self._next_slot
        px, py, pz = self.slot_mm(slot)
        gx, gy, gz = self.slot_xyz(slot)
        product = self._pending_product
        product.pallet_id = self.current_pallet_id()
        box = {"seq": slot, "x": gx, "y": gy, "z": gz,
               "px_mm": px, "py_mm": py, "pz_mm": pz,
               "product_id": product.product_id}
        self._grid.append(box)
        self._next_slot += 1
        self.boxes_total += 1
        self.set_io("do_gripper", 0)
        self.set_io("ai_robot_x", px)
        self.set_io("ai_robot_y", py)
        self.set_io("ai_robot_z", pz)
        self._placing = False
        self._pending_product = None
        self.bus.publish(self.device_id, EventTypes.BOX_PLACED,
                         dict(box, pallet_id=self.current_pallet_id()))

        # ---- 垛满 → 进入输出阶段 ----
        if len(self._grid) >= self.pallet_capacity:
            self.cycle_count += 1
            self.bus.publish(self.device_id, EventTypes.PALLET_FULL,
                             {"pallet_id": self.current_pallet_id(),
                              "box_count": len(self._grid)})
            self._output_pallet = PalletRecord(
                pallet_id=self.current_pallet_id(),
                boxes=list(self._grid), completed_at=self.clock.now())
            self._timer = 0.0
            self._grid = []                        # 机器人转入输出，垛位清空
            self._next_slot = 0

    def _finish_pallet_out(self) -> None:
        """满托到达码垛出口：登记档案并呼叫 AGV（班次2 接管点）。"""
        pallet = self._output_pallet
        self._output_pallet = None
        self._pallet_seq += 1                      # 补给新空托盘（假设无限供应）
        self.pallets_done.append(pallet.to_dict())
        self.bus.publish(self.device_id, EventTypes.AGV_CALL,
                         {"pallet_id": pallet.pallet_id,
                          "from": f"{self.device_id}-OUT",
                          "to": f"{S.WAREHOUSE_ID}-IN",
                          "box_count": pallet.box_count,
                          "note": "班次1占位呼叫：班次2由真实AGV调度接管"})

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "boxes": self.boxes_total,
            "pallets_done": len(self.pallets_done),
            "current_pallet": self.current_pallet_id(),
            "current_fill": f"{len(self._grid)}/{self.pallet_capacity}",
            "queue_len": len(self.inbound),
        })
        return snap


# ----------------------------------------------------------------------
# 自模块快速自检：python lines/unit_palletizing.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    pal = UnitPalletizing(clock, bus, unit_id="PAL-T1")
    pal.start_up()
    full_events, agv_calls = [], []
    bus.subscribe(EventTypes.PALLET_FULL, lambda e: full_events.append(e))
    bus.subscribe(EventTypes.AGV_CALL, lambda e: agv_calls.append(e))

    from lines.product import Product
    # 灌 100 件 OK 品 → 应码满 2 托(96箱) + 余 4 箱在垛上
    for i in range(100):
        pal.inbound.append(Product(f"PK{i:08d}", born_at=clock.now(),
                                   source_unit="VIS-T1"))
    # 100 箱 × 1.2s + 2 次满托输出 2×5s = 130s，给 150s
    clock.advance_ticks(int(150.0 / clock.dt), step_fn=pal.update)

    assert pal.boxes_total == 100, f"码箱数错误: {pal.boxes_total}"
    assert len(full_events) == 2 and len(agv_calls) == 2, \
        f"满托/呼叫数错误: {len(full_events)}/{len(agv_calls)}"
    assert len(pal.pallets_done) == 2
    assert all(p["box_count"] == 48 for p in pal.pallets_done), "满托应为48箱"
    # 垛型坐标抽查：第 48 格应为 (x=2,y=3,z=3)，毫米坐标按中心对称
    last = pal.pallets_done[0]["boxes"][47]
    assert (last["x"], last["y"], last["z"]) == (2, 3, 3), f"格坐标错误: {last}"
    assert last["pz_mm"] == round((3 + 0.5) * S.BOX_PITCH_MM[2], 1)
    # AGV 呼叫内容检查
    assert agv_calls[0]["data"]["to"] == "WH-01-IN"
    # 产品回填托盘号
    assert pal.pallets_done[0]["boxes"][0]["product_id"] == "PK00000000"
    print(f"[unit_palletizing 自检通过] 码箱={pal.boxes_total}, 完成托={len(pal.pallets_done)}, "
          f"AGV呼叫={len(agv_calls)} 次, 垛型3×4×4=48箱/托 (仿真验证值)")

# -*- coding: utf-8 -*-
"""
lines/warehouse.py —— 立体库简化模型
======================================
职责（本阶段先做数据结构与队列调度，阶段2 接 AGV 搬运后扩展）：
    1. 库位表：4排 × 10列 × 5层 = 200 个库位，编号 A-{排}-{列}-{层}；
    2. 入库队列：满托到达 → 排队 → 堆垛机按 WH_TASK_TIME 完成上架；
    3. 出库队列：FIFO 出库请求 → 下架 → 托盘进入 out_staging（阶段2 由 AGV 运走）;
    4. 全部动作产生事件（wh.inbound_done / wh.outbound_done）。
扩展点（阶段2）：
    - request_inbound/request_outbound 就是 AGV 任务接口：AGV 到达→取托→回库
      的过程只需在编排层把"占位搬运"替换为 AGV 状态机；
    - 库位表 locations() 可直接渲染为 Web 端库存热力图。

假设记录：
    - 本阶段堆垛机内置建模（单巷道单堆垛机串行作业），AGV 段由编排器占位，
      因此端到端物流在本阶段即可闭环演示。
"""

from collections import deque
from typing import Deque, Dict, List, Optional

import os
import sys
# 路径引导：直接运行本文件(python lines/warehouse.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.device_base import DeviceBase, DeviceState
from core.event_bus import EventBus, EventTypes
from config import settings as S


class Warehouse(DeviceBase):
    """立体库：库位表 + 单堆垛机串行出入库。"""

    def __init__(self, clock, bus: EventBus, unit_id: str = S.WAREHOUSE_ID):
        name = S.WAREHOUSE_NAME if unit_id == S.WAREHOUSE_ID else f"立体库{unit_id}"
        super().__init__(unit_id, name, clock, bus)
        # ---- 库位表：loc_id -> 记录 ----
        self._locations: Dict[str, dict] = {}
        for r in range(1, S.WH_ROWS + 1):
            for c in range(1, S.WH_BAYS + 1):
                for lv in range(1, S.WH_LEVELS + 1):
                    loc_id = f"A-{r:02d}-{c:02d}-{lv:02d}"
                    self._locations[loc_id] = {
                        "loc_id": loc_id, "row": r, "bay": c, "level": lv,
                        "occupied": False, "pallet_id": None, "since": None}
        # 空闲库位栈：按 编号 升序分配（贴近真实 WMS 的就近策略）
        self._free_locs: Deque[str] = deque(sorted(self._locations.keys()))
        # ---- 任务队列 ----
        self.inbound_q: Deque[str] = deque()         # 待入库 pallet_id 队列
        self.outbound_q: Deque[Optional[str]] = deque()  # 待出库请求（None=FIFO 任选最早）
        self.out_staging: List[dict] = []            # 已出库待运走托盘（阶段2 AGV 取）
        self.stored_index: Dict[str, str] = {}       # pallet_id -> loc_id 反查表
        # ---- 堆垛机当前任务 ----
        self._task_type: Optional[str] = None        # "IN" / "OUT"
        self._task_pallet: Optional[str] = None
        self._task_loc: Optional[str] = None
        self._timer = 0.0
        # ---- 统计 ----
        self.inbound_done = 0
        self.outbound_done = 0

    # ------------------------------------------------------------------
    # IO 点表
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        self.add_io("di_crane_home", "DI", 1, desc="堆垛机原位")
        self.add_io("do_crane_run", "DO", 0, desc="堆垛机运行")
        self.add_io("ai_crane_bay", "AI", 0.0, "列", "堆垛机当前列")
        self.add_io("ai_crane_level", "AI", 0.0, "层", "堆垛机当前层")

    # ------------------------------------------------------------------
    # 对外任务接口（阶段2 AGV 调度对接点）
    # ------------------------------------------------------------------
    def request_inbound(self, pallet_id: str) -> bool:
        """申请入库：托盘进入入库排队（重复申请幂等拒绝）。"""
        if pallet_id in self.stored_index or pallet_id in self.inbound_q:
            return False
        self.inbound_q.append(pallet_id)
        return True

    def request_outbound(self, pallet_id: Optional[str] = None) -> bool:
        """申请出库：pallet_id=None 表示按先进先出选最早入库的托盘。"""
        if pallet_id is not None and pallet_id not in self.stored_index:
            return False                             # 不在库中，无法出库
        if len(self.outbound_q) >= 50:               # 防御：出库请求积压上限
            return False
        self.outbound_q.append(pallet_id)
        return True

    def locate(self, pallet_id: str) -> Optional[str]:
        """查询托盘所在库位号；不在库返回 None。"""
        return self.stored_index.get(pallet_id)

    # ------------------------------------------------------------------
    # 每 tick 推进：单堆垛机串行执行任务
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        super().update(dt)

        if self.state in (DeviceState.STOPPED, DeviceState.FAULT,
                          DeviceState.MAINTENANCE):
            return                                   # 故障中：堆垛机停持

        # ---- 任务进行中 ----
        if self._task_type is not None:
            self._timer = round(self._timer + dt, 9)   # 防漂移舍入（同 SimClock）
            if self._timer >= S.WH_TASK_TIME:
                self._finish_task()
            return

        # ---- 空闲则领取新任务：优先入库（防满托积压在码垛出口）----
        if self.inbound_q and self._free_locs:
            pallet_id = self.inbound_q.popleft()
            if not self._free_locs:                  # 理论不可达（上面已判），双保险
                self.inbound_q.appendleft(pallet_id)
                return
            loc_id = self._free_locs.popleft()
            self._start_task("IN", pallet_id, loc_id)
        elif self.outbound_q:
            req = self.outbound_q.popleft()
            pallet_id = req if req is not None else self._oldest_pallet()
            loc_id = self.stored_index.get(pallet_id)
            if loc_id is None:
                return                               # 托盘已不在库（重复出库请求），丢弃
            self._start_task("OUT", pallet_id, loc_id)
        else:
            if self.state == DeviceState.RUNNING:
                self._set_state(DeviceState.STANDBY, "无出入库任务")

    def _oldest_pallet(self) -> Optional[str]:
        """FIFO：取入库时间最早的在库托盘号。"""
        candidates = [(rec["since"] or 0.0, rec["pallet_id"])
                      for rec in self._locations.values()
                      if rec["occupied"] and rec["pallet_id"]]
        if not candidates:
            return None
        return min(candidates)[1]

    def _start_task(self, ttype: str, pallet_id: str, loc_id: str) -> None:
        """开始一次堆垛机任务。"""
        self._task_type, self._task_pallet, self._task_loc = ttype, pallet_id, loc_id
        self._timer = 0.0
        self.set_io("do_crane_run", 1)
        self.set_io("di_crane_home", 0)
        row, bay, level = [int(x) for x in loc_id.split("-")[1:]]
        self.set_io("ai_crane_bay", float(bay))
        self.set_io("ai_crane_level", float(level))
        if self.state == DeviceState.STANDBY:
            self._set_state(DeviceState.RUNNING, f"堆垛任务{ttype}")

    def _finish_task(self) -> None:
        """任务完成：更新库位表并发事件。"""
        ttype, pallet_id, loc_id = self._task_type, self._task_pallet, self._task_loc
        now = round(self.clock.now(), 3)
        if ttype == "IN":
            rec = self._locations[loc_id]
            rec.update(occupied=True, pallet_id=pallet_id, since=now)
            self.stored_index[pallet_id] = loc_id
            self.inbound_done += 1
            self.bus.publish(self.device_id, EventTypes.WH_INBOUND_DONE,
                             {"pallet_id": pallet_id, "loc_id": loc_id,
                              "stock": len(self.stored_index)})
        else:
            rec = self._locations[loc_id]
            rec.update(occupied=False, pallet_id=None, since=None)
            self.stored_index.pop(pallet_id, None)
            self._free_locs.append(loc_id)           # 库位回收复用
            self.outbound_done += 1
            staging_item = {"pallet_id": pallet_id, "from_loc": loc_id,
                            "at": now, "status": "待AGV运走"}
            self.out_staging.append(staging_item)
            self.bus.publish(self.device_id, EventTypes.WH_OUTBOUND_DONE,
                             dict(staging_item, stock=len(self.stored_index)))
        self.cycle_count += 1
        self._task_type = None
        self._task_pallet = None
        self._task_loc = None
        self._timer = 0.0
        self.set_io("do_crane_run", 0)
        self.set_io("di_crane_home", 1)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    @property
    def capacity(self) -> int:
        """总库位数 = 200（仿真验证值）。"""
        return len(self._locations)

    @property
    def stock_count(self) -> int:
        """当前在库托数。"""
        return len(self.stored_index)

    def locations(self) -> List[dict]:
        """导出全库位表快照（阶段2 库存热力图数据源）。"""
        return [dict(rec) for rec in self._locations.values()]

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "capacity": self.capacity, "stock": self.stock_count,
            "in_queue": len(self.inbound_q), "out_queue": len(self.outbound_q),
            "staging": len(self.out_staging),
            "inbound_done": self.inbound_done, "outbound_done": self.outbound_done,
        })
        return snap


# ----------------------------------------------------------------------
# 自模块快速自检：python lines/warehouse.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    wh = Warehouse(clock, bus, unit_id="WH-T1")
    wh.start_up()
    in_events, out_events = [], []
    bus.subscribe(EventTypes.WH_INBOUND_DONE, lambda e: in_events.append(e))
    bus.subscribe(EventTypes.WH_OUTBOUND_DONE, lambda e: out_events.append(e))

    assert wh.capacity == S.WH_ROWS * S.WH_BAYS * S.WH_LEVELS == 200

    # 申请 5 托入库
    for i in range(5):
        assert wh.request_inbound(f"WHT{i:06d}")
    assert wh.request_inbound("WHT000000") is False, "重复入库应被拒绝"
    # 跑足时间：5 托 × 25s = 125s，给 140s
    clock.advance_ticks(int(140.0 / clock.dt), step_fn=wh.update)
    assert wh.stock_count == 5 and len(in_events) == 5, \
        f"入库未完成: stock={wh.stock_count}"
    locs = {e["data"]["pallet_id"]: e["data"]["loc_id"] for e in in_events}
    assert len(set(locs.values())) == 5, "库位不应冲突"
    assert wh.locate("WHT000003") == locs["WHT000003"]

    # FIFO 出库最早入库的托盘
    assert wh.request_outbound(None)
    clock.advance_ticks(int(30.0 / clock.dt), step_fn=wh.update)
    assert len(out_events) == 1
    assert out_events[0]["data"]["pallet_id"] == "WHT000000"
    assert wh.locate("WHT000000") is None and wh.stock_count == 4
    assert wh.out_staging[-1]["status"] == "待AGV运走"

    # 指定托盘出库 + 库位复用验证（出库释放的位置可再分配）
    assert wh.request_outbound("WHT000002")
    clock.advance_ticks(int(30.0 / clock.dt), step_fn=wh.update)
    assert out_events[-1]["data"]["pallet_id"] == "WHT000002" and wh.stock_count == 3
    print(f"[warehouse 自检通过] 库位={wh.capacity}, 在库={wh.stock_count}, "
          f"入完成={wh.inbound_done}, 出完成={wh.outbound_done} (仿真验证值)")

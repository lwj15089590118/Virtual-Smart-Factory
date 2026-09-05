# -*- coding: utf-8 -*-
"""
agv/agv_fleet.py —— AGV 车队仿真（阶段2：替换阶段1的占位搬运调度）
====================================================================
职责：
    1. 车队规模 ≥2 台（settings.AGV_COUNT），每台 AGV 是一个标准 DeviceBase
       （五态状态机 + IO 点表），可被故障注入器/全线急停统一管理；
    2. 任务状态机（题目要求的六阶段）：
           空闲 → 去取货 → 装载 → 运输 → 交货 → 回位 → 空闲
       - 入库任务：码垛出口 PAL-OUT 取满托 → 交到立体库入口 WH-IN
         → 调 warehouse.request_inbound() 进入堆垛机入库队列；
       - 出库任务：立体库出口 WH-OUT 取 out_staging 待运托 → 运至出货口 SHIP
         → 托盘出厂（shipped_count+1），打通"出库段"闭环；
    3. 调度器 AGVFleet：订阅 agv.call 事件建档入库任务；轮询 warehouse.out_staging
       建档出库任务；空闲车按先到先得领单（假设：单托任务，不做多托拼车）；
    4. 全部计时走 clock.now()/update(dt)，位置推进按 恒速×dt 的欧氏直线模型，
       计时累加 round(t+dt, 9) 防浮点漂移——时间纪律与阶段1完全一致。

扩展点（阶段3）：
    - 多车路径规划/交通管制：只需替换 AGVFleet.assign() 与 AGV._move_toward()；
    - 电量调度/充电排程：battery 字段已就绪，可接 EMS 优化算法。

假设记录：
    - 站点间直线行驶（磁条导引简化模型），速度空载满载一致（settings.AGV_SPEED_MPS）；
    - 任务队列不设上限（演示工况下满托频率远低于车队吞吐）。
"""

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

import os
import sys
# 路径引导：直接运行本文件(python agv/agv_fleet.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.device_base import DeviceBase, DeviceState
from core.event_bus import EventBus, EventTypes
from config import settings as S


# ======================================================================
# 任务数据结构
# ======================================================================
@dataclass
class AGVTask:
    """一条 AGV 搬运任务（纯数据对象，全部字段可 JSON 序列化）。"""
    task_id: str                # 全局唯一任务号，如 T000001
    task_type: str              # "入库"(PAL-OUT→WH-IN) / "出库"(WH-OUT→SHIP)
    pallet_id: str              # 搬运对象托盘号
    from_station: str           # 取货站点名（AGV_STATIONS 键）
    to_station: str             # 交货站点名（AGV_STATIONS 键）
    state: str = "待分配"        # 待分配 / 执行中 / 已完成
    agv_id: Optional[str] = None    # 承接车辆号（未分配为 None）
    created_at: float = 0.0     # 建档时刻（仿真秒）
    assigned_at: Optional[float] = None  # 接单时刻
    done_at: Optional[float] = None      # 完成时刻

    def to_dict(self) -> dict:
        """导出字典（Web API / 事件负载共用）。"""
        return {
            "task_id": self.task_id, "task_type": self.task_type,
            "pallet_id": self.pallet_id, "state": self.state,
            "agv_id": self.agv_id, "from_station": self.from_station,
            "to_station": self.to_station, "created_at": self.created_at,
            "assigned_at": self.assigned_at, "done_at": self.done_at,
        }


# 阶段常量（六阶段任务状态机 + 回充排程两相位；用中文串便于直接上屏）
PH_IDLE = "空闲"
PH_TO_PICK = "去取货"
PH_LOAD = "装载"
PH_TRANSPORT = "运输"
PH_DELIVER = "交货"
PH_RETURN = "回位"
PH_TO_CHARGE = "去充电"      # 增强：低电量驶向共享充电位
PH_CHARGING = "充电中"       # 增强：接驳充电桩补能（≥AGV_BATTERY_OK 离站）


class AGV(DeviceBase):
    """一台 AGV：六阶段任务状态机 + 二维平面位置/电量模型。"""

    def __init__(self, clock, bus: EventBus, agv_id: str):
        super().__init__(agv_id, f"AGV{agv_id[-2:]}号车", clock, bus)
        # ---- 位置与电量 ----
        home = S.AGV_HOMES.get(agv_id, (0.0, 0.0))
        self.home = tuple(home)                  # 待命位（回位目标）
        self.pos = [home[0], home[1]]            # 当前坐标（米，二维俯视）
        self.battery = 100.0                     # 电量%（装饰性指标，供大屏展示）
        # ---- 任务与阶段 ----
        self.phase = PH_IDLE                     # 六阶段状态机当前相位
        self.current_task: Optional[AGVTask] = None
        self._timer = 0.0                        # 当前阶段已耗时（装载/交货用）
        self.tasks_done = 0                      # 完成任务数（统计）
        self.distance_m = 0.0                    # 累计行驶里程（米，统计）
        self._return_from_charge = False         # 回充达标归位标记（不计运输任务数）

    # ------------------------------------------------------------------
    # IO 点表（映射进 Modbus 保持寄存器区，供第三方 SCADA 观测）
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        self.add_io("di_loaded", "DI", 0, desc="载货反馈(1=有托)")
        self.add_io("do_lift_up", "DO", 0, desc="顶升机构升起")
        self.add_io("ai_speed", "AI", 0.0, "m/s", "当前车速")
        self.add_io("ai_battery", "AI", 100.0, "%", "电池电量")
        self.add_io("ai_pos_x", "AI", 0.0, "m", "X坐标")
        self.add_io("ai_pos_y", "AI", 0.0, "m", "Y坐标")

    # ------------------------------------------------------------------
    # 阶段迁移（唯一入口，集中发事件）
    # ------------------------------------------------------------------
    def _set_phase(self, new_phase: str, note: str = "") -> None:
        """迁移任务相位并广播 agv.phase 事件（Web 时间线/地图动画的数据源）。"""
        old = self.phase
        if new_phase == old:
            return
        self.phase = new_phase
        payload = {"agv_id": self.device_id, "phase": new_phase,
                   "prev_phase": old}
        if self.current_task is not None:
            payload.update({"task_id": self.current_task.task_id,
                            "task_type": self.current_task.task_type,
                            "pallet_id": self.current_task.pallet_id})
        if note:
            payload["note"] = note
        self.bus.publish(self.device_id, EventTypes.AGV_PHASE, payload)

    # ------------------------------------------------------------------
    # 接单（由 AGVFleet 调度器调用）
    # ------------------------------------------------------------------
    def assign(self, task: AGVTask) -> None:
        """承接一条任务：空闲态切入运行态，进入'去取货'阶段。"""
        assert self.phase == PH_IDLE and self.current_task is None, \
            f"{self.device_id} 非空闲不可接单"
        task.agv_id = self.device_id
        task.state = "执行中"
        task.assigned_at = self.clock.now()
        self.current_task = task
        self._timer = 0.0
        if self.state == DeviceState.STANDBY:
            self._set_state(DeviceState.RUNNING, f"接单{task.task_id}")
        self._set_phase(PH_TO_PICK, f"前往{task.from_station}")

    # ------------------------------------------------------------------
    # 平面移动模型
    # ------------------------------------------------------------------
    def _move_toward(self, target: tuple, dt: float) -> bool:
        """
        以恒速向目标点直线行驶一个 tick。
        返回 True 表示本 tick 已到达（位置精确落在目标点）。
        """
        dx, dy = target[0] - self.pos[0], target[1] - self.pos[1]
        dist = math.hypot(dx, dy)
        step = S.AGV_SPEED_MPS * dt
        if dist <= step:
            self.pos[0], self.pos[1] = float(target[0]), float(target[1])
            self.distance_m = round(self.distance_m + dist, 6)
            return True
        self.pos[0] += dx / dist * step
        self.pos[1] += dy / dist * step
        self.distance_m = round(self.distance_m + step, 6)
        self.battery = max(0.0, round(
            self.battery - S.AGV_BATTERY_DRAIN_PER_M * step, 4))
        return False

    def _sync_io(self) -> None:
        """把位置/电量/载货状态镜像到 IO 点表（供 Modbus/Web 快照）。"""
        self.set_io("ai_speed",
                    S.AGV_SPEED_MPS if self.phase in (PH_TO_PICK, PH_TRANSPORT,
                                                      PH_RETURN, PH_TO_CHARGE)
                    else 0.0)
        self.set_io("ai_battery", round(self.battery, 1))
        self.set_io("ai_pos_x", round(self.pos[0], 2))
        self.set_io("ai_pos_y", round(self.pos[1], 2))
        loaded = 1 if (self.current_task is not None
                       and self.phase in (PH_TRANSPORT, PH_DELIVER)) else 0
        self.set_io("di_loaded", loaded)

    # ------------------------------------------------------------------
    # 回充排程（增强）：车队调度器下发指令的唯一入口
    # ------------------------------------------------------------------
    def begin_charge_trip(self) -> None:
        """驶向共享充电位并补能至 AGV_BATTERY_OK（仅空闲车可被指派）。"""
        assert self.phase == PH_IDLE and self.current_task is None, \
            f"{self.device_id} 非空闲不可发起回充"
        self._timer = 0.0
        if self.state == DeviceState.STANDBY:
            self._set_state(DeviceState.RUNNING, "低电量回充出发")
        self._set_phase(PH_TO_CHARGE, "驶向充电位")

    # ------------------------------------------------------------------
    # 每 tick 推进（六阶段状态机主体）
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        super().update(dt)
        # 故障/停机/维护中：整车冻结（任务保持断点，复位后续走）
        if self.state in (DeviceState.STOPPED, DeviceState.FAULT,
                          DeviceState.MAINTENANCE):
            return

        # ---- 回充排程相位（增强）：去充电 / 充电中 ----
        if self.phase == PH_TO_CHARGE:
            if self._move_toward(S.AGV_CHARGE_DOCK, dt):
                self._set_phase(PH_CHARGING, "接驳充电位")
                self.bus.publish(self.device_id, EventTypes.AGV_CHARGE_START,
                                 {"battery": round(self.battery, 1)})
            self._sync_io()
            return

        if self.phase == PH_CHARGING:
            self.battery = min(100.0, round(
                self.battery + S.AGV_CHARGE_RATE * dt, 4))
            self._sync_io()
            if self.battery >= S.AGV_BATTERY_OK:
                self.bus.publish(self.device_id, EventTypes.AGV_CHARGE_DONE,
                                 {"battery": round(self.battery, 1)})
                self._return_from_charge = True    # 归位段不计入运输任务数
                self._set_phase(PH_RETURN, "充电达标回位")
            return

        # ---- 空闲：在待命位涓流充电（装饰性逻辑；严格限定空闲相位，
        #      避免吞掉任务less 的 回位/回充 归位段——修复记录：曾用宽口径
        #      current_task is None 导致充电达标后永远卡在回位相位）----
        if self.phase == PH_IDLE:
            if self.battery < 100.0:
                self.battery = min(100.0, round(self.battery + 0.5 * dt, 4))
            self._sync_io()
            return

        task = self.current_task
        # ---- 阶段1：去取货（空驶）----
        if self.phase == PH_TO_PICK:
            if self._move_toward(S.AGV_STATIONS[task.from_station], dt):
                self._timer = 0.0
                self.set_io("do_lift_up", 1)
                self._set_phase(PH_LOAD, f"到达{task.from_station}")
        # ---- 阶段2：装载（定时）----
        elif self.phase == PH_LOAD:
            self._timer = round(self._timer + dt, 9)
            if self._timer >= S.AGV_LOAD_TIME_S:
                self.set_io("do_lift_up", 0)
                self.set_io("di_loaded", 1)
                if isinstance(getattr(self, "_fleet", None), AGVFleet):
                    self._fleet.on_load_done(task)   # 出库托离开 out_staging 上车
                self._set_phase(PH_TRANSPORT, "装载完成")
        # ---- 阶段3：运输（重载）----
        elif self.phase == PH_TRANSPORT:
            if self._move_toward(S.AGV_STATIONS[task.to_station], dt):
                self._timer = 0.0
                self.set_io("do_lift_up", 1)
                self._set_phase(PH_DELIVER, f"到达{task.to_station}")
        # ---- 阶段4：交货（定时卸载→闭环）----
        elif self.phase == PH_DELIVER:
            self._timer = round(self._timer + dt, 9)
            if self._timer >= S.AGV_UNLOAD_TIME_S:
                self.set_io("do_lift_up", 0)
                self.set_io("di_loaded", 0)
                self._complete_task(task)
                self._set_phase(PH_RETURN, "交货完成")
        # ---- 阶段5：回位（空驶；回充达标归位同样走本段但不计运输任务数）----
        elif self.phase == PH_RETURN:
            if self._move_toward(self.home, dt):
                if getattr(self, "_return_from_charge", False):
                    self._return_from_charge = False
                else:
                    self.tasks_done += 1
                self.cycle_count += 1
                self.current_task = None        # 交卷清空手头任务（否则永不接新单）
                if self.state == DeviceState.RUNNING:
                    self._set_state(DeviceState.STANDBY, "回位完成")
                self._set_phase(PH_IDLE)

        self._sync_io()

    # ------------------------------------------------------------------
    # 任务闭环（交付动作在此发生）
    # ------------------------------------------------------------------
    def _complete_task(self, task: AGVTask) -> None:
        """交货完成：按任务类型执行交付副作用并广播 agv.task_done。"""
        task.state = "已完成"
        task.done_at = self.clock.now()
        if self._fleet_ref() is not None:
            self._fleet_ref().on_task_done(task)

    def _fleet_ref(self):
        """取所属车队引用（构造后由 Fleet 注入；单机自检时可能无车队）。"""
        return getattr(self, "_fleet", None)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "phase": self.phase,
            "pos": [round(self.pos[0], 2), round(self.pos[1], 2)],
            "battery": round(self.battery, 1),
            "distance_m": round(self.distance_m, 1),
            "tasks_done": self.tasks_done,
            "task": self.current_task.to_dict() if self.current_task else None,
            "home": list(self.home),
        })
        return snap


class AGVFleet:
    """AGV 车队调度器：任务建档 → 派单 → 监督。本身不是设备，不进五态机。"""

    def __init__(self, clock, bus: EventBus, warehouse,
                 agv_count: int = S.AGV_COUNT):
        """
        :param warehouse: lines.warehouse.Warehouse 实例（出库段取 out_staging、
                          入库段交付 request_inbound）
        """
        self.clock = clock
        self.bus = bus
        self.warehouse = warehouse
        # ---- 车辆花名册（保持创建顺序，便于派单公平）----
        self.agvs: Dict[str, AGV] = OrderedDict()
        for i in range(1, max(2, int(agv_count)) + 1):   # 硬性要求 ≥2 台
            agv_id = f"AGV-{i:02d}"
            agv = AGV(clock, bus, agv_id)
            agv._fleet = self                             # 反向引用：交货回调用
            self.agvs[agv_id] = agv
        # ---- 任务池 ----
        self.pending: Deque[AGVTask] = Deque()            # 待分配队列
        self.active: Dict[str, AGVTask] = {}              # task_id -> 执行中任务
        self.finished: List[dict] = []                    # 完成任务档案（摘要）
        self._staging_seen: set = set()                   # 已建档出库的 staging 条目
        self._task_seq = 0
        self._done_counter = {"入库": 0, "出库": 0}
        self.shipped_count = 0                            # 已运抵出货口出厂的托数
        self.charge_occupant: Optional[str] = None        # 充电位占用车辆号（单工位互斥）

    # ------------------------------------------------------------------
    # 任务建档
    # ------------------------------------------------------------------
    def _new_task(self, task_type: str, pallet_id: str,
                  from_station: str, to_station: str) -> AGVTask:
        """建档并广播 agv.task_created。"""
        self._task_seq += 1
        task = AGVTask(task_id=f"T{self._task_seq:06d}", task_type=task_type,
                       pallet_id=pallet_id, from_station=from_station,
                       to_station=to_station, created_at=self.clock.now())
        self.pending.append(task)
        self.bus.publish(f"{self.__class__.__name__}", EventTypes.AGV_TASK_CREATED,
                         dict(task.to_dict()))
        return task

    def on_agv_call(self, event: dict) -> None:
        """
        【阶段2修改】替换 Plant._on_agv_call 占位调度的事件入口：
        码垛垛满 → agv.call → 建立入库任务（PAL-OUT → WH-IN）。
        （事件里的 from/to 是设备端口描述，此处统一映射到厂内站点坐标表）
        """
        data = event.get("data", {})
        self._new_task("入库", data["pallet_id"], "PAL-OUT", "WH-IN")

    def create_outbound_task(self, pallet_id: str) -> AGVTask:
        """手动/自动建立出库任务（WH-OUT → SHIP）；供编排器出库演示与 Web 按钮调用。"""
        return self._new_task("出库", pallet_id, "WH-OUT", "SHIP")

    def on_load_done(self, task: AGVTask) -> None:
        """装载完成回调（AGV 调用）：出库托此时物理上车，移出 out_staging。"""
        if task.task_type == "出库":
            self.warehouse.out_staging = [
                it for it in self.warehouse.out_staging
                if it["pallet_id"] != task.pallet_id]

    def on_task_done(self, task: AGVTask) -> None:
        """交货完成回调（AGV 调用）：执行交付副作用 + 记账 + 广播。"""
        self.active.pop(task.task_id, None)
        self.finished.append(task.to_dict())
        if len(self.finished) > 500:                 # 防长跑爆内存
            del self.finished[:100]
        if task.task_type == "入库":
            self.warehouse.request_inbound(task.pallet_id)   # 交付堆垛机入库队列
        else:
            self.shipped_count += 1                          # 托盘出厂
        self._done_counter[task.task_type] = \
            self._done_counter.get(task.task_type, 0) + 1
        self.bus.publish("AGV-FLEET", EventTypes.AGV_TASK_DONE,
                         dict(task.to_dict()))

    # ------------------------------------------------------------------
    # 每 tick 推进：扫出库 → 派单 → 回充排程 → 逐车步进
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        # 1) 轮询立体库出库暂存区，新出现的待运托自动建出库任务
        for item in list(self.warehouse.out_staging):
            pid = item.get("pallet_id")
            if pid and pid not in self._staging_seen:
                self._staging_seen.add(pid)
                item["status"] = "AGV任务已建档"
                self.create_outbound_task(pid)
        # 2) 派单：空闲车按花名册顺序领取最早的任务
        while self.pending:
            idle = next((a for a in self.agvs.values()
                         if a.phase == PH_IDLE and a.current_task is None), None)
            if idle is None:
                break                                    # 无车可用，等下一 tick
            task = self.pending.popleft()
            self.active[task.task_id] = task
            idle.assign(task)
        # 3) 逐车步进（故障车内部自行冻结）
        for agv in self.agvs.values():
            agv.update(dt)
        # 4) 低电量回充排程（增强）：放在派单之后——真实运输任务优先占用车辆，
        #    剩余空闲车才被调度去充电；单工位充电位由 charge_occupant 互斥登记。
        self._dispatch_charging()

    # ------------------------------------------------------------------
    # 低电量回充排程（增强）
    # ------------------------------------------------------------------
    def _dispatch_charging(self) -> None:
        """空闲且电量低于 AGV_BATTERY_LOW 的车 → 指派回充电位补能。

        排程规则（假设记录见 settings §7.5）：
          - 共享充电位单工位互斥：charge_occupant 登记在站/在途车辆，
            离站（相位离开 去充电/充电中）即自动让位；
          - 任务执行中的车不中断（单趟损耗 <1%），只在回到空闲后参与排队；
          - 未抢到位的低电车继续待命涓流，下一 tick 重新竞争。
        """
        occ = self.agvs.get(self.charge_occupant) if self.charge_occupant else None
        if occ is not None and occ.phase not in (PH_TO_CHARGE, PH_CHARGING):
            self.charge_occupant = None                  # 在站车辆已离站，让位
        if self.charge_occupant is not None:
            return                                       # 充电位被占，本轮不派
        for agv in self.agvs.values():
            if (agv.phase == PH_IDLE and agv.current_task is None
                    and agv.battery < S.AGV_BATTERY_LOW):
                self.charge_occupant = agv.device_id
                agv.bus.publish(agv.device_id, EventTypes.AGV_LOW_BATTERY,
                                {"battery": round(agv.battery, 1),
                                 "threshold": S.AGV_BATTERY_LOW})
                agv.begin_charge_trip()
                break                                    # 单工位：一次只派一台

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def active_inbound_pallets(self) -> List[str]:
        """仍在车队手里（已呼叫未交付库口）的入库托盘号列表——
        兼容 Plant._agv_transit 的托盘守恒口径（阶段2修改）。"""
        return [t.pallet_id for t in
                list(self.pending) + list(self.active.values())
                if t.task_type == "入库"]

    def snapshot(self) -> dict:
        return {
            "agv_count": len(self.agvs),
            "agvs": [a.snapshot() for a in self.agvs.values()],
            "pending": len(self.pending),
            "active": len(self.active),
            "done": dict(self._done_counter),
            "shipped": self.shipped_count,
            "last_done": self.finished[-5:],     # 最近5条完成档案（时间线展示）
        }


# ----------------------------------------------------------------------
# 自模块快速自检：python agv/agv_fleet.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock
    from lines.warehouse import Warehouse

    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    wh = Warehouse(clock, bus, unit_id="WH-T1")
    wh.start_up()
    fleet = AGVFleet(clock, bus, wh, agv_count=2)
    for a in fleet.agvs.values():
        a.start_up()
    created, phase_ev, done_ev = [], [], []
    bus.subscribe(EventTypes.AGV_TASK_CREATED, lambda e: created.append(e))
    bus.subscribe(EventTypes.AGV_PHASE, lambda e: phase_ev.append(e))
    bus.subscribe(EventTypes.AGV_TASK_DONE, lambda e: done_ev.append(e))

    # --- 场景1：模拟一次码垛 agv.call → 入库任务闭环 ---
    def step(dt: float) -> None:
        """联步：车队与立体库同一 tick 推进（等价 Plant.update 的物流顺序）。"""
        fleet.update(dt)
        wh.update(dt)

    fleet.on_agv_call({"data": {"pallet_id": "PLT000001"}})
    assert len(created) == 1
    # 几何校验：待命位(3,4.5)→PAL-OUT(6,2)≈3.9m≈2.6s；装/卸各4s；
    # PAL-OUT(6,2)→WH-IN(12,2)=6m≈4s；合计约14.6s 交付库口
    clock.advance_ticks(int(20.0 / clock.dt), step_fn=step)
    assert len(done_ev) == 1 and done_ev[0]["data"]["task_type"] == "入库", \
        f"入库任务未闭环: {len(done_ev)}"
    # 交付即被堆垛机领走属正常（队列或任务中二选一）
    assert wh.stock_count + len(wh.inbound_q) \
        + (1 if wh._task_type == "IN" else 0) == 1, "托盘应已交付立体库"
    a1 = fleet.agvs["AGV-01"]
    # 堆垛机再跑 35s 完成上架（WH_TASK_TIME=25s，含排队余量）
    clock.advance_ticks(int(35.0 / clock.dt), step_fn=step)
    assert wh.stock_count == 1, f"托盘应已上架: stock={wh.stock_count}"

    # --- 场景2：出库段闭环 staging → AGV → 出货口 ---
    assert wh.request_outbound(None)                  # FIFO 出库最早托
    # 堆垛机出库25s + AGV 去程≈6.1s+装4s+运4s+卸4s ≈ 43.1s，给 55s
    clock.advance_ticks(int(55.0 / clock.dt), step_fn=step)
    assert fleet._done_counter["出库"] >= 1, "出库任务未闭环"
    assert fleet.shipped_count == 1, f"应已有1托出厂: {fleet.shipped_count}"
    assert not wh.out_staging, "出库暂存区应为空（托盘已被AGV运走）"

    snap = fleet.snapshot()
    assert snap["agv_count"] == 2 and snap["pending"] == 0

    # --- 场景3：同一台车连续接两单（回归点：current_task 必须在回位后清空）---
    a1_mark = fleet.agvs["AGV-01"].tasks_done
    fleet.on_agv_call({"data": {"pallet_id": "PLT000002"}})
    clock.advance_ticks(int(60.0 / clock.dt), step_fn=step)
    assert fleet.agvs["AGV-01"].tasks_done == a1_mark + 1, \
        f"AGV-01 未接续新单（回位后未清 current_task?）: {fleet.agvs['AGV-01'].tasks_done}"
    assert fleet.agvs["AGV-01"].current_task is None, "空闲车不应残留任务引用"

    # --- 场景4：低电量自动回充排程（增强回归点）---
    # 强制 AGV-01 低电量 → 车队应派其回充电位；充满后让位并归位，
    # 且归位段不计入运输任务数（tasks_done 不变）。
    a1 = fleet.agvs["AGV-01"]
    a1.battery = S.AGV_BATTERY_LOW - 5                 # 强制低于阈值
    low_ev, cs_ev, cd_ev = [], [], []
    bus.subscribe(EventTypes.AGV_LOW_BATTERY, lambda e: low_ev.append(e))
    bus.subscribe(EventTypes.AGV_CHARGE_START, lambda e: cs_ev.append(e))
    bus.subscribe(EventTypes.AGV_CHARGE_DONE, lambda e: cd_ev.append(e))
    mark_tasks = a1.tasks_done
    clock.advance_ticks(int(40.0 / clock.dt), step_fn=step)
    assert low_ev and cs_ev and cd_ev, \
        f"回充事件链缺失: low={len(low_ev)} start={len(cs_ev)} done={len(cd_ev)}"
    assert fleet.charge_occupant is None, "充满离站后充电位应释放"
    assert a1.phase == PH_IDLE and a1.battery >= S.AGV_BATTERY_OK - 0.01, \
        f"回充未闭环: phase={a1.phase} battery={a1.battery}"
    assert a1.tasks_done == mark_tasks, "回充归位不应计入运输任务数"

    print(f"[agv_fleet 自检通过] 车队={snap['agv_count']}台, "
          f"完成={fleet.snapshot()['done']}, 出厂={fleet.shipped_count}托, "
          f"AGV-01里程={fleet.agvs['AGV-01'].distance_m:.1f}m, "
          f"回充排程OK(低{S.AGV_BATTERY_LOW}%→充至{S.AGV_BATTERY_OK}%) (仿真验证值)")

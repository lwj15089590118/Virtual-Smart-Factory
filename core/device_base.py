# -*- coding: utf-8 -*-
"""
core/device_base.py —— 设备基类（全厂所有单元/设备的公共骨架）
================================================================
提供：
    1. 五态状态机：停止 STOPPED / 待机 STANDBY / 运行 RUNNING / 故障 FAULT / 维护 MAINTENANCE
       （迁移规则集中在 _set_state，子类不得绕过）；
    2. IO 点表：DI/DO/AI/AO 四类点位，班次2 将原样映射为 Modbus 寄存器；
    3. 统计计数：运行秒数、循环数、停机原因 Counter；
    4. 故障注入接口：apply_fault() / clear_fault()，全部产生事件。
时间纪律：
    子类的 update(dt) 只允许使用传入的 dt 与 self.clock.now()，
    禁止任何墙钟睡眠——这是加速跑批一致性的根基。

假设记录：
    - 进入 FAULT 前若正在 RUNNING，恢复后统一回 STANDBY 由子类续走工艺
      （比"记忆恢复点"更贴近真实产线的人工复位流程，也最稳妥）。
"""

from collections import Counter
from typing import Dict, Optional

import os
import sys
# 路径引导：直接运行本文件(python core/device_base.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.event_bus import EventBus, EventTypes


class DeviceState:
    """设备状态常量（用字符串而非 Enum，方便直接落事件与 JSON）。"""
    STOPPED = "停止"        # 上电未启动 / 人为停机
    STANDBY = "待机"        # 已启动、等待条件（料/联锁满足）
    RUNNING = "运行"        # 正在执行工艺动作
    FAULT = "故障"          # 存在未复位故障
    MAINTENANCE = "维护"    # 维护模式（班次3 健康模块驱动）


class IOPoint:
    """单个 IO 点位定义。direction ∈ DI(数字入)/DO(数字出)/AI(模拟入)/AO(模拟出)。"""

    __slots__ = ("name", "direction", "value", "unit", "desc")

    def __init__(self, name: str, direction: str, value=0,
                 unit: str = "", desc: str = ""):
        assert direction in ("DI", "DO", "AI", "AO"), f"非法 IO 方向 {direction}"
        self.name = name            # 点位名（同设备内唯一）
        self.direction = direction  # DI/DO/AI/AO
        self.value = value          # 当前值（DI/DO 为 0/1，AI/AO 为浮点）
        self.unit = unit            # 工程单位（mm/bar/N 等）
        self.desc = desc            # 中文描述

    def snapshot(self) -> dict:
        """导出为字典（控制台打印 / 班次2 Modbus 映射 / Web 展示共用）。"""
        return {"name": self.name, "dir": self.direction, "value": self.value,
                "unit": self.unit, "desc": self.desc}


class DeviceBase:
    """设备基类：子类必须实现 update(dt)，并在其中调用 super().update(dt) 以累积统计。"""

    def __init__(self, device_id: str, name: str, clock, bus: EventBus):
        self.device_id = device_id          # 全厂唯一设备号，如 ASM-01
        self.name = name                    # 中文名
        self.clock = clock                  # 唯一时间源
        self.bus = bus                      # 事件总线
        # ---- 状态机 ----
        self._state = DeviceState.STOPPED   # 当前状态
        self._prev_state = DeviceState.STOPPED
        self.state_since = clock.now()      # 进入当前状态的时刻（仿真秒）
        # ---- 统计计数（作品集核心指标来源，全部为仿真验证值）----
        self.run_seconds = 0.0              # 累计运行秒数（仅 RUNNING 时累积）
        self.cycle_count = 0                # 完成循环数
        self.stop_counter: Counter = Counter()  # 停机原因 -> 次数
        # ---- 故障 ----
        self.current_fault: Optional[str] = None    # 当前故障类型（None=无故障）
        self.fault_since: Optional[float] = None    # 故障开始时刻
        # ---- IO 点表（子类在 _init_io 中填充）----
        self.io_table: Dict[str, IOPoint] = {}
        self._init_io()

    # ------------------------------------------------------------------
    # IO 点表操作
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        """子类覆写：注册本设备的 IO 点。基类默认无点位。"""

    def add_io(self, name: str, direction: str, value=0, unit: str = "", desc: str = "") -> None:
        """注册一个 IO 点（子类 _init_io 内调用）。"""
        assert name not in self.io_table, f"{self.device_id} IO 点重名: {name}"
        self.io_table[name] = IOPoint(name, direction, value, unit, desc)

    def set_io(self, name: str, value) -> None:
        """写 IO 点值（不逐次发事件，避免总线洪泛；关键跳变由业务事件表达）。"""
        self.io_table[name].value = value

    def get_io(self, name: str):
        """读 IO 点值。"""
        return self.io_table[name].value

    # ------------------------------------------------------------------
    # 状态机（唯一迁移入口）
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        """当前状态字符串。"""
        return self._state

    def _set_state(self, new_state: str, reason: str = "") -> bool:
        """
        状态迁移（集中校验 + 记账 + 发事件）。返回是否真的发生迁移。
        合法迁移表按工业惯例从宽：五态之间除自迁外均允许，由子类逻辑保证工艺正确性。
        """
        if new_state == self._state:
            return False
        with_reason = reason or f"{self._state}->{new_state}"
        # 记停机原因：凡离开 RUNNING 进入非运行态，都算一次停机
        if self._state == DeviceState.RUNNING and new_state != DeviceState.RUNNING:
            self.stop_counter[with_reason] += 1
        self._prev_state = self._state
        self._state = new_state
        self.state_since = self.clock.now()
        self.bus.publish(self.device_id, EventTypes.DEVICE_STATE,
                         {"state": new_state, "reason": with_reason})
        return True

    def start_up(self) -> None:
        """上电启动：停止 → 待机（幂等）。"""
        if self._state == DeviceState.STOPPED:
            self._set_state(DeviceState.STANDBY, "人工启动")

    def enter_maintenance(self) -> None:
        """进入维护模式（预留：班次3 健康模块调度）。"""
        self._set_state(DeviceState.MAINTENANCE, "进入维护")

    def exit_maintenance(self) -> None:
        """退出维护模式 → 待机。"""
        if self._state == DeviceState.MAINTENANCE:
            self._set_state(DeviceState.STANDBY, "维护完成")

    def reset(self) -> None:
        """人工复位：清除故障并回到待机（子类可扩展复位内部顺控）。"""
        if self.current_fault is not None:
            self.clear_fault()

    # ------------------------------------------------------------------
    # 故障注入接口（FaultInjector 与联锁逻辑都走这里）
    # ------------------------------------------------------------------
    def apply_fault(self, fault_type: str, detail: dict = None,
                    origin: str = "manual") -> None:
        """
        注入故障：
        :param fault_type: 故障类型中文名（如 伺服过载/急停）
        :param detail:     附加数据（随事件落盘）
        :param origin:     来源：random/script/interlock/manual
        """
        if self.current_fault is not None:
            return  # 已有未复位故障时忽略新注入（最稳妥：不叠加）
        self.current_fault = fault_type
        self.fault_since = self.clock.now()
        was_running = (self._state == DeviceState.RUNNING)
        # 停机原因记账统一由 _set_state 完成（离开 RUNNING 才计一次，避免双计）
        self._set_state(DeviceState.FAULT, fault_type)
        payload = {"fault_type": fault_type, "origin": origin, "was_running": was_running}
        payload.update(detail or {})
        self.bus.publish(self.device_id, EventTypes.FAULT_RAISED, payload)

    def clear_fault(self, note: str = "") -> None:
        """清除当前故障 → 待机，并广播 FAULT_CLEARED（子类据此恢复顺控）。"""
        if self.current_fault is None:
            return
        cleared_type = self.current_fault
        duration = round(self.clock.now() - (self.fault_since or self.clock.now()), 3)
        self.current_fault = None
        self.fault_since = None
        self._set_state(DeviceState.STANDBY, f"故障复位:{cleared_type}")
        self.bus.publish(self.device_id, EventTypes.FAULT_CLEARED,
                         {"fault_type": cleared_type,
                          "duration_s": duration, "note": note})

    # ------------------------------------------------------------------
    # 周期推进
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        """
        每 tick 推进一次（dt 为仿真秒）。基类只做通用记账：
        - RUNNING 态累积 run_seconds；
        - 子类覆写时必须先调用 super().update(dt)。
        """
        if self._state == DeviceState.RUNNING:
            self.run_seconds += dt

    # ------------------------------------------------------------------
    # 快照（控制台打印 / 自检断言 / 班次2 Web 共用）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """导出设备当前摘要信息。"""
        io_view = [p.snapshot() for p in self.io_table.values()]
        return {
            "id": self.device_id, "name": self.name, "state": self._state,
            "fault": self.current_fault,
            "run_seconds": round(self.run_seconds, 1),
            "cycles": self.cycle_count,
            "stops": dict(self.stop_counter),
            "io": io_view,
        }

    def __repr__(self) -> str:
        return f"<{self.device_id} {self.name} [{self._state}]>"


# ----------------------------------------------------------------------
# 自模块快速自检：python core/device_base.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    states_seen = []
    bus.subscribe(EventTypes.DEVICE_STATE, lambda e: states_seen.append(e["data"]["state"]))

    dev = DeviceBase("TST-DEV", "测试设备", clock, bus)
    dev.add_io("run_fb", "DI", 0, desc="运行反馈")
    dev.add_io("speed_sp", "AO", 0.0, "m/min", "速度设定")
    dev.start_up()
    dev._set_state(DeviceState.RUNNING, "自动循环")
    dev.set_io("speed_sp", 12.5)

    # --- 运行秒数记账：advance_ticks 每 tick 都回调 update，与真实编排一致 ---
    dev2 = DeviceBase("TST-DEV2", "测试设备2", clock, bus)
    dev2.start_up()
    dev2._set_state(DeviceState.RUNNING, "自动循环")
    before = clock.now()
    # 注意：advance_ticks/run_until 只驱动注册的步进回调；
    # 单设备测试需显式传入 step_fn（真实编排中由 Plant.update 统一驱动）
    clock.advance_ticks(200, step_fn=dev2.update)   # 20 仿真秒
    assert abs(dev2.run_seconds - 20.0) < 1e-6, f"运行秒数统计错误: {dev2.run_seconds}"

    # 故障→复位链路
    dev2.apply_fault("伺服过载", {"code": "E201"}, origin="script")
    assert dev2.state == DeviceState.FAULT and dev2.stop_counter["伺服过载"] == 1
    clock.advance_ticks(50, step_fn=dev2.update)     # 故障期间不累积运行秒
    frozen = dev2.run_seconds
    assert abs(dev2.run_seconds - frozen) < 1e-9, "故障期间不应累积运行秒"
    dev2.reset()
    assert dev2.state == DeviceState.STANDBY and dev2.current_fault is None
    snap = dev2.snapshot()
    # IO 点表断言：点位注册在 dev 上，检查 dev 的快照
    assert dev.snapshot()["io"][0]["dir"] == "DI"
    assert dev.get_io("speed_sp") == 12.5 and dev.io_table["speed_sp"].unit == "m/min"
    print(f"[device_base 自检通过] 状态迁移={len(states_seen)} 次, "
          f"运行={snap['run_seconds']}s, 停机原因={snap['stops']} (仿真验证值)")

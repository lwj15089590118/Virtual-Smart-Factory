# -*- coding: utf-8 -*-
"""
core/fault_injector.py —— 故障注入器（随机故障 + 脚本故障 + 人工触发）
=======================================================================
三种注入方式，全部经由 DeviceBase.apply_fault() 产生 fault.raised 事件：
    1. 随机故障：按设备配置"次/仿真小时"的泊松率，逐 tick 以 p=rate*dt/3600 掷骰；
       到达恢复时长后自动 clear_fault；
    2. 脚本故障：按预设表 {at 仿真时刻, target 设备, type 类型, duration} 精确触发，
       duration=None 表示需人工复位（如急停）——用于复现特定工况做作品集演示；
    3. 人工/程序触发：trigger() 接口，供 main.py 联锁急停、阶段2 Web 按钮调用。

假设记录：
    - 泊松过程用逐 tick 伯努利近似（dt=0.1s 时误差可忽略），避免引入事件队列复杂度。
"""

import random
from typing import Dict, List, Optional

import os
import sys
# 路径引导：直接运行本文件(python core/fault_injector.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.event_bus import EventBus
from config import settings as S


class _ActiveFault:
    """一条正在生效的故障记录（用于到期自动恢复）。"""

    __slots__ = ("device_id", "fault_type", "until", "need_manual")

    def __init__(self, device_id: str, fault_type: str, until: Optional[float],
                 need_manual: bool):
        self.device_id = device_id
        self.fault_type = fault_type
        self.until = until              # 自动恢复的仿真时刻；None=需人工复位
        self.need_manual = need_manual


class FaultInjector:
    """全厂故障调度器：每 tick 由编排器调用 update(dt)。"""

    def __init__(self, clock, bus: EventBus, devices: Dict[str, object],
                 rng: Optional[random.Random] = None,
                 random_rates: Optional[Dict[str, float]] = None,
                 random_types: Optional[Dict[str, list]] = None,
                 scripted: Optional[List[dict]] = None,
                 enabled: bool = True,
                 random_enabled: bool = True):
        """
        :param devices: 设备号 -> DeviceBase 实例的映射（注入目标注册表）
        :param rng:     独立随机源（与视觉判定等隔离，保证可复现）
        :param enabled: 总开关：False 时 update() 不做任何自动注入
                        （人工 trigger() 不受影响，急停联锁必须始终可用）
        :param random_enabled: 随机故障子开关（--no-random-faults 用，
                        脚本故障不受它影响，保证演示工况可复现）
        """
        self.clock = clock
        self.bus = bus
        self.devices = devices
        self.rng = rng or random.Random(S.DEFAULT_SEED + 77)
        self.enabled = enabled
        self.random_enabled = random_enabled
        # 配置拷贝，防止运行期被外部误改
        self._rates = dict(random_rates if random_rates is not None else S.RANDOM_FAULT_RATES)
        self._types = dict(random_types if random_types is not None else S.RANDOM_FAULT_TYPES)
        # 脚本队列：按 at 升序排序，逐个消费
        self._script_q: List[dict] = sorted(
            (dict(x) for x in (scripted if scripted is not None else S.SCRIPTED_FAULTS)),
            key=lambda x: x["at"])
        self.active: List[_ActiveFault] = []      # 生效中的故障
        self.stats_injected = 0                   # 注入总数（自检/报表用）

    # ------------------------------------------------------------------
    # 周期推进
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        """每 tick：先触发到期脚本故障，再掷随机故障，最后处理自动恢复。"""
        if not self.enabled:
            return
        now = self.clock.now()
        self._fire_due_scripts(now)
        self._roll_random_faults(dt)
        self._auto_recover(now)

    def _fire_due_scripts(self, now: float) -> None:
        """脚本故障：仿真时刻到达即触发（支持同一时刻多条）。"""
        while self._script_q and self._script_q[0]["at"] <= now:
            spec = self._script_q.pop(0)
            self.trigger(spec.get("target"), spec.get("type", "未命名故障"),
                         duration=spec.get("duration"), origin="script")

    def _roll_random_faults(self, dt: float) -> None:
        """随机故障：对每台配置了概率的设备独立掷骰（受 random_enabled 子开关控制）。"""
        if not (self.enabled and self.random_enabled):
            return
        for dev_id, rate_per_hour in self._rates.items():
            if rate_per_hour <= 0 or dev_id not in self.devices:
                continue
            dev = self.devices[dev_id]
            if dev.current_fault is not None:
                continue                       # 已在故障中的设备不重复注入
            p_tick = rate_per_hour * dt / 3600.0   # 泊松率的步长近似
            if self.rng.random() < p_tick:
                ftype = self._pick_random_type(dev_id)
                duration = self.rng.uniform(*S.FAULT_DURATION_RANGE)
                self.trigger(dev_id, ftype, duration=duration, origin="random")

    def _pick_random_type(self, dev_id: str) -> str:
        """按权重抽取该设备的故障类型池。"""
        pool = self._types.get(dev_id) or [("通用异常", 1)]
        total = sum(w for _, w in pool)
        r = self.rng.random() * total
        acc = 0.0
        for ftype, w in pool:
            acc += w
            if r <= acc:
                return ftype
        return pool[-1][0]

    def _auto_recover(self, now: float) -> None:
        """到期自动恢复（需人工复位的急停类不在此列）。"""
        still: List[_ActiveFault] = []
        for f in self.active:
            if f.until is not None and now >= f.until:
                dev = self.devices.get(f.device_id)
                if dev is not None and dev.current_fault == f.fault_type:
                    dev.clear_fault(note="自动恢复")
            else:
                still.append(f)
        self.active = [f for f in still
                       if not (f.device_id in self.devices
                               and self.devices[f.device_id].current_fault is None)]

    # ------------------------------------------------------------------
    # 对外触发接口
    # ------------------------------------------------------------------
    def trigger(self, device_id: str, fault_type: str,
                duration: Optional[float] = None, origin: str = "manual") -> bool:
        """
        立即向指定设备注入一次故障。
        :param duration: 自动恢复时长(仿真秒)；None → 需人工 reset 复位
        :return: 是否注入成功（设备不存在/已在故障中返回 False）
        """
        dev = self.devices.get(device_id)
        if dev is None or dev.current_fault is not None:
            return False
        dev.apply_fault(fault_type,
                        {"until": None if duration is None
                         else round(self.clock.now() + duration, 3)},
                        origin=origin)
        self.active.append(_ActiveFault(
            device_id, fault_type,
            None if duration is None else round(self.clock.now() + duration, 3),
            duration is None))
        self.stats_injected += 1
        return True

    def snapshot(self) -> dict:
        """当前注入器状态摘要。"""
        return {
            "injected_total": self.stats_injected,
            "active": [{"dev": f.device_id, "type": f.fault_type,
                        "need_manual": f.need_manual} for f in self.active],
        }


# ----------------------------------------------------------------------
# 自模块快速自检：python core/fault_injector.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock
    from core.event_bus import EventTypes
    from core.device_base import DeviceBase, DeviceState

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    d1 = DeviceBase("INJ-T1", "注入测试设备", clock, bus)
    raised, cleared = [], []
    bus.subscribe(EventTypes.FAULT_RAISED, lambda e: raised.append(e))
    bus.subscribe(EventTypes.FAULT_CLEARED, lambda e: cleared.append(e))

    inj = FaultInjector(
        clock, bus, {"INJ-T1": d1},
        rng=random.Random(7), random_rates={},        # 空随机表=纯脚本验证
        scripted=[{"at": 5.0, "target": "INJ-T1", "type": "气压不足", "duration": 10.0}])

    # 时刻 5.0s 触发（回调先于时间累加，第51拍看到 now=5.0），10s 后自动恢复
    clock.advance_ticks(200, step_fn=inj.update)      # 0 → 20s（覆盖 until=15s）
    assert len(raised) == 1 and raised[0]["ts_sim"] >= 4.9 and \
        raised[0]["data"]["origin"] == "script", "脚本故障未按时触发"
    assert len(cleared) == 1 and cleared[0]["data"]["note"] == "自动恢复"
    assert d1.state == DeviceState.STANDBY

    # 急停：duration=None → 必须人工复位
    assert inj.trigger("INJ-T1", "急停", duration=None, origin="interlock")
    clock.advance_ticks(300, step_fn=inj.update)      # 再跑 30s 不应自动恢复
    assert d1.current_fault == "急停" and len(cleared) == 1
    d1.reset()
    assert d1.current_fault is None

    # 随机故障概率链路：把速率调到 3600 次/h → 每 tick 命中概率 0.1，200 tick 内必中
    inj2 = FaultInjector(clock, bus, {"INJ-T1": d1}, rng=random.Random(1),
                         random_rates={"INJ-T1": 36000.0}, enabled=True, scripted=[])
    hit_before = inj2.stats_injected
    for _ in range(200):
        d1.reset()
        inj2.update(0.1)
        if inj2.stats_injected > hit_before:
            break
    assert inj2.stats_injected > hit_before, "高故障率下随机注入未命中"
    print(f"[fault_injector 自检通过] 脚本触发={len(raised)}, 自动恢复={len(cleared)}, "
          f"累计注入={inj2.stats_injected} 次 (仿真验证值)")

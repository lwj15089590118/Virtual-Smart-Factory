# -*- coding: utf-8 -*-
"""
ems/energy_model.py —— 设备能耗模型（班次3新增）
==================================================
建模口径（交付范围3：按 运行时长 × 功率曲线 估算 kWh，仿真验证值）：
    能量 = ∫ P(状态) dt —— 把每个设备的状态轨迹视为分段恒功率曲线：
        - 订阅 device.state 事件，得到"设备×状态段"的起止仿真时刻；
        - 每段能量 kWh = 功率kW(该状态) × 段时长s ÷ 3600；
        - 未闭合的当前段在 snapshot() 时按 clock.now() 虚拟闭合（不落账）。
    功率曲线来自 settings.EMS_POWER_KW（按设备号前缀匹配，运行/待机/故障分档）。

时间纪律：
    只用事件 ts_sim 与 clock.now() 做差分积分，绝不接触墙钟。

假设记录：
    - 状态内功率恒定、忽略启停瞬态冲击（作品集演示精度足够，
      真实系统可换 P(t)=P0+k·负载率 的回归模型，接口不变）。
"""

import os
import sys
# 路径引导：直接运行本文件(python ems/energy_model.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, Optional, Tuple

from core.event_bus import EventTypes
from config import settings as S


class EnergyModel:
    """全厂能耗台账：订阅 device.state 事件做分段积分。"""

    def __init__(self, plant):
        self.plant = plant
        self.bus = plant.bus
        self.clock = plant.clock
        # dev_id -> (状态, 段起点ts)；首次见到设备前默认"停止"从 t=0 起算
        self._open_seg: Dict[str, Tuple[str, float]] = {}
        # dev_id -> 已闭合段累计 kWh（只增不减；开放段在快照中虚拟结算）
        self._closed_kwh: Dict[str, float] = {d: 0.0 for d in plant.devices}
        self._token = self.bus.subscribe(EventTypes.DEVICE_STATE,
                                         self.ingest, "EMS能耗")

    # ------------------------------------------------------------------
    def _power_kw(self, dev_id: str, state: str) -> float:
        """查功率曲线：按设备号前缀匹配（ASM-/VIS-/PAL-/WH-/AGV-）。"""
        for prefix, curve in S.EMS_POWER_KW.items():
            if dev_id.startswith(prefix):
                return float(curve.get(state, 0.0))
        return 0.0                    # 未登记的设备按 0 kW（假设：辅助设施不计量）

    def ingest(self, event: dict) -> None:
        """device.state 回调：闭合上一状态段能量，开启新段。"""
        dev = event.get("source")
        if dev not in self._closed_kwh:
            return                    # 不在设备注册表中的来源（如 MES-ENGINE）跳过
        ts = float(event.get("ts_sim", 0.0))
        new_state = (event.get("data") or {}).get("state", "停止")
        seg = self._open_seg.pop(dev, None)
        if seg is not None:
            old_state, t0 = seg
            kw = self._power_kw(dev, old_state)
            self._closed_kwh[dev] = round(
                self._closed_kwh[dev] + kw * max(ts - t0, 0.0) / 3600.0, 9)
        self._open_seg[dev] = (new_state, ts)

    def _flush_open(self) -> Dict[str, float]:
        """把未闭合段按当前时刻虚拟结算（不改已落账数字）。"""
        now = round(self.clock.now(), 3)
        out = dict(self._closed_kwh)
        for dev, (state, t0) in self._open_seg.items():
            kw = self._power_kw(dev, state)
            out[dev] = round(out.get(dev, 0.0) + kw * max(now - t0, 0.0) / 3600.0, 6)
        return out

    # ------------------------------------------------------------------
    def current_kw(self, dev_id: str) -> float:
        """设备当前时刻功率（开放段功率，大屏实时功率条用）。"""
        seg = self._open_seg.get(dev_id)
        return self._power_kw(dev_id, seg[0]) if seg else 0.0

    def snapshot(self) -> dict:
        """全厂能耗快照（Web /api/ems/energy 数据源；全部为仿真验证值）。"""
        kwh_map = self._flush_open()
        devices = []
        total_kwh = 0.0
        for dev_id, dev in self.plant.devices.items():
            kwh = round(kwh_map.get(dev_id, 0.0), 4)
            total_kwh += kwh
            devices.append({
                "id": dev_id, "name": dev.name,
                "state": dev.state,
                "kw_now": round(self.current_kw(dev_id), 2),
                "kwh": kwh,
                "run_hours": round(dev.run_seconds / 3600.0, 4),  # 交叉核对口径
            })
        return {
            "note": "能耗为功率曲线估算的仿真验证值",
            "devices": devices,
            "total_kwh": round(total_kwh, 4),
            "cost_yuan": round(total_kwh * S.EMS_PRICE_YUAN_PER_KWH, 2),
            "co2_kg": round(total_kwh * S.EMS_CO2_KG_PER_KWH, 4),
            "price_yuan_per_kwh": S.EMS_PRICE_YUAN_PER_KWH,
        }

    def close(self) -> None:
        """退订总线。"""
        if self._token is not None:
            self.bus.unsubscribe(self._token)
            self._token = None


# ----------------------------------------------------------------------
# 自模块快速自检：python ems/energy_model.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock
    from core.event_bus import EventBus
    from core.device_base import DeviceBase, DeviceState

    class _FakePlant:
        """最小 Plant 替身：bus + clock + 一台测试设备。"""
        def __init__(self):
            self.clock = SimClock(dt=0.1)
            self.bus = EventBus(self.clock, persist=False)
            self.devices = {"TST-E": DeviceBase("TST-E", "测试设备",
                                                self.clock, self.bus)}

    plant = _FakePlant()
    em = EnergyModel(plant)
    dev = plant.devices["TST-E"]

    # 上电→待机→运行 100s→待机：待机功率1.5kW×~0s + 运行12kW×100s
    dev.start_up()
    dev._set_state(DeviceState.RUNNING, "自动")
    plant.clock.advance_ticks(int(100.0 / 0.1), step_fn=lambda d: None)  # 只推时间
    snap_mid = em.snapshot()
    running_kwh = snap_mid["devices"][0]["kwh"]
    expect = S.EMS_POWER_KW["TST-"]["运行"] * 100.0 / 3600.0 \
        if "TST-" in S.EMS_POWER_KW else None
    # 测试设备不在功率表 → 用 AGV 曲线验证公式：手动登记一条临时曲线
    assert running_kwh == 0.0, "未登记设备应按 0kW"
    S.EMS_POWER_KW["TST-"] = {"运行": 12.0, "待机": 1.5, "停止": 0.0}
    try:
        dev2 = DeviceBase("TST-E2", "测试设备2", plant.clock, plant.bus)
        plant.devices["TST-E2"] = dev2
        em._closed_kwh["TST-E2"] = 0.0
        dev2.start_up()
        dev2._set_state(DeviceState.RUNNING, "自动")
        plant.clock.advance_ticks(int(90.0 / 0.1), step_fn=lambda d: None)
        s2 = em.snapshot()
        got = [d for d in s2["devices"] if d["id"] == "TST-E2"][0]["kwh"]
        want = 12.0 * 90.0 / 3600.0                       # 12kW × 90s ÷ 3600
        assert abs(got - want) < 0.01, f"kWh 积分错误: {got} vs {want}"
        # 待机段也计费
        dev2.reset()                                       # 运行→待机
        plant.clock.advance_ticks(int(60.0 / 0.1), step_fn=lambda d: None)
        s3 = em.snapshot()
        got2 = [d for d in s3["devices"] if d["id"] == "TST-E2"][0]["kwh"]
        want2 = want + 1.5 * 60.0 / 3600.0
        assert abs(got2 - want2) < 0.01, f"待机段未计费: {got2} vs {want2}"
        assert s3["total_kwh"] >= got2
        print(f"[energy_model 自检通过] 运行段={want:.4f}kWh 精确积分, "
              f"待机段计费正常, 全厂合计={s3['total_kwh']:.4f}kWh (仿真验证值)")
    finally:
        del S.EMS_POWER_KW["TST-"]

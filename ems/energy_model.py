# -*- coding: utf-8 -*-
"""
ems/energy_model.py —— 设备能耗模型（阶段3新增）
==================================================
建模口径（交付范围3：按 运行时长 × 功率曲线 估算 kWh，仿真验证值）：
    能量 = ∫ P(状态) dt —— 把每个设备的状态轨迹视为分段恒功率曲线：
        - 订阅 device.state 事件，得到"设备×状态段"的起止仿真时刻；
        - 每段能量 kWh = 功率kW(该状态) × 段时长s ÷ 3600；
        - 未闭合的当前段在 snapshot() 时按 clock.now() 虚拟闭合（不落账）。
    功率曲线来自 settings.EMS_POWER_KW（按设备号前缀匹配，运行/待机/故障分档）。
    电费口径（增强）：支持尖峰平谷分时电价（settings.EMS_TOU_PERIODS）——
        仿真时刻映射为一日内小时（t=0 为 00:00，每 86400s 一天循环），
        状态段跨档位边界时自动切分子段分别计价，分档电量/电费随快照输出。

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

from typing import Dict, Tuple

from core.event_bus import EventTypes
from config import settings as S


# ======================================================================
# 分时电价（TOU）纯函数：积分落账 / 自检 / 报表共用（无副作用，可独立单测）
# ======================================================================
def tou_price_at(ts: float) -> tuple:
    """
    查询仿真时刻所在电价档位，返回 (档名, 单价 元/kWh)。
    TOU 关闭或时段配置存在缺口时，按基准单一电价（"平"档锚点）兜底。
    """
    if not S.EMS_TOU_ENABLED:
        return "单一", float(S.EMS_PRICE_YUAN_PER_KWH)
    h = (float(ts) % 86400.0) / 3600.0            # 一日内小时 [0,24)
    for name, price, intervals in S.EMS_TOU_PERIODS:
        for start, end in intervals:
            # 区间 [start, end)；start>end 表示跨零点环绕（如 23→7）
            in_iv = (h >= start or h < end) if start >= end else (start <= h < end)
            if in_iv:
                return name, float(price)
    return "平", float(S.EMS_PRICE_YUAN_PER_KWH)


def tou_segments(ts0: float, ts1: float) -> list:
    """
    把仿真时间窗 [ts0, ts1) 切成"同档位"子段，返回 [(a, b, 档名, 单价)]。
    候选边界 = 窗口两端 ∪ 窗口覆盖到的每一天中、各时段起止小时的绝对时刻——
    不假设边界必须落在整点，配置改为任意分钟粒度同样正确。
    """
    t0, t1 = float(ts0), float(ts1)
    if t1 <= t0:
        return []
    edges = {t0, t1}
    day0, day1 = int(t0 // 86400.0), int(t1 // 86400.0)
    hours = sorted({float(x) for _n, _p, ivs in S.EMS_TOU_PERIODS
                    for iv in ivs for x in iv})
    for day in range(day0, day1 + 1):
        base = day * 86400.0
        for hh in hours:
            bt = base + hh * 3600.0
            if t0 < bt < t1:
                edges.add(bt)
    pts = sorted(edges)
    segs = []
    for a, b in zip(pts, pts[1:]):
        name, price = tou_price_at((a + b) / 2.0)  # 子段内无边界，中点定档
        segs.append((a, b, name, price))
    return segs


def tou_cost(kw: float, ts0: float, ts1: float) -> tuple:
    """
    恒功率 kw 在仿真时间窗 [ts0, ts1] 的分时电费核算（纯函数）：
    返回 (总电费元, {档名: 电量kWh}, {档名: 电费元})。
    """
    yuan_total = 0.0
    kwh_by: Dict[str, float] = {}
    yuan_by: Dict[str, float] = {}
    for a, b, name, price in tou_segments(ts0, ts1):
        dkwh = float(kw) * (b - a) / 3600.0
        dyuan = dkwh * price
        yuan_total += dyuan
        kwh_by[name] = kwh_by.get(name, 0.0) + dkwh
        yuan_by[name] = yuan_by.get(name, 0.0) + dyuan
    return yuan_total, kwh_by, yuan_by


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
        # 分时电价账本（仅闭合段落账；开放段在快照中按当前时刻虚拟结算，不污染实账）
        self._closed_yuan = 0.0
        self._closed_tier_kwh: Dict[str, float] = {}
        self._closed_tier_yuan: Dict[str, float] = {}
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
        """device.state 回调：闭合上一状态段能量与分时电费，开启新段。"""
        dev = event.get("source")
        if dev not in self._closed_kwh:
            return                    # 不在设备注册表中的来源（如 MES-ENGINE）跳过
        ts = float(event.get("ts_sim", 0.0))
        new_state = (event.get("data") or {}).get("state", "停止")
        seg = self._open_seg.pop(dev, None)
        if seg is not None:
            old_state, t0 = seg
            self._settle_closed(dev, self._power_kw(dev, old_state), t0, ts)
        self._open_seg[dev] = (new_state, ts)

    def _settle_closed(self, dev_id: str, kw: float, t0: float, t1: float) -> None:
        """把一段已结束的恒功率过程落账：累计 kWh + 分时电费/分档电量电费。"""
        if kw <= 0.0 or t1 <= t0:
            return
        self._closed_kwh[dev_id] = round(
            self._closed_kwh.get(dev_id, 0.0) + kw * (t1 - t0) / 3600.0, 9)
        yuan, kwh_by, yuan_by = tou_cost(kw, t0, t1)
        self._closed_yuan = round(self._closed_yuan + yuan, 9)
        for name, v in kwh_by.items():
            self._closed_tier_kwh[name] = round(
                self._closed_tier_kwh.get(name, 0.0) + v, 9)
        for name, v in yuan_by.items():
            self._closed_tier_yuan[name] = round(
                self._closed_tier_yuan.get(name, 0.0) + v, 9)

    def _settle_open(self) -> tuple:
        """开放段一次性虚拟结算（只算不落账）。

        合并记录（规范轴坏味道#1）：原 _flush_open/_flush_open_cost 同构两趟遍历
        （各自取 now、扫 _open_seg、聚合字典），snapshot() 连调两趟——
        现单趟同时产出 每设备kWh映射 / 电费元 / {档:kWh} / {档:元}。
        返回 (kwh_map, 电费元, {档:kWh}, {档:元})。
        """
        now = round(self.clock.now(), 3)
        kwh_map = dict(self._closed_kwh)
        yuan_total = 0.0
        kwh_by: Dict[str, float] = {}
        yuan_by: Dict[str, float] = {}
        for dev, (state, t0) in self._open_seg.items():
            kw = self._power_kw(dev, state)
            dt = max(now - t0, 0.0)
            kwh_map[dev] = round(kwh_map.get(dev, 0.0) + kw * dt / 3600.0, 6)
            y, kb, yb = tou_cost(kw, t0, now)
            yuan_total += y
            for n, v in kb.items():
                kwh_by[n] = kwh_by.get(n, 0.0) + v
            for n, v in yb.items():
                yuan_by[n] = yuan_by.get(n, 0.0) + v
        return kwh_map, yuan_total, kwh_by, yuan_by

    # ------------------------------------------------------------------
    def current_kw(self, dev_id: str) -> float:
        """设备当前时刻功率（开放段功率，大屏实时功率条用）。"""
        seg = self._open_seg.get(dev_id)
        return self._power_kw(dev_id, seg[0]) if seg else 0.0

    def snapshot(self) -> dict:
        """全厂能耗+分时电费快照（Web /api/ems/energy 数据源；全部为仿真验证值）。"""
        kwh_map, open_yuan, open_kwh_by, open_yuan_by = self._settle_open()
        total_kwh = sum(kwh_map.values())
        cost_yuan = self._closed_yuan + open_yuan
        # 分档台账 = 闭合段实账 + 开放段虚拟结算；档位顺序按配置表，额外档附后
        merged_kwh: Dict[str, float] = {}
        merged_yuan: Dict[str, float] = {}
        for src_k, src_y in ((self._closed_tier_kwh, self._closed_tier_yuan),
                             (open_kwh_by, open_yuan_by)):
            for n, v in src_k.items():
                merged_kwh[n] = merged_kwh.get(n, 0.0) + v
            for n, v in src_y.items():
                merged_yuan[n] = merged_yuan.get(n, 0.0) + v
        tier_order = [name for name, _p, _iv in S.EMS_TOU_PERIODS]
        extras = sorted(n for n in merged_kwh if n not in tier_order)
        tiers = [{"tier": n,
                  "kwh": round(merged_kwh.get(n, 0.0), 4),
                  "yuan": round(merged_yuan.get(n, 0.0), 4)}
                 for n in (tier_order + extras)]
        now_name, now_price = tou_price_at(self.clock.now())
        devices = []
        for dev_id, dev in self.plant.devices.items():
            kwh = round(kwh_map.get(dev_id, 0.0), 4)
            devices.append({
                "id": dev_id, "name": dev.name,
                "state": dev.state,
                "kw_now": round(self.current_kw(dev_id), 2),
                "kwh": kwh,
                "run_hours": round(dev.run_seconds / 3600.0, 4),  # 交叉核对口径
            })
        return {
            "note": "能耗为功率曲线估算、电费为尖峰平谷分时计费的仿真验证值",
            "devices": devices,
            "total_kwh": round(total_kwh, 4),
            "tou_enabled": bool(S.EMS_TOU_ENABLED),
            "period_now": {"tier": now_name, "price": now_price},
            "cost_yuan": round(cost_yuan, 4),
            "avg_price": (round(cost_yuan / total_kwh, 4)
                          if total_kwh > 1e-9 else 0.0),
            "tiers": tiers,
            "price_yuan_per_kwh": S.EMS_PRICE_YUAN_PER_KWH,
            "co2_kg": round(total_kwh * S.EMS_CO2_KG_PER_KWH, 4),
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
        # 修复记录：原此处调 dev2.reset() 期待"运行→待机"，但基类 reset() 无故障时为
        # 空操作，设备实际保持 RUNNING 按 12kW 继续计费（0.5≠0.325）——改为显式迁移
        dev2._set_state(DeviceState.STANDBY, "自检：运行→待机")
        plant.clock.advance_ticks(int(60.0 / 0.1), step_fn=lambda d: None)
        s3 = em.snapshot()
        got2 = [d for d in s3["devices"] if d["id"] == "TST-E2"][0]["kwh"]
        want2 = want + 1.5 * 60.0 / 3600.0
        assert abs(got2 - want2) < 0.01, f"待机段未计费: {got2} vs {want2}"
        assert s3["total_kwh"] >= got2

        # --- 分时电价集成验证：本用例时间窗 t<300s=00:xx，全程落在"谷"档 ---
        assert s3["tou_enabled"] is True and s3["period_now"]["tier"] == "谷"
        assert abs(s3["cost_yuan"] - s3["total_kwh"] * 0.35) < 0.001, \
            f"谷档电费不符: {s3['cost_yuan']} vs {s3['total_kwh'] * 0.35}"
        tier_sum = sum(t["yuan"] for t in s3["tiers"])
        assert abs(s3["cost_yuan"] - tier_sum) < 0.001, \
            f"分档电费合计应等于总电费: {s3['tiers']}"
        assert s3["tiers"][0]["tier"] == "谷" and s3["tiers"][0]["kwh"] > 0
        print(f"[energy_model 自检通过] 运行段={want:.4f}kWh 精确积分, "
              f"待机段计费正常, 全厂合计={s3['total_kwh']:.4f}kWh, "
              f"谷档电费={s3['cost_yuan']}元 (仿真验证值)")

        # --- TOU 纯函数验证：跨档切分 / 跨零点环绕 / 关闭退回单一价 ---
        y, kb, yb = tou_cost(1.0, 6 * 3600, 8 * 3600)      # 06:00→08:00 跨 谷|平 边界
        assert abs(kb["谷"] - 1.0) < 1e-9 and abs(kb["平"] - 1.0) < 1e-9, f"跨档切分错: {kb}"
        assert abs(y - (0.35 + 0.65)) < 1e-9 and yb.get("峰", 0.0) < 1e-12
        y2, kb2, _ = tou_cost(2.0, 22.5 * 3600, 23.5 * 3600)  # 22:30→23:30 平|谷 各0.5h@2kW
        assert abs(kb2["平"] - 1.0) < 1e-9 and abs(kb2["谷"] - 1.0) < 1e-9, f"环绕切分错: {kb2}"
        assert abs(y2 - (0.65 + 0.35)) < 1e-9, f"环绕电费错: {y2}"
        S.EMS_TOU_ENABLED = False
        try:
            y3, kb3, _ = tou_cost(1.0, 0.0, 3600.0)
            assert abs(y3 - S.EMS_PRICE_YUAN_PER_KWH) < 1e-9, f"单一电价退回失败: {y3}"
            assert abs(kb3.get("单一", 0.0) - 1.0) < 1e-9
        finally:
            S.EMS_TOU_ENABLED = True
        print("[energy_model TOU] 跨档切分/跨零点环绕/单一价退回 全部通过 (仿真验证值)")
    finally:
        del S.EMS_POWER_KW["TST-"]

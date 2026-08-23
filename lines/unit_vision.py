# -*- coding: utf-8 -*-
"""
lines/unit_vision.py —— 视觉质检单元
======================================
职责：
    1. 从装配单元流出队列逐件取品，占用检测站 VISION_INSPECT_TIME 仿真秒；
    2. 判定规则（班次3 将替换为真实深度学习算法推理，本班次用规则模拟）：
       关键尺寸 = 名义值 + N(0, σ) 过程散布，|偏差| ≤ 公差 → OK，否则 NG；
       理论 NG 率 ≈ 4.6%（仿真验证值：σ=0.04, 公差±0.08, 名义 10.00mm）；
    3. NG 品分流到返修道（rework_lane），OK 品进入流出队列供码垛；
    4. 全部判定写入质检记录 qc_records（班次3/SPC 分析的数据源）。
扩展点（班次3）：
    - 覆写 judge() 方法即可接入真实模型，其余流程零改动；
    - qc_records 结构已含算法所需的原始测量字段。

假设记录：
    - 检测站单工位串行作业（真实视觉节拍通常 1~3s/件，取 2.5s 居中）。
"""

import collections
from collections import deque
from typing import Deque, List, Optional

import numpy as np

import os
import sys
# 路径引导：直接运行本文件(python lines/unit_vision.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.device_base import DeviceBase, DeviceState
from core.event_bus import EventBus, EventTypes
from lines.product import Product
from config import settings as S


class UnitVision(DeviceBase):
    """视觉质检单元：单工位串行检测。"""

    def __init__(self, clock, bus: EventBus,
                 unit_id: str = S.VISION_ID,
                 rng: Optional[np.random.Generator] = None):
        name = S.VISION_NAME if unit_id == S.VISION_ID else f"视觉质检单元{unit_id}"
        super().__init__(unit_id, name, clock, bus)
        # 独立高斯源：与故障注入等随机过程隔离，保证作品集结果可复现
        self._rng = rng if rng is not None else np.random.default_rng(S.DEFAULT_SEED + 1)
        # ---- 物流 ----
        self.inbound: Deque[Product] = deque()       # 待检队列（装配流出接入）
        self.outbound: Deque[Product] = deque()      # OK 品流出队列（码垛接入）
        self.rework_lane: List[Product] = []         # 返修道（NG 品）
        self.qc_records: deque = collections.deque(maxlen=S.VISION_RECORD_LIMIT)
        # ---- 检测站内部状态 ----
        self._current: Optional[Product] = None      # 正在检测的产品
        self._timer = 0.0                            # 已检测耗时
        # 班次3修改后修复：NG 剔除挡板脉冲保持截止时刻（None=挡板已复位）。
        # 修复记录：原实现同一 tick 内置 1 又清 0，脉宽为 0，
        # Modbus(0.5s 刷新)/大屏永远采样不到该信号——现按 VISION_REJECT_PULSE_S 保持。
        self._reject_until: Optional[float] = None
        # ---- 统计 ----
        self.ok_total = 0
        self.ng_total = 0

    # ------------------------------------------------------------------
    # IO 点表
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        self.add_io("di_part_in_place", "DI", 0, desc="待检品到位")
        self.add_io("do_reject_gate", "DO", 0, desc="NG 剔除挡板")
        self.add_io("ai_light_intensity", "AI", 100.0, "%", "光源亮度")
        self.add_io("ai_measure_dim", "AI", 0.0, "mm", "关键尺寸测量值")

    # ------------------------------------------------------------------
    # 判定核心（班次3 接真实算法时只需覆写本方法）
    # ------------------------------------------------------------------
    def judge(self, product: Product) -> tuple:
        """
        规则模拟判定：返回 (结果"OK"/"NG", 测量尺寸mm)。
        规则：尺寸 ~ N(名义值, σ)；|尺寸-名义| ≤ 公差 → OK。
        """
        dim = float(self._rng.normal(S.VISION_NOMINAL_DIM, S.VISION_SIGMA))
        ok = abs(dim - S.VISION_NOMINAL_DIM) <= S.VISION_TOLERANCE
        return ("OK" if ok else "NG"), round(dim, 4)

    # ------------------------------------------------------------------
    # 每 tick 推进
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        super().update(dt)

        # NG 剔除挡板脉冲到期复位（放在冻结态判断之前：
        # 即使检测中发生故障，挡板也会在脉宽到期后落回安全位）
        if self._reject_until is not None and self.clock.now() >= self._reject_until:
            self.set_io("do_reject_gate", 0)
            self._reject_until = None

        if self.state in (DeviceState.STOPPED, DeviceState.FAULT,
                          DeviceState.MAINTENANCE):
            return                              # 故障中：检测暂停，来料在队列排队

        # 空闲且有来料 → 取品开始检测
        if self._current is None and self.inbound:
            self._current = self.inbound.popleft()
            self._timer = 0.0
            self.set_io("di_part_in_place", 1)
            if self.state == DeviceState.STANDBY:
                self._set_state(DeviceState.RUNNING, "开始检测")

        if self._current is None:
            if self.state == DeviceState.RUNNING:
                self._set_state(DeviceState.STANDBY, "待检队列空")
            return

        # 检测进行中：模拟光源与测量值输出（供班次2 Modbus/UI 观测）
        # 计时累加 9 位舍入，防止长跑浮点漂移（与 SimClock 同款处理）
        self._timer = round(self._timer + dt, 9)
        self.set_io("ai_light_intensity", 100.0)
        if self._timer < S.VISION_INSPECT_TIME:
            return

        # ---- 检测完成：判定 + 分流 + 记录 ----
        result, dim = self.judge(self._current)
        # 班次3修改：取走算法包(vision/vision_upgrade.py)暂存的判定明细，
        # 并入质检记录与 vision.ok/ng 事件负载；规则法路径下该字段为 None，行为不变。
        algo_detail = getattr(self, "last_judge_detail", None)
        if hasattr(self, "last_judge_detail"):
            self.last_judge_detail = None
        product = self._current
        product.qc_result = result
        product.qc_dim = dim
        self.set_io("ai_measure_dim", dim)
        record = {
            "product_id": product.product_id,
            "ts_sim": round(self.clock.now(), 3),
            "result": result,
            "dim_mm": dim,
            "nominal_mm": S.VISION_NOMINAL_DIM,
            "tol_mm": S.VISION_TOLERANCE,
        }
        if algo_detail:
            record.update(algo_detail)      # 班次3修改：A/B对照/概率/特征向量随事件落盘
        self.qc_records.append(record)
        self.cycle_count += 1

        if result == "OK":
            self.ok_total += 1
            self.outbound.append(product)
            self.bus.publish(self.device_id, EventTypes.VISION_OK, record)
        else:
            self.ng_total += 1
            product.rework = True
            self.rework_lane.append(product)
            # 剔除挡板动作：置位并保持 VISION_REJECT_PULSE_S 脉宽（到期由 update 复位），
            # 保证低速/高速运行下该 DO 信号均可被 Modbus/UI 观测到
            self.set_io("do_reject_gate", 1)
            self._reject_until = round(self.clock.now() + S.VISION_REJECT_PULSE_S, 3)
            self.bus.publish(self.device_id, EventTypes.VISION_NG,
                              dict(record, lane="返修道"))

        self._current = None
        self.set_io("di_part_in_place", 0)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def ng_rate(self) -> float:
        """累计 NG 率（仿真验证值）。"""
        total = self.ok_total + self.ng_total
        return round(self.ng_total / total, 4) if total else 0.0

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "ok": self.ok_total, "ng": self.ng_total,
            "ng_rate": self.ng_rate(),
            "queue_len": len(self.inbound),
            "rework_len": len(self.rework_lane),
        })
        # 班次3修改：注入了升级算法时，导出算法档案（训练指标/在线混淆矩阵）
        if getattr(self, "algo_info", None):
            snap["algo"] = self.algo_info
        return snap


# ----------------------------------------------------------------------
# 自模块快速自检：python lines/unit_vision.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    vis = UnitVision(clock, bus, unit_id="VIS-T1",
                     rng=np.random.default_rng(42))
    vis.start_up()
    ok_events, ng_events, gate_at_ng = [], [], []
    bus.subscribe(EventTypes.VISION_OK, lambda e: ok_events.append(e))
    bus.subscribe(EventTypes.VISION_NG,
                  lambda e: gate_at_ng.append(vis.get_io("do_reject_gate"))
                  or ng_events.append(e))

    # 灌入 200 件待检品（直接构造产品对象，模拟装配流出）
    from lines.product import Product
    for i in range(200):
        vis.inbound.append(Product(f"PT{i:08d}", born_at=clock.now(),
                                   source_unit="ASM-T1"))
    clock.advance_ticks(int(200 * 3.0 / clock.dt), step_fn=vis.update)  # 给足 3s/件

    judged = vis.ok_total + vis.ng_total
    assert judged == 200, f"应全部判完 200 件，实际 {judged}"
    assert len(ok_events) == vis.ok_total and len(ng_events) == vis.ng_total
    assert len(vis.outbound) == vis.ok_total, "OK 品应全部进入流出队列"
    assert len(vis.rework_lane) == vis.ng_total, "NG 品应全部进入返修道"
    assert all(p.qc_result == "OK" for p in vis.outbound)
    assert all(p.rework for p in vis.rework_lane)
    assert 0.01 <= vis.ng_rate() <= 0.12, f"NG 率异常偏离理论值: {vis.ng_rate()}"
    assert len(vis.qc_records) == 200
    # 剔除挡板脉宽验证（修复回归点）：NG 判定时刻挡板应为 1，
    # 且脉宽(1s)过后自动复位为 0——不再出现"同拍置位又清零采不到"的问题
    assert ng_events, "种子42下200件应产生 NG 样本"
    assert all(g == 1 for g in gate_at_ng), \
        f"NG 事件时刻剔除挡板未保持: {gate_at_ng}"
    clock.advance_ticks(int((S.VISION_REJECT_PULSE_S + 0.5) / clock.dt),
                        step_fn=vis.update)
    assert vis.get_io("do_reject_gate") == 0 and vis._reject_until is None, \
        "脉宽到期后剔除挡板应自动复位"
    # 相同种子复现性验证（作品集可复现性）
    vis2 = UnitVision(clock, bus, unit_id="VIS-T2", rng=np.random.default_rng(42))
    vis2.start_up()                                   # 复现性对照机也需上电
    vis2.inbound.append(Product("PX00000001", born_at=0.0, source_unit="T"))
    clock.advance_ticks(int(3.0 / clock.dt), step_fn=vis2.update)
    assert vis2.qc_records[-1]["dim_mm"] == vis.qc_records[0]["dim_mm"], \
        "同种子判定结果应可复现"
    print(f"[unit_vision 自检通过] OK={vis.ok_total}, NG={vis.ng_total}, "
          f"NG率={vis.ng_rate()*100:.1f}% (理论≈4.6%, 仿真验证值)")

# -*- coding: utf-8 -*-
"""
lines/unit_assembly.py —— 装配单元（以 PLC 顺控/梯图思想实现的 8 步状态机）
============================================================================
工艺流程（S1→S8 循环）：
    等待上料 → 上料 → 输送入站 → 定位夹紧 → 压装 → 拧紧 → 输送流出 → 流出完成
联锁逻辑（工业标准做法）：
    联锁1  安全门打开 → 顺控"保持"(HOLD)：步计时器冻结、输出点全部置安
           （保持≠停机，门关后从断点继续——与真实 PLC 的 M0 停止位一致）；
    联锁2  急停 → 全线故障态：由编排器对本单元注入"急停"故障，
           必须人工 reset() 复位后从断点继续（步计时器保留剩余量）。
节拍可配：每步耗时来自 config.settings.ASSEMBLY_STEP_DURATIONS。

假设记录：
    - 急停复位后产品保留在工位、当前步骤从头计时（比"丢弃在制品"更稳妥，
      且与真实产线"急停后重新启动当前工步"的惯例一致）；
    - 原料经内嵌有限料仓供应（增强：落地班次1预留的扩展点——料空冻结于
      "等待上料"步，补料后断点续走；低水位滞回告警，支持手动/自动补料）。
"""

from collections import deque
from typing import Deque, List, Optional

import os
import sys
# 路径引导：直接运行本文件(python lines/unit_assembly.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.device_base import DeviceBase, DeviceState
from core.event_bus import EventBus, EventTypes
from lines.product import Product
from config import settings as S

# 顺控步序（固定顺序，与 settings 中耗时字典一一对应）
STEP_ORDER: List[str] = ["等待上料", "上料", "输送入站", "定位夹紧",
                         "压装", "拧紧", "输送流出", "流出完成"]


class UnitAssembly(DeviceBase):
    """装配单元：单工位旋转式顺控，一次加工一件。"""

    def __init__(self, clock, bus: EventBus,
                 unit_id: str = S.ASSEMBLY_ID,
                 step_durations: Optional[dict] = None):
        name = S.ASSEMBLY_NAME if unit_id == S.ASSEMBLY_ID else f"装配单元{unit_id}"
        super().__init__(unit_id, name, clock, bus)
        self.step_durations = dict(step_durations or S.ASSEMBLY_STEP_DURATIONS)
        # ---- 顺控内部状态 ----
        self._step_index = 0                # 当前步序号（0..7）
        self._step_timer = 0.0              # 当前步已耗时（仿真秒）
        self._hold = False                  # 安全门保持标志
        self._door_open = False             # 安全门状态（True=开）
        self._wip: Optional[Product] = None # 工位上的在制品
        # ---- 物流与统计 ----
        self.outbound: Deque[Product] = deque()  # 流出产品队列（视觉单元取）
        self.products_out_total = 0         # 累计流出数
        self._product_seq = 0               # 产品序号发生器
        self.takt_seconds = sum(self.step_durations.values())  # 实际生效节拍
        # ---- 有限料仓（增强）：装配单元内嵌上料机构 ----
        self.feeder_capacity = int(S.FEEDER_CAPACITY)
        self.feeder_stock = min(int(S.FEEDER_INITIAL), self.feeder_capacity)
        self._feeder_low_latched = False     # 低水位告警滞回（补到阈值上方才复位）
        self._starving = False               # 料空冻结节拍中（补料后自动续走）

    # ------------------------------------------------------------------
    # IO 点表（DI/DO/AI/AO —— 班次2 将按此表映射 Modbus 寄存器）
    # ------------------------------------------------------------------
    def _init_io(self) -> None:
        self.add_io("di_door_closed", "DI", 1, desc="安全门关闭信号(1=关)")
        self.add_io("di_estop_ok", "DI", 1, desc="急停回路正常(1=正常)")
        self.add_io("di_part_present", "DI", 0, desc="原料到位检测")
        self.add_io("do_conveyor_run", "DO", 0, desc="输送带运行")
        self.add_io("do_press_down", "DO", 0, desc="压机下行")
        self.add_io("do_tighten_on", "DO", 0, desc="电批拧紧")
        self.add_io("do_clamp_on", "DO", 0, desc="治具夹紧")
        self.add_io("ai_press_force", "AI", 0.0, "kN", "压装力实时值")
        self.add_io("ai_torque", "AI", 0.0, "N·m", "拧紧扭矩实时值")
        self.add_io("do_stack_g", "DO", 0, desc="三色灯-绿(自动运行)")
        self.add_io("ai_feeder_level", "AI",
                    float(min(S.FEEDER_INITIAL, S.FEEDER_CAPACITY)),
                    "件", "料仓余量(件)")

    # ------------------------------------------------------------------
    # 对外操作接口（班次2 Web 按钮 / 自检脚本调用）
    # ------------------------------------------------------------------
    def set_door(self, open_: bool) -> None:
        """设置安全门状态：True=开门（触发保持联锁）。"""
        self._door_open = bool(open_)
        self.set_io("di_door_closed", 0 if open_ else 1)

    def start_auto(self) -> None:
        """切入自动循环（停止→待机→运行 由顺控自然推进）。"""
        self.start_up()

    # ------------------------------------------------------------------
    # 有限料仓（增强）：消耗 / 低水位滞回告警 / 补料
    # ------------------------------------------------------------------
    def feeder_refill(self, qty: Optional[int] = None,
                      auto: bool = False) -> dict:
        """补料入口（Web 命令 feeder_refill 与自动策略共用）。

        默认按 FEEDER_REFILL_QTY 补料，封顶容量；补到低水位阈值上方
        自动解除滞回告警；若产线正因料空冻结则立即恢复"等待上料"步。
        """
        add = int(qty) if qty is not None else int(S.FEEDER_REFILL_QTY)
        if add <= 0:
            return {"ok": False, "msg": "补料数量必须为正数"}
        before = self.feeder_stock
        self.feeder_stock = min(self.feeder_capacity, before + add)
        added = self.feeder_stock - before
        self.set_io("ai_feeder_level", float(self.feeder_stock))
        if self.feeder_stock > S.FEEDER_LOW:
            self._feeder_low_latched = False          # 滞回复位
        if self.feeder_stock > 0:
            self._starving = False                    # 料到：等待上料步自动续走
        self.bus.publish(self.device_id, EventTypes.FEEDER_REFILL,
                         {"added": added, "stock": self.feeder_stock,
                          "auto": auto})
        return {"ok": True, "added": added, "stock": self.feeder_stock}

    # ------------------------------------------------------------------
    # 每 tick 推进（唯一的时间使用入口是 dt 与 clock.now()）
    # ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        # 假设记录：安全门保持期间不计入"运行秒数"（保持≠有效加工时间）
        if self.state in (DeviceState.STOPPED, DeviceState.FAULT,
                          DeviceState.MAINTENANCE):
            return                              # 故障/停机/维护中：顺控完全冻结

        # ---- 联锁1：安全门开 → 保持（先于基类记账，运行秒数同步冻结）----
        if self._door_open:
            if not self._hold and self.state == DeviceState.RUNNING:
                self._hold = True
                self.bus.publish(self.device_id, EventTypes.DOOR_HOLD,
                                 {"step": self.current_step_name()})
            self._safe_outputs()                # 门开：输出点全部置安全态
            return                              # 冻结步计时器与统计（保持）
        super().update(dt)                      # 基类记账（RUNNING 积累运行秒数）
        if self._hold:
            self._hold = False
            self.bus.publish(self.device_id, EventTypes.DOOR_RESUME,
                             {"step": self.current_step_name(),
                              "remain_s": round(self._step_remain(), 2)})

        # 待机 → 自动起跑（条件恒满足；料空由下方等待上料冻结段接管）
        if self.state == DeviceState.STANDBY:
            self._set_state(DeviceState.RUNNING, "自动循环开始")

        step_name = STEP_ORDER[self._step_index]
        dur = self.step_durations.get(step_name, 1.0)

        # ---- 有限料仓（增强）：料空 → 冻结在"等待上料"步直至补料 ----
        if step_name == "等待上料" and self.feeder_stock <= 0:
            if not self._starving:
                self._starving = True
                self._feeder_low_latched = True   # 料空必然已越过低水位
                self.bus.publish(self.device_id, EventTypes.FEEDER_EMPTY,
                                 {"stock": 0}, severity="WARNING")
            self.set_io("do_stack_g", 0)          # 三色灯灭（等料）
            return                                # 冻结：不计时、不推进、不产出

        self._drive_outputs(step_name, dur)     # 按步驱动输出点（供 UI/Modbus 观察）
        # 计时累加做 9 位舍入：与 SimClock 同款防漂移处理（长跑批节拍确定性）
        self._step_timer = round(self._step_timer + dt, 9)

        if self._step_timer < dur:
            return                              # 本步未完成

        # ---- 步完成转移（PLC 顺序功能图 SFC 的 TRANSITION）----
        self._step_timer -= dur                 # 保留相位余量，保证加速跑批节拍一致
        self._on_step_done(step_name)

    def _on_step_done(self, step_name: str) -> None:
        """步完成动作：按步序执行副作用并推进步指针。"""
        if step_name == "上料":
            # 有限料仓（增强）：消耗一件毛坯；低水位滞回告警（可选自动补料）
            self.feeder_stock = max(0, self.feeder_stock - 1)
            self.set_io("ai_feeder_level", float(self.feeder_stock))
            if self.feeder_stock <= S.FEEDER_LOW and not self._feeder_low_latched:
                self._feeder_low_latched = True
                self.bus.publish(self.device_id, EventTypes.FEEDER_LOW,
                                 {"stock": self.feeder_stock,
                                  "threshold": S.FEEDER_LOW})
                if S.FEEDER_AUTO_REFILL:
                    self.feeder_refill(auto=True)  # 自动策略：即触即补
            # 产品在此刻"出生"，分配全局唯一 ID
            self._product_seq += 1
            self._wip = Product(product_id=f"P{self._product_seq:08d}",
                                born_at=self.clock.now(), source_unit=self.device_id)
            self.set_io("di_part_present", 1)
        elif step_name == "定位夹紧":
            self.set_io("do_clamp_on", 1)
        elif step_name == "流出完成":
            self.set_io("di_part_present", 0)
            if self._wip is not None:
                self.cycle_count += 1
                self.products_out_total += 1
                self.outbound.append(self._wip)
                self.bus.publish(self.device_id, EventTypes.PRODUCT_OUT,
                                 {"product": self._wip.to_dict(),
                                  "takt_s": round(self.takt_seconds, 2)})
                self._wip = None
        # 推进步指针（循环）
        self._step_index = (self._step_index + 1) % len(STEP_ORDER)

    def _drive_outputs(self, step_name: str, dur: float) -> None:
        """按当前步刷新 DO/AI 点位（模拟真实执行机构反馈，供班次2 Modbus 映射）。"""
        g = 1 if not self._hold else 0
        self.set_io("do_stack_g", g)
        self.set_io("do_conveyor_run", 1 if step_name in ("输送入站", "输送流出") else 0)
        self.set_io("do_press_down", 1 if step_name == "压装" else 0)
        self.set_io("do_tighten_on", 1 if step_name == "拧紧" else 0)
        # 压装力斜坡 0→设计满量程 50kN（仿真验证值）；拧紧扭矩方波 8±0.5 N·m
        progress = min(self._step_timer / dur, 1.0)
        self.set_io("ai_press_force", round(50.0 * progress, 2) if step_name == "压装" else 0.0)
        self.set_io("ai_torque", round(7.5 + 1.0 * progress, 2) if step_name == "拧紧" else 0.0)

    def _safe_outputs(self) -> None:
        """安全门打开时的安全输出态（真实 PLC 联锁的标准写法）。"""
        for io_name in ("do_conveyor_run", "do_press_down", "do_tighten_on"):
            self.set_io(io_name, 0)
        self.set_io("ai_press_force", 0.0)
        self.set_io("ai_torque", 0.0)
        self.set_io("do_stack_g", 0)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def current_step_name(self) -> str:
        """当前顺控步名称。"""
        return STEP_ORDER[self._step_index]

    def _step_remain(self) -> float:
        """当前步剩余时间（仿真秒）。"""
        dur = self.step_durations.get(STEP_ORDER[self._step_index], 1.0)
        return max(dur - self._step_timer, 0.0)

    def step_progress_pct(self) -> float:
        """当前步进度百分比（控制台/进度条用）。"""
        dur = self.step_durations.get(STEP_ORDER[self._step_index], 1.0)
        return min(self._step_timer / dur * 100.0, 100.0)

    def take_output(self) -> Optional[Product]:
        """下游（视觉单元）取走一件流出产品；无货返回 None。"""
        if self.outbound:
            return self.outbound.popleft()
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "step": self.current_step_name(),
            "step_progress": round(self.step_progress_pct(), 1),
            "products_out": self.products_out_total,
            "takt_s": round(self.takt_seconds, 2),
            "door_hold": self._hold,
            # 有限料仓（增强）
            "feeder_stock": self.feeder_stock,
            "feeder_capacity": self.feeder_capacity,
            "feeder_state": ("空" if self.feeder_stock <= 0
                             else ("低" if self.feeder_stock <= S.FEEDER_LOW
                                   else "正常")),
        })
        return snap


# ----------------------------------------------------------------------
# 自模块快速自检：python lines/unit_assembly.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    asm = UnitAssembly(clock, bus, unit_id="ASM-T1",
                       step_durations={k: 1.0 for k in STEP_ORDER})  # 全部 1s → 节拍 8s
    asm.start_auto()
    outs = []
    bus.subscribe(EventTypes.PRODUCT_OUT, lambda e: outs.append(e["data"]["product"]))
    clock.advance_ticks(int((8 * 5 + 4) / clock.dt), step_fn=asm.update)   # 跑 44s ≈ 5 件

    assert len(outs) == 5, f"44s 应产出 5 件，实际 {len(outs)}"
    assert all(p["qc_result"] is None for p in outs), "装配阶段不应有质检结论"
    assert asm.cycle_count == 5 and abs(asm.run_seconds - 44.0) < 0.11, \
        f"循环数/运行秒错误: {asm.cycle_count}, {asm.run_seconds}"

    # 联锁1：安全门开 → 保持（产量冻结），关门 → 断点续走
    asm.set_door(True)
    mark_out, mark_run = asm.products_out_total, asm.run_seconds
    clock.advance_ticks(50, step_fn=asm.update)          # 门开 5s
    assert asm._hold and asm.products_out_total == mark_out \
        and abs(asm.run_seconds - mark_run) < 0.11, "门开期间未冻结"
    asm.set_door(False)
    clock.advance_ticks(int(80 / clock.dt), step_fn=asm.update)  # 关门再跑 80s ≥ 10 个节拍
    assert asm.products_out_total >= mark_out + 9, "门关后未恢复产出"

    # 联锁2：急停 → 故障态，人工复位后续走
    asm.apply_fault("急停", origin="interlock")
    frozen_cycles = asm.cycle_count
    clock.advance_ticks(30, step_fn=asm.update)          # 急停 3s 无推进
    assert asm.state == DeviceState.FAULT and asm.cycle_count == frozen_cycles
    asm.reset()
    clock.advance_ticks(int(16 / clock.dt), step_fn=asm.update)
    assert asm.cycle_count > frozen_cycles, "急停复位后未恢复"
    print(f"[unit_assembly 自检通过] 产出={asm.products_out_total}件, "
          f"运行={asm.run_seconds:.1f}s, 停机原因={dict(asm.stop_counter)} (仿真验证值)")

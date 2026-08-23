# -*- coding: utf-8 -*-
"""
main.py —— Virtual-Smart-Factory 编排入口（班次1：仿真内核与产线层）
======================================================================
职责：
    1. 组装全厂：时钟 → 事件总线 → 设备注册表 → 故障注入器 → 四大单元；
    2. 物流接线：装配流出 → 视觉质检 → OK品码垛 → 满托呼叫AGV
       → 【占位AGV搬运】→ 立体库入库（班次2 将占位段替换为真实 AGV 调度）;
    3. 驱动循环：把"全厂 update"注册为时钟的每 tick 回调，
       实时模式(--mode realtime)与加速批量模式(--mode fast)走同一条推进路径，
       保证加速跑批结果一致；
    4. 控制台仪表：每 STATUS_PERIOD 仿真秒打印各单元状态与产量统计。

启动方式：
    python main.py                          # 默认 fast 加速跑 600 仿真秒, 倍率10x
    python main.py --speed 60               # 1/10/60 倍率预设（也接受任意正数）
    python main.py --mode realtime          # 实时模式（墙钟按倍率节拍）
    python main.py --duration 3600 --seed 42
    python main.py --no-random-faults       # 关闭随机故障（脚本故障保留）

扩展点（后续班次挂接处，均有注释标记 [班次2] / [班次3]）：
    - SCADA/MES/EMS：订阅事件总线即可，见 Plant._install_extension_hooks()
"""

import argparse
import random
import sys
from typing import Dict, List, Optional

import numpy as np

# 保证 Windows 控制台中文输出不乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.sim_clock import SimClock
from core.event_bus import EventBus, EventTypes
from core.device_base import DeviceBase, DeviceState
from core.fault_injector import FaultInjector
from lines.unit_assembly import UnitAssembly
from lines.unit_vision import UnitVision
from lines.unit_palletizing import UnitPalletizing
from lines.warehouse import Warehouse
from config import settings as S


class Plant:
    """全厂编排器：唯一拥有各单例对象引用的地方。"""

    def __init__(self, speed: float = S.DEFAULT_SPEED,
                 mode: str = "fast",
                 seed: int = S.DEFAULT_SEED,
                 enable_random_faults: bool = True):
        # ---- 内核 ----
        self.clock = SimClock(dt=S.SIM_DT, speed=speed)
        self.bus = EventBus(self.clock, log_dir=S.LOG_DIR)
        self.mode = mode
        self.seed = seed
        # ---- 设备与单元 ----
        self.assembly = UnitAssembly(self.clock, self.bus)
        self.vision = UnitVision(self.clock, self.bus,
                                 rng=np.random.default_rng(seed + 1))
        self.palletizer = UnitPalletizing(self.clock, self.bus)
        self.warehouse = Warehouse(self.clock, self.bus)
        # 设备注册表（故障注入与未来 Modbus 从站的遍历入口）
        self.devices: Dict[str, DeviceBase] = {
            d.device_id: d for d in
            (self.assembly, self.vision, self.palletizer, self.warehouse)}
        # ---- 故障注入（总开关恒开：急停联锁必须始终可用；--no-random-faults 只关随机子开关）----
        self.injector = FaultInjector(
            self.clock, self.bus, self.devices,
            rng=random.Random(seed + 77),
            enabled=True,
            random_enabled=enable_random_faults)
        # ---- 占位 AGV 调度器的在途任务表 {完成时刻: pallet_id} ----
        self._agv_transit: List[list] = []       # [due_sim_s, pallet_id]
        # ---- 状态打印节拍 ----
        self._next_report_at = S.STATUS_PERIOD
        # ---- 全厂急停标志 ----
        self.line_estop_latched = False

    # ==================================================================
    # 组装与接线
    # ==================================================================
    def build(self) -> None:
        """物流接线 + 订阅关系 + 注册每 tick 回调。重复调用幂等。"""
        # 1) 时钟每 tick 回调 = 全厂步进（两种模式共用同一路径）
        self.clock.set_step_callback(self.update)
        # 2) 占位 AGV：监听码垛 agv.call，安排 PLACEHOLDER_AGV_TRANSFER_TIME 后送达库口
        self.bus.subscribe(EventTypes.AGV_CALL, self._on_agv_call, "占位AGV调度")
        # 3) 扩展点安装（本班次为空实现+注释说明）
        self._install_extension_hooks()

    def _on_agv_call(self, event: dict) -> None:
        """
        【占位AGV调度——班次2 替换点】
        本班次假设：AGV 平均 PLACEHOLDER_AGV_TRANSFER_TIME 秒把满托从
        码垛出口送到立体库入口；到点后调用 warehouse.request_inbound()。
        班次2 只需把本方法替换为真实 AGV 任务状态机（取货-运输-交货三段）。
        """
        pallet_id = event["data"]["pallet_id"]
        due = round(self.clock.now() + S.PLACEHOLDER_AGV_TRANSFER_TIME, 3)
        self._agv_transit.append([due, pallet_id])

    def _install_extension_hooks(self) -> None:
        """
        后续班次扩展点（集中声明，防止散落改动）：
        -------------------------------------------------
        [班次2] SCADA Web 服务：Flask 订阅 bus.recent()/replay() 提供 REST+WebSocket，
                端口用 settings.SCADA_HTTP_PORT；
        [班次2] Modbus TCP 从站：pymodbus 按 devices[*].io_table 映射保持寄存器；
        [班次2] 真实 AGV 调度：替换 Plant._on_agv_call；
        [班次3] 视觉算法：覆写 UnitVision.judge()；
        [班次3] EMS/健康模块：订阅 EventTypes.FAULT_RAISED / DEVICE_STATE 提取特征。
        """
        pass

    # ==================================================================
    # 每 tick 全厂步进（顺序即物料流向，先注入故障再让设备响应）
    # ==================================================================
    def update(self, dt: float) -> None:
        now = self.clock.now()
        # 0) 占位 AGV 在途任务到点 → 交付立体库入库队列
        if self._agv_transit:
            due_now = [t for t in self._agv_transit if t[0] <= now]
            if due_now:
                for _, pallet_id in due_now:
                    self.warehouse.request_inbound(pallet_id)
                self._agv_transit = [t for t in self._agv_transit if t[0] > now]
        # 1) 故障注入器先行（本 tick 的故障立刻被下方设备逻辑感知）
        self.injector.update(dt)
        # 2) 产线单元按物料流方向推进
        self.assembly.update(dt)
        #   装配流出 → 视觉待检（直接搬队列，等价于一段无延迟输送线）
        while True:
            product = self.assembly.take_output()
            if product is None:
                break
            self.vision.inbound.append(product)
        self.vision.update(dt)
        #   OK 品 → 码垛
        while True:
            ok_product = self.vision.outbound.popleft() if self.vision.outbound else None
            if ok_product is None:
                break
            self.palletizer.inbound.append(ok_product)
        self.palletizer.update(dt)
        # 3) 立体库（含占位 AGV 交付的入库队列）
        self.warehouse.update(dt)
        # 4) 周期性控制台报表
        if now >= self._next_report_at:
            self._next_report_at += S.STATUS_PERIOD
            self.print_status()

    # ==================================================================
    # 运行控制
    # ==================================================================
    def start_up_all(self) -> None:
        """全厂上电切入自动。"""
        for dev in self.devices.values():
            dev.start_up()

    def trigger_line_estop(self) -> None:
        """联锁：急停 → 全线停（对全部设备注入需人工复位的'急停'故障）。"""
        self.line_estop_latched = True
        for dev in self.devices.values():
            dev.apply_fault("急停", {"scope": "全线"}, origin="interlock")
        self.print_status(force=True)

    def reset_line(self) -> None:
        """人工复位全线（急停/故障后）。"""
        self.line_estop_latched = False
        for dev in self.devices.values():
            dev.reset()
        self._next_report_at = self.clock.now() + S.STATUS_PERIOD

    def run(self, duration: Optional[float] = None) -> None:
        """
        启动仿真：
        - fast 模式：同步满速跑完 duration 仿真秒（自检冒烟同款路径）；
        - realtime 模式：后台线程按倍率推进，Ctrl+C 优雅退出。
        """
        self.build()
        self.start_up_all()
        print("\n=== Virtual-Smart-Factory 班次1 启动 ===")
        print(f"模式={self.mode} | 倍率={self.clock.speed}x | 步长={self.clock.dt}s | "
              f"时长={duration if duration else '∞(Ctrl+C退出)'} 仿真秒 | 种子={self.seed}")
        import time as _wt
        try:
            if self.mode == "realtime":
                # 实时模式：时钟线程按倍率节拍推进，本线程只负责到点停机
                if duration:
                    stop_at = self.clock.now() + duration
                    self.clock.start()
                    while self.clock.now() < stop_at:
                        _wt.sleep(0.1)
                    self.clock.stop()
                else:
                    self.clock.start()
                    while True:
                        _wt.sleep(3600)      # 由 KeyboardInterrupt 打断
            else:                            # fast 加速批量模式
                assert not self.clock.is_paused()
                end = self.clock.now() + (duration or S.DEFAULT_RUN_SECONDS)
                self.clock.run_until(end)
        except KeyboardInterrupt:
            print("\n[收到 Ctrl+C] 正在优雅停机…")
            self.clock.pause()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """停机：停时钟、关总线、打终报。"""
        self.clock.stop()
        self.print_status(force=True, final=True)
        self.bus.close()
        print(f"[事件日志] {len(self.bus.recent(10**6))} 条缓冲 / "
              f"共 {self.bus.total_published} 条已发布 → {self.bus.log_path}")

    # ==================================================================
    # 控制台仪表盘
    # ==================================================================
    def print_status(self, force: bool = False, final: bool = False) -> None:
        """打印全厂状态块（控制台版仪表盘；班次2 由 Web 端替代）。"""
        a, v, p, w = (self.assembly.snapshot(), self.vision.snapshot(),
                      self.palletizer.snapshot(), self.warehouse.snapshot())
        bar = "-" * 74
        head = "【终报】" if final else ""
        print(f"\n{bar}\n{head}仿真时刻 t={self.clock.now():>8.1f}s "
              f"(倍率{self.clock.speed}x{'·暂停' if self.clock.is_paused() else ''})\n{bar}")
        print(f"装配 {a['id']} [{a['state']}] 步骤:{a['step']} {a['step_progress']:>5.1f}%"
              f" | 流出:{a['products_out']}件 | 节拍:{a['takt_s']}s"
              f"{' | 门保持!' if a['door_hold'] else ''}")
        print(f"质检 {v['id']} [{v['state']}] | OK:{v['ok']} NG:{v['ng']}"
              f" (NG率{v['ng_rate']*100:.1f}%) | 待检:{v['queue_len']}"
              f" 返修道:{v['rework_len']}")
        print(f"码垛 {p['id']} [{p['state']}] | 码箱:{p['boxes']} 完成托:{p['pallets_done']}"
              f" | 当前垛 {p['current_fill']} @ {p['current_pallet']}")
        print(f"立体库 {w['id']} [{w['state']}] | 在库:{w['stock']}/{w['capacity']}"
              f" | 入队:{w['in_queue']} 出队:{w['out_queue']} 出口待运:{w['staging']}")
        inj = self.injector.snapshot()
        active = ", ".join(f"{f['dev']}:{f['type']}" for f in inj["active"]) or "无"
        print(f"故障注入 | 累计:{inj['injected_total']} 生效中: {active}")


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Virtual-Smart-Factory 班次1：仿真内核与产线层（全软件仿真，指标均为仿真验证值）")
    parser.add_argument("--speed", type=float, default=S.DEFAULT_SPEED,
                        help="加速倍率：推荐 1/10/60（默认10）")
    parser.add_argument("--mode", choices=["fast", "realtime"], default="fast",
                        help="fast=加速批量(默认)；realtime=实时墙钟模式")
    parser.add_argument("--duration", type=float, default=S.DEFAULT_RUN_SECONDS,
                        help="运行时长（仿真秒，默认600）")
    parser.add_argument("--seed", type=int, default=S.DEFAULT_SEED,
                        help="全局随机种子（保证可复现）")
    parser.add_argument("--no-random-faults", action="store_true",
                        help="关闭随机故障（脚本故障仍生效）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plant = Plant(speed=args.speed, mode=args.mode, seed=args.seed,
                  enable_random_faults=not args.no_random_faults)
    plant.run(duration=args.duration)


if __name__ == "__main__":
    main()

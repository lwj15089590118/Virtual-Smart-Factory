# -*- coding: utf-8 -*-
"""
main.py —— Virtual-Smart-Factory 编排入口
======================================================================
班次1：仿真内核与产线层；班次2修改：接入真实 AGV 车队 + --web SCADA 服务。
=======================================================================
职责：
    1. 组装全厂：时钟 → 事件总线 → 设备注册表 → 故障注入器 → 四大单元；
    2. 物流接线：装配流出 → 视觉质检 → OK品码垛 → 满托呼叫AGV
       → 【班次2：agv/agv_fleet.py 车队状态机搬运】→ 立体库入库；
       出库段：堆垛机下架 → out_staging → AGV 运抵出货口（出厂计数）;
    3. 驱动循环：把"全厂 update"注册为时钟的每 tick 回调，
       实时模式(--mode realtime)与加速批量模式(--mode fast)走同一条推进路径，
       保证加速跑批结果一致；
    4. 控制台仪表：每 STATUS_PERIOD 仿真秒打印各单元状态与产量统计；
    5. 班次2新增：--web 启动 Flask REST+WebSocket 大屏 与 pymodbus 从站，
       大屏按钮经 POST /api/command → Plant.execute_command() 公开方法。

启动方式：
    python main.py                          # 默认 fast 加速跑 600 仿真秒, 倍率10x
    python main.py --speed 60               # 1/10/60 倍率预设（也接受任意正数）
    python main.py --mode realtime          # 实时模式（墙钟按倍率节拍）
    python main.py --web                    # ★班次2演示：实时模式 + 监控大屏 + Modbus(1502)
    python main.py --duration 3600 --seed 42
    python main.py --no-random-faults       # 关闭随机故障（脚本故障保留）

扩展点（后续班次挂接处，均有注释标记 [班次2✔已实现] / [班次3]）：
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
from core.device_base import DeviceBase
from core.fault_injector import FaultInjector
from lines.unit_assembly import UnitAssembly
from lines.unit_vision import UnitVision
from lines.unit_palletizing import UnitPalletizing
from lines.warehouse import Warehouse
from agv.agv_fleet import AGVFleet          # 班次2修改：引入真实 AGV 车队
from config import settings as S


class Plant:
    """全厂编排器：唯一拥有各单例对象引用的地方。"""

    def __init__(self, speed: float = S.DEFAULT_SPEED,
                 mode: str = "fast",
                 seed: int = S.DEFAULT_SEED,
                 enable_random_faults: bool = True,
                 enable_agv: bool = True,                # 班次2修改：允许关闭车队做回归对照
                 enable_vision_algo: Optional[bool] = None,  # 修复：None=跟随 S.VISION_ALGO_ENABLE；
                 enable_ems: bool = True):               # 修复：EMS 装配开关（对齐 VISION/MES 风格）
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
        # ---- 班次2修改：真实 AGV 车队（替换班次1占位调度）----
        # 车辆一并注册进 devices：全线急停/人工复位天然覆盖车队
        self.enable_agv = enable_agv
        self.agv_fleet: Optional[AGVFleet] = None
        if enable_agv:
            self.agv_fleet = AGVFleet(self.clock, self.bus, self.warehouse,
                                      agv_count=S.AGV_COUNT)
            for agv in self.agv_fleet.agvs.values():
                self.devices[agv.device_id] = agv
        # ---- 故障注入（总开关恒开：急停联锁必须始终可用；--no-random-faults 只关随机子开关）----
        #     班次2修改：把 AGV 低频随机故障并入配置（车队容错演示用）
        rates = dict(S.RANDOM_FAULT_RATES)
        rates.update(S.AGV_RANDOM_FAULT_RATES)
        types_pool = dict(S.RANDOM_FAULT_TYPES)
        types_pool.update(S.AGV_RANDOM_FAULT_TYPES)
        self.injector = FaultInjector(
            self.clock, self.bus, self.devices,
            rng=random.Random(seed + 77),
            enabled=True,
            random_enabled=enable_random_faults,
            random_rates=rates, random_types=types_pool)
        # ---- 占位 AGV 在途任务表（仅 enable_agv=False 的回归模式使用）----
        self._agv_transit_legacy: List[list] = []    # [due_sim_s, pallet_id]
        # ---- 出库演示游标（班次2修改：每入库 N 托触发一次 FIFO 出库）----
        self._outbound_mark = 0
        # ---- 状态打印节拍 ----
        self._next_report_at = S.STATUS_PERIOD
        # ---- 全厂急停标志 ----
        self.line_estop_latched = False
        # ---- 班次3修改：扩展子系统占位（build() 时在 _install_extension_hooks 装配）----
        self.vision_algo = None      # 视觉算法升级包句柄（None=仍为规则法）
        self.mes = None              # MES 引擎（工单/追溯/报工）
        self.ems_energy = None       # EMS 能耗模型
        self.ems_health = None       # EMS 健康监视器
        # 修复记录：装配开关收敛为实例参数，CLI 只传参、不再改写 config 全局量
        self.enable_vision_algo = (S.VISION_ALGO_ENABLE
                                   if enable_vision_algo is None
                                   else bool(enable_vision_algo))
        self.enable_ems = bool(enable_ems) and S.EMS_ENABLE

    @property
    def _agv_transit(self) -> List[str]:
        """兼容属性（班次2修改）：班次1为占位在途表，现委托车队查询'已呼叫未交付库口'的托盘。
        selftest B1 托盘守恒口径继续可用；无车队模式退回旧表。"""
        if self.agv_fleet is not None:
            return self.agv_fleet.active_inbound_pallets()
        return [pid for _, pid in self._agv_transit_legacy]

    # ==================================================================
    # 组装与接线
    # ==================================================================
    def build(self) -> None:
        """物流接线 + 订阅关系 + 注册每 tick 回调。重复调用幂等。"""
        # 1) 时钟每 tick 回调 = 全厂步进（两种模式共用同一路径）
        self.clock.set_step_callback(self.update)
        # 2) 班次2修改：码垛满托 agv.call 改由真实 AGV 车队接管建档
        if self.agv_fleet is not None:
            self.bus.subscribe(EventTypes.AGV_CALL, self._on_agv_call, "AGV车队调度")
        else:
            # 无车队回归模式：沿用班次1占位逻辑（PLACEHOLDER 时间后直送库口）
            self.bus.subscribe(EventTypes.AGV_CALL, self._on_agv_call, "占位AGV调度")
        # 3) 扩展点安装（本班次为空实现+注释说明）
        self._install_extension_hooks()

    def _on_agv_call(self, event: dict) -> None:
        """
        【班次2修改】原占位调度已被真实 AGV 车队状态机替换；
        本方法保留为转发壳以兼容旧调用方（有车队转车队，无车队走班次1占位）。
        """
        pallet_id = event["data"]["pallet_id"]
        if self.agv_fleet is not None:
            self.agv_fleet.on_agv_call(event)
            return
        due = round(self.clock.now() + S.PLACEHOLDER_AGV_TRANSFER_TIME, 3)
        self._agv_transit_legacy.append([due, pallet_id])

    def _install_extension_hooks(self) -> None:
        """
        后续班次扩展点（集中声明，防止散落改动）：
        -------------------------------------------------
        [班次2✔] SCADA Web 服务：scada/web_server.py（Flask REST+WebSocket 推送），
                 由 run(enable_web=True) / main.py --web 启动；
        [班次2✔] Modbus TCP 从站：scada/modbus_server.py（io_table→保持寄存器）；
        [班次2✔] 真实 AGV 调度：agv/agv_fleet.py 替换占位搬运；
        [班次3✔] 视觉算法：vision/vision_upgrade.py 注入覆写 UnitVision.judge()
                 （保留规则法做 A/B 对照；--rule-vision 可退回规则法回归对照）；
        [班次3✔] MES：mes/mes_engine.py 订阅事件总线自动报工/追溯/OEE；
        [班次3✔] EMS：ems/energy_model.py 能耗积分 + ems/health_monitor.py 健康评分，
                 均只订阅 EventTypes 事件，不侵入仿真内核。
        """
        # ---- 班次3修改：视觉算法升级包注入（实例级覆写 judge，原类零改动）----
        # 修复记录：改用实例开关 enable_vision_algo 判断（CLI --rule-vision 走传参），
        # 配置中心 S.VISION_ALGO_ENABLE 仍作为默认值保留，运行期不再被外部改写
        if self.enable_vision_algo and S.VISION_ALGO_ENABLE:
            from vision.vision_upgrade import install_vision_upgrade
            self.vision_algo = install_vision_upgrade(self.vision, seed=self.seed)
            print(f"[班次3 视觉] 判定算法已注入: {self.vision_algo.ALGO_ID} "
                  f"(训练准确率{self.vision_algo.train_metrics['accuracy'] * 100:.1f}%"
                  f"/查全{self.vision_algo.train_metrics['recall'] * 100:.1f}%，仿真验证值)")
        # ---- 班次3修改：MES 引擎（订阅 "*" 自动建档报工）----
        if S.MES_ENGINE_ENABLE:
            from mes.mes_engine import MESEngine
            self.mes = MESEngine(self)
            print(f"[班次3 MES] 引擎已挂接，首张工单 {self.mes.orders[0].wo_id}"
                  f"(计划{self.mes.orders[0].target_qty}件)")
        # ---- 班次3修改：EMS 能耗模型 + 健康监视器（纯事件驱动，零内核侵入）----
        # 修复记录：受 EMS_ENABLE / enable_ems 双重开关控制（此前无条件装配，
        # 与 VISION/MES 的开关风格不一致）；关闭时 Web /api/ems/* 自动降级 enabled=False
        if self.enable_ems:
            from ems.energy_model import EnergyModel
            from ems.health_monitor import HealthMonitor
            self.ems_energy = EnergyModel(self)
            self.ems_health = HealthMonitor(self)
            print("[班次3 EMS] 能耗模型与健康监视器已订阅事件总线")

    # ==================================================================
    # 每 tick 全厂步进（顺序即物料流向，先注入故障再让设备响应）
    # ==================================================================
    def update(self, dt: float) -> None:
        now = self.clock.now()
        # 0) 占位在途结算（仅 enable_agv=False 的回归模式；班次2修改）
        if self.agv_fleet is None and self._agv_transit_legacy:
            due_now = [t for t in self._agv_transit_legacy if t[0] <= now]
            if due_now:
                for _, pallet_id in due_now:
                    self.warehouse.request_inbound(pallet_id)
                self._agv_transit_legacy = \
                    [t for t in self._agv_transit_legacy if t[0] > now]
        # 1) 故障注入器先行（本 tick 的故障立刻被下方设备逻辑感知）
        self.injector.update(dt)
        # 1.5) 班次2修改：AGV 车队步进（派单+六阶段状态机；交付动作写库口队列）
        if self.agv_fleet is not None:
            self.agv_fleet.update(dt)
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
        # 3) 立体库（含 AGV 交付的入库队列 / 出库任务下架）
        self.warehouse.update(dt)
        # 3.5) 班次2修改：出库演示——每入库 OUTBOUND_DEMO_EVERY_N 托，
        #      自动申请一托 FIFO 出库（堆垛机下架→out_staging→车队运抵出货口）
        if (S.OUTBOUND_DEMO_EVERY_N > 0
                and self.warehouse.inbound_done - self._outbound_mark
                >= S.OUTBOUND_DEMO_EVERY_N):
            self._outbound_mark = self.warehouse.inbound_done
            self.warehouse.request_outbound(None)
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

    def pallet_balance(self) -> dict:
        """
        托盘守恒分解（班次2修改：B1 不变量4 的权威口径）。
        恒等式：完成托 = 在库 + 入库队列 + AGV入库在途 + 出库暂存
                              + AGV出库在途(已装车未出厂) + 已出厂
        """
        fleet = self.agv_fleet
        outbound_inflight = 0
        if fleet is not None:
            for t in list(fleet.pending) + list(fleet.active.values()):
                if t.task_type != "出库" or t.agv_id is None:
                    continue
                agv = fleet.agvs.get(t.agv_id)
                # 仅"已装车(离开暂存区)、未交货"的托计入在途，避免与 staging 双计
                if (agv is not None and agv.current_task is t
                        and agv.phase in ("运输", "交货")):
                    outbound_inflight += 1
        return {
            "stock": self.warehouse.stock_count,
            "wh_in_queue": len(self.warehouse.inbound_q),
            "agv_inbound": len(self._agv_transit),
            "staging": len(self.warehouse.out_staging),
            "agv_outbound_inflight": outbound_inflight,
            "shipped": (self.agv_fleet.shipped_count if self.agv_fleet else 0),
        }

    def execute_command(self, cmd: str, params: Optional[dict] = None) -> dict:
        """
        【班次2新增】Web 大屏命令统一入口：REST /api/command → 本方法 → 公开API。
        只做参数校验与既有公开方法调用，不侵入仿真内核；返回 {"ok": bool, "msg": str}。
        """
        params = params or {}
        try:
            if cmd == "start":                      # 启动/恢复（幂等）
                was = self.clock.is_paused()
                self.clock.resume()
                self.start_up_all()
                return {"ok": True,
                        "msg": "全厂已启动" + ("（自暂停恢复）" if was else "")}
            if cmd == "pause":                      # 暂停推进
                if self.mode == "fast":
                    return {"ok": False,
                            "msg": "fast 批量模式不支持暂停，请用 --web（自动 realtime 模式）"}
                self.clock.pause()
                return {"ok": True, "msg": "仿真已暂停"}
            if cmd == "estop":                      # 全线急停（需人工复位）
                self.trigger_line_estop()
                return {"ok": True, "msg": "急停已触发：全线停止，等待人工复位"}
            if cmd == "reset":                      # 人工复位全线
                self.reset_line()
                return {"ok": True, "msg": "全线已复位（故障清除，回待机）"}
            if cmd == "door_open":                  # 开安全门 → 顺控保持
                self.assembly.set_door(True)
                return {"ok": True, "msg": "安全门已打开：装配顺控保持(HOLD)"}
            if cmd == "door_close":
                self.assembly.set_door(False)
                return {"ok": True, "msg": "安全门已关闭：顺控恢复"}
            if cmd == "set_speed":                  # 调倍率
                v = float(params.get("speed", 0))
                if v <= 0:
                    return {"ok": False, "msg": "倍率必须为正数"}
                self.clock.set_speed(v)
                return {"ok": True, "msg": f"加速倍率已设为 {v}x"}
            if cmd == "outbound":                   # 手动出库申请（FIFO 或指定托）
                pid = params.get("pallet_id") or None
                if pid is None and self.warehouse.stock_count == 0:
                    return {"ok": False, "msg": "立体库当前无在库托盘，无法出库"}
                ok = self.warehouse.request_outbound(pid)
                msg = (f"出库申请已受理：{pid or 'FIFO最早托'}（车队将自动建档运输）"
                       if ok else "出库申请被拒绝（托不在库或请求积压）")
                return {"ok": bool(ok), "msg": msg}
            # ---- 班次3修改：MES/EMS 命令（沿用班次2 REST 命令分发模式）----
            if cmd == "mes_new_order":              # 手动开立工单（qty/model 可选）
                if getattr(self, "mes", None) is None:
                    return {"ok": False, "msg": "MES 引擎未启用"}
                try:
                    qty = int(params.get("qty") or S.MES_DEFAULT_ORDER_QTY)
                except (TypeError, ValueError):
                    return {"ok": False, "msg": "计划数量须为正整数"}
                model = str(params.get("model") or S.MES_PRODUCT_MODEL)
                if qty <= 0 or qty > 100000:
                    return {"ok": False, "msg": "计划数量须为正数且不超过10万"}
                wo = self.mes.create_order(qty, model)
                return {"ok": True,
                        "msg": f"工单已开立: {wo.wo_id} 型号{model} 计划{qty}件"}
            if cmd == "ems_maintain":               # 健康 → 进入维护
                if getattr(self, "ems_health", None) is None:
                    return {"ok": False, "msg": "EMS 健康模块未启用"}
                dev_id = str(params.get("dev_id", ""))
                return self.ems_health.apply_maintenance(dev_id, reason="大屏命令下发")
            if cmd == "ems_maintain_done":          # 维护完成 → 待机
                if getattr(self, "ems_health", None) is None:
                    return {"ok": False, "msg": "EMS 健康模块未启用"}
                dev_id = str(params.get("dev_id", ""))
                return self.ems_health.exit_maintenance(dev_id)
            if cmd == "feeder_refill":              # 有限料仓补料（手动/演示）
                qty = params.get("qty")
                ret = self.assembly.feeder_refill(
                    int(qty) if qty not in (None, "") else None)
                ret["msg"] = (f"料仓补料 +{ret['added']} 件 → "
                              f"余 {ret['stock']}/{self.assembly.feeder_capacity}")
                return ret
            return {"ok": False, "msg": f"未知命令: {cmd}"}
        except Exception as exc:                    # 命令层兜底：异常不炸 Web 线程
            return {"ok": False, "msg": f"命令执行异常: {exc.__class__.__name__}: {exc}"}

    def run(self, duration: Optional[float] = None,
            enable_web: bool = False) -> None:
        """
        启动仿真：
        - fast 模式：同步满速跑完 duration 仿真秒（自检冒烟同款路径）；
        - realtime 模式：后台线程按倍率推进，Ctrl+C 优雅退出；
        - 班次2修改：enable_web=True 时同时启动 SCADA Web(REST+WS) 与
          Modbus TCP 从站两个 daemon 服务线程。
        """
        self.build()
        self.start_up_all()
        servers = []
        if enable_web:
            from scada.web_server import ScadaWebServer      # 局部导入防环
            from scada.modbus_server import ModbusServer
            web = ScadaWebServer(self)
            web.start()
            servers.append(web)
            mb = ModbusServer(self)
            mb.start()
            servers.append(mb)
            print(f"[班次2 SCADA] {web.info()}")
        print("\n=== Virtual-Smart-Factory 班次2 启动 ===")
        print(f"模式={self.mode} | 倍率={self.clock.speed}x | 步长={self.clock.dt}s | "
              f"时长={duration if duration else '∞(Ctrl+C退出)'} 仿真秒 | 种子={self.seed}"
              f" | AGV={'%d台' % len(self.agv_fleet.agvs) if self.agv_fleet else '关闭'}")
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
            for srv in servers:              # 班次2修改：停 Web/Modbus 服务
                try:
                    srv.stop()
                except Exception:
                    pass
            self.shutdown()

    def shutdown(self) -> None:
        """停机：停时钟、关总线、打终报。"""
        self.clock.stop()
        self.print_status(force=True, final=True)
        # 班次3修改：终报追加 MES 报工摘要（仿真验证值）
        if getattr(self, "mes", None) is not None:
            rep = self.mes.report()
            print(f"[MES 报工] 判定{rep['judged']}件(OK{rep['ok']}/NG{rep['ng']}) "
                  f"良率{rep['quality_pct']}% | 可用{rep['availability_pct']}% "
                  f"性能{rep['performance_pct']}% OEE≈{rep['oee_pct']}% | "
                  f"工单{len(self.mes.orders)}张 满托{rep['pallets_done']} 出厂{rep['shipped']}")
            # 增强：规范关闭 SQLite 台账连接（写入均已即时 commit，此处仅收尾）
            self.mes.close()
        # 班次3修改：终报追加 EMS 能耗/健康摘要（均为仿真验证值）
        if getattr(self, "ems_energy", None) is not None:
            es = self.ems_energy.snapshot()
            hs = self.ems_health.snapshot()
            print(f"[EMS 能耗] 全厂 {es['total_kwh']}kWh · 电费≈{es['cost_yuan']}元 "
                  f"· CO₂≈{es['co2_kg']}kg")
            worst = next((d for d in hs["devices"] if d["dev_id"] == hs["worst"]), None)
            print(f"[EMS 健康] 平均健康分 {hs['avg_score']} / 100，最差设备: "
                  f"{worst['dev_id']}({worst['score']}分·{worst['advice']})"
                  if worst else "[EMS 健康] 无设备数据")
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
        # 班次2修改：控制台同步显示 AGV 车队行（与 Web 大屏同源数据）
        if self.agv_fleet is not None:
            fs = self.agv_fleet.snapshot()
            cars = ", ".join(
                f"{g['id']}[{g['state']}·{g['phase']} @{g['battery']:.0f}%]"
                for g in fs["agvs"])
            print(f"AGV车队 | 待派:{fs['pending']} 执行:{fs['active']} "
                  f"完成:{fs['done']} 出厂:{fs['shipped']}托 | {cars}")


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Virtual-Smart-Factory 班次2：SCADA监控层+AGV物流+Web可视化（全软件仿真，指标均为仿真验证值）")
    parser.add_argument("--speed", type=float, default=S.DEFAULT_SPEED,
                        help="加速倍率：推荐 1/10/60（默认10）")
    parser.add_argument("--mode", choices=["fast", "realtime"], default=None,
                        help="fast=加速批量；realtime=实时墙钟模式（班次2修改：--web 未指定时默认 realtime）")
    parser.add_argument("--duration", type=float, default=None,
                        help="运行时长（仿真秒；班次2修改：默认无——普通跑600，--web 跑到 Ctrl+C）")
    parser.add_argument("--seed", type=int, default=S.DEFAULT_SEED,
                        help="全局随机种子（保证可复现）")
    parser.add_argument("--no-random-faults", action="store_true",
                        help="关闭随机故障（脚本故障仍生效）")
    # ---- 班次2修改：新增 Web/Modbus 开关 ----
    parser.add_argument("--web", action="store_true",
                        help="启动 SCADA 大屏(REST+WebSocket) 与 Modbus TCP 从站（演示推荐 --speed 1）")
    parser.add_argument("--no-agv", action="store_true",
                        help="关闭 AGV 车队（退回班次1占位搬运，用于回归对照）")
    # ---- 班次3修改：视觉算法回归对照开关 ----
    parser.add_argument("--rule-vision", action="store_true",
                        help="关闭班次3视觉算法注入（退回班次1规则法判定，用于 A/B 回归对照）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # 班次2修改：--web 默认走实时模式（大屏按钮的 暂停/倍率 才有墙钟语义）
    mode = args.mode or ("realtime" if args.web else "fast")
    # 班次2修改：时长缺省语义 —— 普通跑批 600s；--web 长驻直到 Ctrl+C
    duration = args.duration
    if duration is None:
        duration = None if args.web else S.DEFAULT_RUN_SECONDS
    # 班次3修改：--rule-vision 退回规则法判定
    # 修复记录：改为向 Plant 传参（enable_vision_algo=False），不再改写 S.VISION_ALGO_ENABLE 全局量
    plant = Plant(speed=args.speed, mode=mode, seed=args.seed,
                  enable_random_faults=not args.no_random_faults,
                  enable_agv=not args.no_agv,
                  enable_vision_algo=not args.rule_vision)
    plant.run(duration=duration, enable_web=args.web)


if __name__ == "__main__":
    main()

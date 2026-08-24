# -*- coding: utf-8 -*-
"""
selftest.py —— 全厂自检（逐模块自检 + 10 分钟加速联跑冒烟测试）
================================================================
自检内容：
    A. 模块级检查：时钟/总线/设备基类/注入器/装配/视觉/码垛/立体库/有限料仓
       （每项调用公开接口做行为断言，与各模块内置 __main__ 自检互补）；
    B. 系统级冒烟：用与 main.py 完全相同的 Plant 编排，fast 模式加速联跑
       600 仿真秒（=10 分钟），校验端到端物流守恒、事件落盘完整性；
    C. 班次3修改：新增 C 组用例——视觉算法指标达标(C1) / MES 追溯闭环(C2) /
       EMS 能耗与评分合理性(C3，电费为分时口径) / 指定数量订单全生命周期(C4)；
    D. 输出自检报告到 reports/selftest_report_*.txt 并打印结论。

运行方式：
    python selftest.py                 # 全部检查 + 冒烟
    python selftest.py --skip-smoke    # 只跑模块级检查（快速）
退出码：全部通过=0；任一失败=1（可接 CI）。

说明：报告中所有产量、NG率等数字均为"仿真验证值"。
"""

import argparse
import json
import os
import random
import sys
import traceback
from datetime import datetime

# 保证 Windows 控制台中文输出不乱码；并保证可从任意目录启动
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from core.sim_clock import SimClock
from core.event_bus import EventBus, EventTypes
from core.device_base import DeviceBase, DeviceState
from core.fault_injector import FaultInjector
from lines.unit_assembly import UnitAssembly, STEP_ORDER
from lines.unit_vision import UnitVision
from lines.unit_palletizing import UnitPalletizing
from lines.warehouse import Warehouse
from lines.product import Product
from main import Plant
from config import settings as S

# ----------------------------------------------------------------------
# 结果收集器
# ----------------------------------------------------------------------
RESULTS = []          # [(编号, 名称, 是否通过, 详情)]


def record(no: str, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((no, name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {no} {name} —— {detail}")


def run_case(no: str, name: str, fn) -> None:
    """执行单个用例：异常即判失败并保留堆栈摘要。"""
    try:
        detail = fn()
        record(no, name, True, detail or "通过")
    except AssertionError as exc:
        record(no, name, False, f"断言失败: {exc}")
    except Exception as exc:                       # 非断言异常同样算失败
        record(no, name, False, f"异常: {exc.__class__.__name__}: {exc}")
        traceback.print_exc()


# ======================================================================
# A. 模块级检查
# ======================================================================
def case_clock() -> str:
    """A1 时钟引擎：倍率/暂停语义 + 快速推进时间精度。"""
    c = SimClock(dt=S.SIM_DT, speed=S.DEFAULT_SPEED)
    assert abs(c.now()) < 1e-9
    c.set_speed(60)
    assert c.speed == 60.0
    c.pause()
    assert c.is_paused()
    try:
        c.run_until(10.0)
        raise AssertionError("暂停态 run_until 未拒绝")
    except RuntimeError:
        pass
    c.resume()
    hits = []
    c.set_step_callback(lambda d: hits.append(d))
    c.run_until(100.0)
    assert abs(c.now() - 100.0) < 1e-6, f"100s 推进误差过大: {c.now()}"
    assert abs(sum(hits) - 100.0) < 0.11, "步长之和不等于仿真时长"
    c.stop()
    return f"dt={c.dt}s, 100s推进 tick 数={len(hits)}, 时间误差<1e-6s"


def case_bus() -> str:
    """A2 事件总线：精确/通配订阅、JSONL 落盘行数一致。"""
    import tempfile
    clock = SimClock(dt=S.SIM_DT)
    tmpdir = tempfile.mkdtemp(prefix="vsf_selftest_")
    bus = EventBus(clock, log_dir=tmpdir)
    exact, wildcard = [], []
    bus.subscribe(EventTypes.DEVICE_STATE, lambda e: exact.append(e), "精确")
    bus.subscribe("*", lambda e: wildcard.append(e), "通配")
    for i in range(50):
        bus.publish("ST-BUS", EventTypes.DEVICE_STATE, {"i": i})
        bus.publish("ST-BUS", EventTypes.VISION_OK, {"i": i})
    bus.close()
    with open(bus.log_path, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert len(exact) == 50 and len(wildcard) == 100, "订阅分发数量错误"
    assert len(lines) == 100 == bus.total_published, "JSONL 行数与发布数不一致"
    assert lines[0]["ts_sim"] == 0.0 and lines[-1]["seq"] == 100
    # 中文数据往返不乱码
    bus2 = EventBus(clock, log_dir=tmpdir, persist=True)
    bus2.publish("ST-BUS", EventTypes.FAULT_RAISED, {"类型": "伺服过载"})
    bus2.close()
    with open(bus2.log_path, encoding="utf-8") as f:
        last = json.loads(f.read().strip().splitlines()[-1])
    assert last["data"]["类型"] == "伺服过载"
    return f"发布100条, 落盘{len(lines)}行, 中文序列化正常"


def make_device(clock, bus, dev_id="ST-DEV") -> DeviceBase:
    dev = DeviceBase(dev_id, "自检设备", clock, bus)
    dev.add_io("di_fb", "DI", 0, desc="反馈")
    return dev


def case_device() -> str:
    """A3 设备基类：五态迁移、运行秒数、停机原因单次记账、故障复位。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    dev = make_device(clock, bus)
    states = set()
    bus.subscribe(EventTypes.DEVICE_STATE, lambda e: states.add(e["data"]["state"]))
    dev.start_up()
    dev._set_state(DeviceState.RUNNING, "自动")
    clock.advance_ticks(100, step_fn=dev.update)          # 10s
    assert abs(dev.run_seconds - 10.0) < 1e-6
    dev.apply_fault("气压不足", origin="random")           # 运行中故障 → 停机原因记1次
    assert dev.state == DeviceState.FAULT
    assert dev.stop_counter["气压不足"] == 1, \
        f"停机原因应只记一次: {dict(dev.stop_counter)}"
    clock.advance_ticks(20, step_fn=dev.update)
    frozen = dev.run_seconds
    clock.advance_ticks(20, step_fn=dev.update)           # 故障期不再累积
    assert abs(dev.run_seconds - frozen) < 1e-9
    dev.reset()
    assert dev.state == DeviceState.STANDBY
    dev.enter_maintenance(); dev.exit_maintenance()
    assert dev.state == DeviceState.STANDBY
    assert {"待机", "运行", "故障", "维护"} <= states, f"状态覆盖不全: {states}"
    return f"五态迁移正常, 停机原因{dict(dev.stop_counter)}"


def case_injector() -> str:
    """A4 故障注入器：脚本定时触发+自动恢复、急停需人工复位、随机概率生效。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    d = make_device(clock, bus, "ST-INJ")
    d.start_up()
    d._set_state(DeviceState.RUNNING, "自动")
    inj = FaultInjector(clock, bus, {d.device_id: d}, rng=random.Random(99),
                        random_rates={},                  # 关随机，纯脚本
                        scripted=[{"at": 3.0, "target": d.device_id,
                                   "type": "传感器信号丢失", "duration": 8.0}])
    raised, cleared = [], []
    bus.subscribe(EventTypes.FAULT_RAISED, lambda e: raised.append(e))
    bus.subscribe(EventTypes.FAULT_CLEARED, lambda e: cleared.append(e))
    clock.advance_ticks(int(15.0 / S.SIM_DT), step_fn=inj.update)   # 0→15s
    assert len(raised) == 1 and raised[0]["data"]["origin"] == "script"
    assert len(cleared) == 1 and cleared[0]["data"]["note"] == "自动恢复"
    assert inj.trigger(d.device_id, "急停", duration=None, origin="interlock")
    clock.advance_ticks(int(60.0 / S.SIM_DT), step_fn=inj.update)   # 急停不自动恢复
    assert d.current_fault == "急停"
    d.reset()
    # 随机链路：超高概率下必命中
    inj2 = FaultInjector(clock, bus, {d.device_id: d}, rng=random.Random(5),
                         random_rates={d.device_id: 36000.0}, scripted=[])
    for _ in range(300):
        d.reset()
        inj2.update(S.SIM_DT)
        if inj2.stats_injected > 0:
            break
    assert inj2.stats_injected >= 1, "高故障率未命中随机注入"
    return (f"脚本触发1次@t=3.0s, 自动恢复1次, 急停人工复位, "
            f"随机注入累计{inj2.stats_injected}次")


def case_assembly_logic() -> str:
    """A5 装配单元逻辑：节拍一致性 + 门保持冻结 + 急停全线停。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    asm = UnitAssembly(clock, bus, unit_id="ASM-ST",
                       step_durations=dict(S.ASSEMBLY_STEP_DURATIONS))
    asm.start_auto()
    outs = []
    bus.subscribe(EventTypes.PRODUCT_OUT, lambda e: outs.append(e))
    # 无干扰跑 10 个完整节拍
    takt = asm.takt_seconds
    clock.advance_ticks(int(takt * 10 / S.SIM_DT), step_fn=asm.update)
    assert len(outs) == 10, f"{takt}s×10 应产出10件, 实际{len(outs)}"
    # 门保持：开门期间产出与运行秒数双冻结
    asm.set_door(True)
    out_mark, run_mark = asm.products_out_total, asm.run_seconds
    holds, resumes = [], []
    bus.subscribe(EventTypes.DOOR_HOLD, lambda e: holds.append(e))
    bus.subscribe(EventTypes.DOOR_RESUME, lambda e: resumes.append(e))
    clock.advance_ticks(int(10.0 / S.SIM_DT), step_fn=asm.update)
    assert holds and not resumes, "门开应产生保持事件且无恢复事件"
    assert asm.products_out_total == out_mark and abs(asm.run_seconds - run_mark) < 1e-9
    asm.set_door(False)
    clock.advance_ticks(int((takt * 12) / S.SIM_DT), step_fn=asm.update)
    assert resumes, "关门后应有恢复事件"
    assert asm.products_out_total >= out_mark + 10, "关门后未恢复产出"
    # 急停 → 故障 → 复位续走
    frozen_cycles = asm.cycle_count
    asm.apply_fault("急停", origin="interlock")
    clock.advance_ticks(int(5.0 / S.SIM_DT), step_fn=asm.update)
    assert asm.state == DeviceState.FAULT and asm.cycle_count == frozen_cycles
    asm.reset()
    clock.advance_ticks(int(takt * 2 / S.SIM_DT), step_fn=asm.update)
    assert asm.cycle_count > frozen_cycles
    return (f"节拍{takt}s×10件精确产出, 门保持冻结, 急停复位后恢复; "
            f"累计流出{asm.products_out_total}件")


def case_vision_rule() -> str:
    """A6 视觉单元规则判定：全量判定、分流正确、NG率落在理论带宽。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    vis = UnitVision(clock, bus, unit_id="VIS-ST",
                     rng=np.random.default_rng(S.DEFAULT_SEED + 1))
    vis.start_up()
    n = 500
    for i in range(n):
        vis.inbound.append(Product(f"SV{i:08d}", born_at=clock.now(), source_unit="T"))
    clock.advance_ticks(int(n * S.VISION_INSPECT_TIME * 1.2 / S.SIM_DT), step_fn=vis.update)
    judged = vis.ok_total + vis.ng_total
    assert judged == n, f"应判{n}件, 实际{judged}"
    assert len(vis.outbound) == vis.ok_total and len(vis.rework_lane) == vis.ng_total
    assert all(p.qc_result == "NG" and p.rework for p in vis.rework_lane)
    ng_rate = vis.ng_rate()
    sigma, tol = S.VISION_SIGMA, S.VISION_TOLERANCE
    z = tol / sigma
    from math import erf, sqrt
    p_ng_theory = 2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))
    assert abs(ng_rate - p_ng_theory) < 0.03, \
        f"NG率{ng_rate:.3f} 偏离理论{p_ng_theory:.3f} 过大"
    assert len(vis.qc_records) == n
    return (f"判定{n}件, NG率{ng_rate*100:.1f}% vs 理论{p_ng_theory*100:.1f}% "
            f"(σ={sigma}, 公差±{tol}mm)")


def case_palletizing_pattern() -> str:
    """A7 码垛单元：48箱满托、垛型坐标、AGV呼叫事件。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    pal = UnitPalletizing(clock, bus, unit_id="PAL-ST")
    pal.start_up()
    fulls, calls, boxes_ev = [], [], []
    bus.subscribe(EventTypes.PALLET_FULL, lambda e: fulls.append(e))
    bus.subscribe(EventTypes.AGV_CALL, lambda e: calls.append(e))
    bus.subscribe(EventTypes.BOX_PLACED, lambda e: boxes_ev.append(e["data"]))
    cap = pal.pallet_capacity
    for i in range(cap + 3):                          # 51箱 → 1满托+3散箱
        pal.inbound.append(Product(f"SP{i:08d}", born_at=clock.now(), source_unit="T"))
    need = (cap + 3) * S.BOX_PLACE_TIME + S.PALLET_OUT_TIME + 2.0
    clock.advance_ticks(int(need / S.SIM_DT), step_fn=pal.update)
    assert len(fulls) == 1 and len(calls) == 1, \
        f"满托/AGV呼叫应各1次: {len(fulls)}/{len(calls)}"
    assert pal.pallets_done[0]["box_count"] == cap == 48
    # 格坐标唯一性 + 层内顺序
    coords = {(b["x"], b["y"], b["z"]) for b in pal.pallets_done[0]["boxes"]}
    assert len(coords) == cap, "垛内格坐标重复"
    assert max(z for _, _, z in coords) == 3          # 4层
    # BOX_PLACED 事件携带毫米坐标（班次2 ECharts 3D 数据源）
    b0 = boxes_ev[0]
    assert {"px_mm", "py_mm", "pz_mm"} <= set(b0)
    return (f"满托{cap}箱, AGV呼叫1次, 垛型坐标{len(coords)}格唯一, "
            f"毫米坐标字段齐全")


def case_warehouse_structs() -> str:
    """A8 立体库：库位表规模、出入库队列、库位回收复用。"""
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    wh = Warehouse(clock, bus, unit_id="WH-ST")
    wh.start_up()
    assert wh.capacity == S.WH_ROWS * S.WH_BAYS * S.WH_LEVELS
    ins, outs = [], []
    bus.subscribe(EventTypes.WH_INBOUND_DONE, lambda e: ins.append(e))
    bus.subscribe(EventTypes.WH_OUTBOUND_DONE, lambda e: outs.append(e))
    for i in range(8):
        assert wh.request_inbound(f"SW{i:06d}")
    clock.advance_ticks(int(8 * S.WH_TASK_TIME * 1.2 / S.SIM_DT), step_fn=wh.update)
    assert wh.stock_count == 8 and len(ins) == 8
    locs_used = {e["data"]["loc_id"] for e in ins}
    assert len(locs_used) == 8
    assert wh.request_outbound("SW000003")
    clock.advance_ticks(int(S.WH_TASK_TIME * 1.5 / S.SIM_DT), step_fn=wh.update)
    assert outs[0]["data"]["pallet_id"] == "SW000003" and wh.stock_count == 7
    freed_loc = outs[0]["data"]["from_loc"]
    assert wh.request_inbound("SWNEW01")              # 新托入库可复用刚释放的库位
    clock.advance_ticks(int(S.WH_TASK_TIME * 1.5 / S.SIM_DT), step_fn=wh.update)
    assert wh.locate("SWNEW01") is not None
    table = wh.locations()
    assert len(table) == wh.capacity
    return (f"200库位表, 入8出1, 库位{freed_loc}释放后复用成功, 在库{wh.stock_count}")


def case_finite_feeder() -> str:
    """A9 有限料仓：料空冻结于等待上料 → REST 补料续产 → 低水位滞回告警恰一次。"""
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    asm = plant.assembly
    asm.feeder_stock = 2                       # 制造"即将料空"场景（阈值20之上仅剩2件）
    low_ev, empty_ev, refill_ev = [], [], []
    plant.bus.subscribe(EventTypes.FEEDER_LOW, lambda e: low_ev.append(e))
    plant.bus.subscribe(EventTypes.FEEDER_EMPTY, lambda e: empty_ev.append(e))
    plant.bus.subscribe(EventTypes.FEEDER_REFILL, lambda e: refill_ev.append(e))
    plant.build()                              # build 在订阅事件之后亦可（事件走总线）
    plant.start_up_all()

    def advance_until(cond, max_s: float) -> bool:
        deadline = plant.clock.now() + max_s
        while plant.clock.now() < deadline:
            if cond():
                return True
            plant.clock.run_until(plant.clock.now() + 1.0)
        return cond()

    # ---- 阶段1：耗尽最后两件正常产出，随后料空冻结于"等待上料" ----
    out_mark = asm.products_out_total
    ok = advance_until(lambda: (asm.products_out_total >= out_mark + 2
                                and len(empty_ev) >= 1), 240)
    assert ok, f"未如期耗尽并触发 feeder.empty: out={asm.products_out_total}"
    assert asm.current_step_name() == "等待上料" and asm.feeder_stock == 0, \
        f"料空状态异常: step={asm.current_step_name()} stock={asm.feeder_stock}"
    frozen_out = asm.products_out_total
    assert len(low_ev) == 1, f"低水位告警应恰好一次(滞回): {len(low_ev)}"
    plant.clock.run_until(plant.clock.now() + 60.0)   # 冻结期产量必须纹丝不动
    assert asm.products_out_total == frozen_out, "料空期间产量未被冻结"

    # ---- 阶段2：REST 命令补料 → 断点续走；低水位不重复告警 ----
    rv = plant.execute_command("feeder_refill", {})
    assert rv["ok"] and rv["stock"] == S.FEEDER_REFILL_QTY, f"补料异常: {rv}"
    n_refill = len(refill_ev)
    ok = advance_until(lambda: asm.products_out_total >= frozen_out + 3, 240)
    assert ok, "补料后未恢复产出"
    assert len(refill_ev) == n_refill and len(low_ev) == 1, \
        "补料后不应再触发低水位/额外补料事件"
    snap = asm.snapshot()
    assert snap["feeder_state"] in ("正常", "低"), f"料仓状态异常: {snap['feeder_state']}"
    return (f"料空冻结于等待上料(冻结期产出0) → REST补料+{rv['added']}恢复产出"
            f"+3件; 事件账目 low×{len(low_ev)}/empty×{len(empty_ev)}/refill×{len(refill_ev)} "
            f"(仿真验证值)")


# ======================================================================
# B. 班次2 新增用例：Web API 冒烟（B2）/ AGV 任务闭环（B3）
# ======================================================================
def case_web_api() -> str:
    """B2 Web API 冒烟：Flask test_client 全端点 + 命令链路 + Modbus 映射。
    （不绑定真实端口，CI 安全；真实 HTTP/WS/Modbus 由各模块 __main__ 冒烟覆盖）"""
    from scada.web_server import ScadaWebServer
    from scada.modbus_server import build_register_map

    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    srv = ScadaWebServer(plant)
    client = srv.app.test_client()

    # 1) 首页可达且为监控大屏
    rv = client.get("/")
    assert rv.status_code == 200 and "Virtual-Smart-Factory".encode() in rv.data, "首页异常"

    # 2) 六个只读端点全通
    endpoints = ("/api/status", "/api/kpi", "/api/events?n=20",
                 "/api/pallet3d", "/api/warehouse/locations", "/api/modbus/map")
    for path in endpoints:
        rv = client.get(path)
        assert rv.status_code == 200, f"{path} 状态码 {rv.status_code}"
        assert rv.get_json().get("ok") is True, f"{path} ok 字段异常"

    # 3) 推进 40 仿真秒后 KPI 必须出现真实产量
    plant.clock.run_until(plant.clock.now() + 40.0)
    kpi = client.get("/api/kpi").get_json()["kpi"]
    assert kpi["products_out"] >= 1, f"KPI 无产量: {kpi}"
    assert kpi["note"].startswith("所有指标均为仿真验证值")

    # 4) 命令链路：开门保持 → 关门恢复 → 急停 → 复位
    for cmd in ("door_open", "door_close", "estop", "reset"):
        rv = client.post("/api/command", json={"cmd": cmd, "params": {}})
        assert rv.status_code == 200 and rv.get_json()["ok"], \
            f"命令 {cmd} 失败: {rv.get_json()}"
    from core.device_base import DeviceState
    assert plant.assembly.state == DeviceState.STANDBY, "复位后装配应回待机"
    # 未知命令必须被拒绝(400)而非崩溃
    rv = client.post("/api/command", json={"cmd": "no_such_cmd"})
    assert rv.status_code == 400 and rv.get_json()["ok"] is False

    # 5) WS 广播通路（零客户端也不得抛异常）
    plant.bus.publish("ST-B2", EventTypes.VISION_OK, {"product_id": "B2X"})

    # 6) Modbus 寄存器映射：6 设备块(4单元+2AGV)，地址不重叠
    mb_map = build_register_map(plant.devices)
    assert len(mb_map) >= 6, f"映射块不足: {len(mb_map)}"
    bases = [b["base"] for b in mb_map]
    for i in range(len(mb_map) - 1):
        assert bases[i] < bases[i + 1], "寄存器块基址应严格递增"
    srv.stop()
    return (f"页面+{len(endpoints)}个API全200; 命令链路4条OK+未知命令400; "
            f"WS广播无异常; Modbus映射{len(mb_map)}块")


def case_agv_loop() -> str:
    """B3 AGV 任务闭环：码垛满托 agv.call → 车队六阶段搬运 → 入库完成
    → 手动出库申请 → AGV 运抵出货口出厂。"""
    from lines.product import Product

    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)      # 关随机故障保证确定性节奏
    plant.build()
    plant.start_up_all()
    # 直接向码垛单元灌 96 箱 OK 品 → 恰好 2 个满托
    # （绕过装配慢节拍；码垛→事件→车队→立体库 全链路仍走真实编排路径）
    for i in range(96):
        plant.palletizer.inbound.append(
            Product(f"B3{i:08d}", born_at=plant.clock.now(), source_unit="B3-T"))
    plant.clock.run_until(plant.clock.now() + 320.0)

    # ---- 断言1：满托与 agv.call ----
    assert len(plant.palletizer.pallets_done) == 2, \
        f"应完成2托: {len(plant.palletizer.pallets_done)}"
    calls = plant.bus.replay(lambda e: e["type"] == EventTypes.AGV_CALL)
    assert len(calls) >= 2, f"agv.call 应≥2: {len(calls)}"

    # ---- 断言2：车队入库任务闭环（交付库口）----
    done_in = plant.bus.replay(lambda e: e["type"] == EventTypes.AGV_TASK_DONE
                               and e["data"]["task_type"] == "入库")
    assert len(done_in) >= 2, f"AGV入库任务闭环不足: {len(done_in)}"
    w = plant.warehouse.snapshot()
    assert w["inbound_done"] >= 2, f"立体库应已完成≥2次上架: {w['inbound_done']}"

    # ---- 断言3：六阶段状态机全部出现 ----
    phases = {e["data"]["phase"] for e in
              plant.bus.replay(lambda e: e["type"] == EventTypes.AGV_PHASE)}
    need = {"空闲", "去取货", "装载", "运输", "交货", "回位"}
    assert need <= phases, f"阶段缺失: {need - phases}"

    # ---- 断言4：出库段闭环（FIFO 出库 → AGV → 出厂）----
    assert plant.warehouse.request_outbound(None), "FIFO出库申请失败"
    plant.clock.run_until(plant.clock.now() + 90.0)
    assert plant.agv_fleet.shipped_count == 1, \
        f"应有1托运抵出货口: {plant.agv_fleet.shipped_count}"

    # ---- 断言5：托盘守恒（班次2权威口径）----
    lhs = plant.palletizer.snapshot()["pallets_done"]
    bal = plant.pallet_balance()
    assert lhs == sum(bal.values()), f"守恒失败: {lhs} vs {bal}"

    fs = plant.agv_fleet.snapshot()
    return (f"2托满托→AGV入库闭环×{len(done_in)}; 六阶段{sorted(phases)[0]}…齐全; "
            f"出库出厂{plant.agv_fleet.shipped_count}托; 守恒{bal}")


def case_agv_recharge() -> str:
    """B4 AGV 低电量回充排程：单工位互斥派位 → 任务优先不中断 → 滞回充满让位轮换。"""
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    fl = plant.agv_fleet
    a1, a2 = fl.agvs["AGV-01"], fl.agvs["AGV-02"]

    def advance_until(cond, max_s: float) -> bool:
        """小步推进直至条件满足或超时（确定性边界内轮询）。"""
        deadline = plant.clock.now() + max_s
        while plant.clock.now() < deadline:
            if cond():
                return True
            plant.clock.run_until(plant.clock.now() + 1.0)
        return cond()

    # ---- 阶段1：两台同时低电量，单工位只派一台（花名册序）----
    a1.battery = S.AGV_BATTERY_LOW - 5
    a2.battery = S.AGV_BATTERY_LOW - 10
    plant.clock.run_until(plant.clock.now() + 2.0)
    assert fl.charge_occupant == "AGV-01", \
        f"应按花名册先派 AGV-01 回充: {fl.charge_occupant}"
    assert a1.phase in ("去充电", "充电中"), f"AGV-01 相位异常: {a1.phase}"
    assert a2.phase == "空闲", f"单工位互斥被破坏: a2 相位 {a2.phase}"

    # ---- 阶段2：真实运输任务优先，由未充电的 AGV-02 承接；充电车不受扰 ----
    fl.on_agv_call({"data": {"pallet_id": "CHG-T1"}})   # 直呼建档入库任务
    ok = advance_until(lambda: a2.phase in ("运输", "交货")
                       and a2.current_task is not None, 25)
    assert ok, f"任务未被空闲车承接: a2={a2.phase}"
    assert a1.phase in ("去充电", "充电中"), "回充行程不应被派单打断"

    # ---- 阶段3：a1 充至恢复阈值后让位归位 ----
    ok = advance_until(lambda: a1.phase == "空闲"
                       and a1.battery >= S.AGV_BATTERY_OK - 0.01, 45)
    assert ok, f"a1 未按时充满归位: phase={a1.phase} battery={a1.battery}"

    # ---- 阶段4：轮换——a2 仍低于阈值则获得充电位；或已涓流越阈则豁免 ----
    ok = advance_until(
        lambda: fl.charge_occupant == "AGV-02"
        or a2.battery >= S.AGV_BATTERY_OK, 90)
    assert ok, (f"a2 未轮到回充: occupant={fl.charge_occupant} "
                f"bat={a2.battery}")
    rep = fl.snapshot()
    return (f"双车低电: 先派AGV-01(单工位互斥) → 任务由空闲车承接 → "
            f"a1充至{round(a1.battery, 1)}%让位 → 轮换完成; "
            f"车队done={rep['done']} (仿真验证值)")


# ======================================================================
# C. 班次3修改：新增用例 C1 视觉算法指标 / C2 MES 追溯闭环 / C3 EMS 合理性
# ======================================================================
def case_vision_algo() -> str:
    """C1 视觉算法指标达标：三方 A/B 对照达标 + judge 注入后在线混淆矩阵自洽。"""
    from vision.defect_generator import evaluate_classifiers
    from vision.vision_upgrade import install_vision_upgrade

    # ---- 1) 离线评估：规则法 vs 逻辑回归 vs 单类马氏（独立测试集口径）----
    ev = evaluate_classifiers(seed=S.DEFAULT_SEED)
    lr, rule = ev["logistic_regression"], ev["rule"]
    assert lr["accuracy"] >= S.VISION_CLF_ACC_MIN, \
        f"LR 准确率 {lr['accuracy']} 低于达标线 {S.VISION_CLF_ACC_MIN}"
    assert lr["recall"] > rule["recall"] + 0.10, \
        f"查全率优势未体现: LR={lr['recall']} vs 规则={rule['recall']}"
    assert lr["f1"] > rule["f1"], "LR 综合 F1 不应低于规则法"
    cm = lr["confusion"]
    assert sum(cm.values()) == ev["test_n"], "混淆矩阵四要素之和应等于测试样本数"

    # ---- 2) 在线注入：judge 覆写后全流程判定 + 记录明细回填 ----
    clock = SimClock(dt=S.SIM_DT)
    bus = EventBus(clock, persist=False)
    vis = UnitVision(clock, bus, unit_id="VIS-C1",
                     rng=np.random.default_rng(S.DEFAULT_SEED + 1))
    algo = install_vision_upgrade(vis, seed=S.DEFAULT_SEED)
    vis.start_up()
    n = 200
    for i in range(n):
        vis.inbound.append(Product(f"C1{i:08d}", born_at=clock.now(), source_unit="T"))
    clock.advance_ticks(int(n * S.VISION_INSPECT_TIME * 1.3 / S.SIM_DT),
                        step_fn=vis.update)
    judged = vis.ok_total + vis.ng_total
    assert judged == n, f"注入后应全量判定{n}件: {judged}"
    rec = vis.qc_records[-1]
    assert {"algo", "clf_p_ng", "rule_result", "features"} <= set(rec), \
        f"判定明细未回填进质检记录: {list(rec.keys())}"
    om = algo.online_metrics()
    assert om["n"] == n and sum(om["confusion"].values()) == n, "在线混淆矩阵账目不平"
    assert om["clf_acc"] >= 0.90, f"在线准确率异常: {om['clf_acc']}"
    ng_rate = vis.ng_rate()
    return (f"离线: LR准确率{lr['accuracy']*100:.2f}%/查全{lr['recall']*100:.1f}% "
            f"vs 规则{rule['accuracy']*100:.2f}%/{rule['recall']*100:.1f}%; "
            f"在线{n}件: 准确率{om['clf_acc']*100:.1f}%, NG率{ng_rate*100:.1f}%"
            f"(真值先验≈7%) (仿真验证值)")


def case_mes_trace() -> str:
    """C2 MES 追溯闭环：产品→托盘→批次→工单四级反查 + JSONL 回放重建一致。"""
    from mes.jsonl_replay import replay_file

    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    assert plant.mes is not None and len(plant.mes.orders) == 1, "应自动开立首张工单"
    wo0 = plant.mes.orders[0]
    # 直接向视觉待检队列灌 48 件（绕过装配慢节拍；判定→码垛→AGV→入库走真实编排）
    for i in range(48):
        plant.vision.inbound.append(
            Product(f"C2{i:08d}", born_at=plant.clock.now(), source_unit="C2-T"))
    # 48件×2.5s检测 + 码垛48×1.2s+5s输出 + AGV≈15s + 堆垛25s ≈ 220s，给 300s
    plant.clock.run_until(plant.clock.now() + 300.0)

    # ---- 断言1：报工台账（直灌48件 + 装配线并行产出若干件，故用下界断言）----
    rep = plant.mes.report()
    assert rep["judged"] >= 48, f"报工应≥48件: {rep['judged']}"
    assert rep["ok"] + rep["ng"] == rep["judged"] and rep["oee_pct"] > 0
    # ---- 断言2：满托追溯（四级反查）----
    assert len(plant.palletizer.pallets_done) == 1, "应完成1托"
    pallet_id = plant.palletizer.pallets_done[0]["pallet_id"]
    tr = plant.mes.trace(pallet_id)
    assert tr is not None and tr["kind"] == "托盘", "满托应能反查到"
    chain = tr["chain"]
    assert chain["wo_id"] == wo0.wo_id, f"托盘应归属首张工单: {chain['wo_id']}"
    assert len(chain["products"]) == 48, "托盘下应有48件产品"
    stages = [e[1] for e in chain["events"]]
    assert "码垛完成" in stages and "入库上架" in stages, f"流转档案缺失: {stages}"
    loc = plant.warehouse.locate(pallet_id)
    assert loc is not None and tr["status"] == f"在库 {loc}"
    # ---- 断言3：产品级反查 → 托盘 → 工单 ----
    pid = chain["products"][0]
    tp = plant.mes.trace(pid)
    assert tp["kind"] == "产品" and tp["chain"]["pallet_id"] == pallet_id \
        and tp["chain"]["wo_id"] == wo0.wo_id
    # ---- 断言4：REST 命令模式手动开单（照抄班次2分发+审计，走 test_client 全链路）----
    from scada.web_server import ScadaWebServer
    srv = ScadaWebServer(plant)
    client = srv.app.test_client()
    rv = client.post("/api/command", json={"cmd": "mes_new_order",
                                           "params": {"qty": 100}})
    assert rv.status_code == 200 and rv.get_json()["ok"], \
        f"手动开单失败: {rv.get_json()}"
    assert len(plant.mes.orders) == 2, "应新开一张工单"
    audit = plant.bus.replay(lambda e: e["type"] == EventTypes.UI_COMMAND
                             and e["data"]["cmd"] == "mes_new_order")
    assert audit, "开单命令未落 ui.command 审计"
    srv.stop()
    # ---- 断言5：JSONL 回放重建台账与在线一致（交付要求的数据源口径）----
    plant.bus.close()
    eng = replay_file(plant.bus.log_path)
    assert eng.stat_ok == plant.mes.stat_ok and eng.stat_ng == plant.mes.stat_ng, \
        f"回放报工不一致: {eng.stat_ok}/{eng.stat_ng} vs {plant.mes.stat_ok}/{plant.mes.stat_ng}"
    rt = eng.trace(pallet_id)
    assert rt is not None and rt["chain"]["wo_id"] == wo0.wo_id, "回放后追溯链路断裂"
    return (f"48件全链路闭环: 报工OK{rep['ok']}/NG{rep['ng']}, OEE≈{rep['oee_pct']}%; "
            f"{pallet_id} 归属 {wo0.wo_id}-{chain['batch_id']} 在库{loc}; "
            f"JSONL回放重建一致 (仿真验证值)")


def case_ems_energy_health() -> str:
    """C3 EMS 能耗与评分合理性：kWh 积分单调、健康扣分方向正确、维护命令闭环。"""
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    assert plant.ems_energy is not None and plant.ems_health is not None

    # ---- 断言1：能耗积分合理（运行段累计为正且随时间单调增加）----
    plant.clock.run_until(60.0)
    e1 = plant.ems_energy.snapshot()
    assert e1["total_kwh"] > 0, "装配持续运行 60s 应有能耗累积"
    asm1 = next(d for d in e1["devices"] if d["id"] == S.ASSEMBLY_ID)
    expect_asm = S.EMS_POWER_KW["ASM-"]["运行"] * 60.0 / 3600.0
    assert abs(asm1["kwh"] - expect_asm) < 0.02, \
        f"装配 kWh 积分误差过大: {asm1['kwh']} vs {expect_asm:.4f}"
    # 分时电价口径（增强）：t∈[0,60s] 即 00:00-01:00，全部落在"谷"档计费
    from ems.energy_model import tou_price_at
    tier60, price60 = tou_price_at(30.0)
    assert S.EMS_TOU_ENABLED and tier60 == "谷", f"00:30 应处谷档: {tier60}"
    assert abs(e1["cost_yuan"] - e1["total_kwh"] * price60) < 0.01, \
        f"谷档电费不符: {e1['cost_yuan']} vs {e1['total_kwh'] * price60:.4f}"
    assert abs(sum(t["yuan"] for t in e1["tiers"]) - e1["cost_yuan"]) < 0.01, \
        f"分档电费合计应等于总电费: {e1['tiers']}"
    plant.clock.run_until(120.0)
    e2 = plant.ems_energy.snapshot()
    assert e2["total_kwh"] > e1["total_kwh"], "能耗应随仿真时间单调增加"

    # ---- 断言2：健康评分——无故障期近似满分（仅启停切换的微小扣分）----
    h0 = plant.ems_health.snapshot()
    assert all(dv["score"] >= 98.0 for dv in h0["devices"]), \
        f"无故障期评分应≥98: {[(d['dev_id'], d['score']) for d in h0['devices']]}"
    plant.trigger_line_estop()                        # 全线急停（需人工复位）
    plant.clock.run_until(160.0)                      # 急停持续 40s
    h1 = plant.ems_health.assess(S.ASSEMBLY_ID)
    assert h1["score"] < 100.0, f"急停后评分应下降: {h1['score']}"
    assert h1["faults"] >= 1 and h1["downtime_s"] >= 39.0, \
        f"停机特征提取错误: {h1}"
    assert len(h1["advice"]) >= 4, "必须给出维护建议文案"
    # 人工复位急停后，用连续故障循环压低评分 → 跌破告警阈值(60) 触发告警
    plant.reset_line()
    asm_dev = plant.devices[S.ASSEMBLY_ID]
    for k in range(4):
        asm_dev.apply_fault("气压不足", origin="random")
        plant.clock.run_until(plant.clock.now() + 10.0)
        asm_dev.clear_fault("自检恢复")
        plant.clock.run_until(plant.clock.now() + 10.0)
    h2 = plant.ems_health.assess(S.ASSEMBLY_ID)
    assert h2["faults"] >= 5, f"窗口内应累计≥5次故障: {h2['faults']}"
    assert h2["score"] < S.HEALTH_ALERT_BELOW, \
        f"连续故障应跌破告警阈值: {h2['score']}"
    alerts = plant.bus.replay(lambda e: e["type"] == EventTypes.EMS_HEALTH_ALERT)
    assert len(alerts) >= 1, "跌破告警阈值应发布 ems.health_alert"

    # ---- 断言3：维护预留接口经 REST 命令模式触发（enter_maintenance）----
    rv = plant.execute_command("ems_maintain", {"dev_id": S.VISION_ID})
    assert rv["ok"], f"维护命令失败: {rv}"
    assert plant.devices[S.VISION_ID].state == DeviceState.MAINTENANCE
    rd = plant.execute_command("ems_maintain_done", {"dev_id": S.VISION_ID})
    assert rd["ok"] and plant.devices[S.VISION_ID].state == DeviceState.STANDBY
    maint = plant.bus.replay(lambda e: e["type"] == EventTypes.EMS_MAINTENANCE)
    assert len(maint) >= 2, "维护进入/退出均应落审计事件"
    return (f"60s装配能耗{asm1['kwh']}kWh(理论{expect_asm:.3f}), 总能耗单调增, "
            f"谷档电费{e1['cost_yuan']}元(分档合计一致); "
            f"急停40s后装配评分{h0['devices'][0]['score']}→{h1['score']}"
            f"(停机{h1['downtime_s']}s), 连续故障后→{h2['score']}, "
            f"告警{len(alerts)}条; 维护命令闭环OK (仿真验证值)")


def case_mes_order_lifecycle() -> str:
    """C4 指定数量订单全生命周期：REST 命令开立 50 件工单 → 插单优先投产
    （旧工单零污染）→ 满单自动关单（审计事件）→ 自动翻单开新单；
    增强：同步断言 SQLite 台账落库（orders/qc_log 两表账目一致 +
    /api/mes/qc_log 查询端点全链路）。"""
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    wo_first = plant.mes.orders[0]                    # 开局自动工单（默认计划量）
    from scada.web_server import ScadaWebServer
    srv = ScadaWebServer(plant)
    client = srv.app.test_client()
    rv = client.post("/api/command",
                     json={"cmd": "mes_new_order", "params": {"qty": 50}})
    assert rv.status_code == 200 and rv.get_json()["ok"], \
        f"REST 开单失败: {rv.get_json()}"
    wo50 = next(w for w in plant.mes.orders if w.target_qty == 50)
    created = plant.bus.replay(lambda e: e["type"] == EventTypes.MES_ORDER_CREATED
                               and e["data"].get("wo_id") == wo50.wo_id)
    assert created and created[0]["data"]["target_qty"] == 50, "开单审计事件缺失"
    n_orders_before = len(plant.mes.orders)   # =2（开局自动单 + 手动50件单）

    # 直灌 50 件进视觉待检队列（绕过装配慢节拍），判定→报工走真实编排；
    # 插单语义验证：报工必须全部记到新开的 50 件单，开局大单零污染
    for i in range(50):
        plant.vision.inbound.append(
            Product(f"C4{i:08d}", born_at=plant.clock.now(), source_unit="C4-T"))
    plant.clock.run_until(plant.clock.now() + 260.0)

    assert wo_first.total_count == 0, \
        f"插单期间旧工单不应收到报工: {wo_first.total_count}"
    assert wo50.status == "已完成" and wo50.total_count == 50, \
        f"50件订单未按计划量满单关单: {wo50.to_dict()}"
    closed = plant.bus.replay(lambda e: e["type"] == EventTypes.MES_ORDER_CLOSED
                              and e["data"].get("wo_id") == wo50.wo_id)
    assert closed and closed[0]["data"]["total"] == 50, "满单关单审计事件缺失"
    # 翻单断言加强（评审规格轴#c2）：不能只看"存在 240 件执行中单"——
    # 开局旧单同量且未关单时会平凡通过。必须证明：工单总数恰好 +1、
    # 新单 wo_id 全新、状态执行中、且带 MES_ORDER_CREATED 审计事件。
    assert len(plant.mes.orders) == n_orders_before + 1, \
        (f"满单后应自动翻单开立恰好一张新单: "
         f"{[w.wo_id for w in plant.mes.orders]}")
    wo_new = plant.mes.orders[-1]
    assert wo_new.status == "执行中" \
        and wo_new.target_qty == S.MES_DEFAULT_ORDER_QTY \
        and wo_new.wo_id not in {w.wo_id for w in plant.mes.orders[:n_orders_before]}, \
        f"翻单新单异常: {wo_new.to_dict()}"
    created_new = plant.bus.replay(
        lambda e: e["type"] == EventTypes.MES_ORDER_CREATED
        and e["data"].get("wo_id") == wo_new.wo_id)
    assert created_new, "翻单新工单缺少 CREATED 审计事件"
    rep = plant.mes.report()
    assert rep["judged"] >= 50 and rep["ok"] + rep["ng"] == rep["judged"], \
        f"报工账目不平: {rep}"

    # ---- 断言6（增强）：SQLite 台账落库——orders/qc_log 两表 + /api/mes/qc_log 端点 ----
    led = plant.mes.ledger
    assert led is not None, "默认配置(MES_SQLITE_ENABLE=True)应已装配 SQLite 台账"
    assert os.path.exists(led.db_path), f"台账数据库文件应已生成: {led.db_path}"
    # orders 表：50件单落库行与内存台账逐字段一致（按 run_id 隔离，防跨用例串档）
    orows = {r["wo_id"]: r for r in led.query_orders(run_id=led.run_id)}
    r50 = orows.get(wo50.wo_id)
    assert r50 is not None, f"orders 表缺工单行: {sorted(orows)}"
    assert (r50["status"] == "已完成" and r50["target_qty"] == 50
            and r50["total"] == 50 and r50["ok_count"] == wo50.ok_count
            and r50["ng_count"] == wo50.ng_count
            and r50["closed_at"] is not None), \
        f"orders 落库行与内存台账不一致: {r50} vs {wo50.to_dict()}"
    # qc_log 表：行数=内存报工判定总数；NG 过滤与工单归属过滤口径一致
    n_judged = rep["ok"] + rep["ng"]
    n_rows = led.count_qc(run_id=led.run_id)
    assert n_rows == n_judged, \
        f"qc_log 行数应等于报工判定数: {n_rows} vs {n_judged}"
    assert led.count_qc(run_id=led.run_id, result="NG") == rep["ng"], \
        "qc_log 按 result=NG 过滤计数应等于内存 NG 总数"
    assert led.count_qc(run_id=led.run_id, wo_id=wo50.wo_id) == 50, \
        "50件工单名下的判定流水应为恰好50行"
    # REST 端点全链路：默认列表（最新在前，跨 run 取最新，故显式带 run_id 断言本 run 口径）
    rv_all = client.get(f"/api/mes/qc_log?run_id={led.run_id}")
    j_all = rv_all.get_json()
    assert rv_all.status_code == 200 and j_all["ok"] and j_all["enabled"] \
        and j_all["run_id"] == led.run_id \
        and j_all["count"] == min(n_judged, 50) \
        and all(r["product_id"] for r in j_all["rows"]), \
        f"/api/mes/qc_log 默认查询异常: count={j_all.get('count')}"
    rv_ng = client.get(f"/api/mes/qc_log?result=NG&limit=10&run_id={led.run_id}")
    j_ng = rv_ng.get_json()
    assert rv_ng.status_code == 200 and j_ng["ok"] \
        and len(j_ng["rows"]) == min(rep["ng"], 10) \
        and all(r["result"] == "NG" for r in j_ng["rows"]), \
        f"/api/mes/qc_log NG 过滤异常: {j_ng.get('count')}"
    rv_def = client.get("/api/mes/qc_log?limit=5")   # 无过滤默认路径（跨 run 取最新）
    j_def = rv_def.get_json()
    assert rv_def.status_code == 200 and j_def["ok"] \
        and len(j_def["rows"]) <= 5, "/api/mes/qc_log 默认无参查询异常"

    srv.stop()
    return (f"{wo50.wo_id} 计划50件: REST开单→插单投产(旧单{wo_first.wo_id}报工0)"
            f"→满单{wo50.total_count}件(OK{wo50.ok_count}/NG{wo50.ng_count})自动关单"
            f"→翻单{wo_new.wo_id}(CREATED审计✓); 报表judged={rep['judged']}; "
            f"SQLite落库 orders={len(orows)}单/qc_log={n_rows}行(端点OK) (仿真验证值)")


# ======================================================================
# B1. 系统级冒烟：600 仿真秒加速联跑（与 main.py 同一编排路径，班次2延续）
# ======================================================================
def smoke_full_plant(duration: float = S.DEFAULT_RUN_SECONDS) -> str:
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast",
                  seed=S.DEFAULT_SEED, enable_random_faults=True)
    plant.build()
    plant.start_up_all()
    end = plant.clock.now() + duration
    plant.clock.run_until(end)
    plant.print_status(force=True, final=True)

    a, v, p, w = (plant.assembly.snapshot(), plant.vision.snapshot(),
                  plant.palletizer.snapshot(), plant.warehouse.snapshot())
    inj = plant.injector.snapshot()

    # ---- 不变量1：时钟精确推进 duration ----
    expect_ticks = round(duration / S.SIM_DT)
    assert plant.clock.tick_count == expect_ticks, \
        f"tick数 {plant.clock.tick_count} != {expect_ticks}"

    # ---- 不变量2：装配流出 ≈ 视觉已判 + 在检/排队（容差≤2件在制）----
    wip = len(plant.vision.inbound) + (1 if plant.vision._current else 0)
    judged = v["ok"] + v["ng"]
    assert abs(a["products_out"] - (judged + wip)) <= 2, \
        f"物流不守恒: 流出{a['products_out']} vs 判定{judged}+在制{wip}"
    assert a["products_out"] >= 10, f"10分钟产量过低: {a['products_out']}"

    # ---- 不变量3：OK品 ≈ 码垛已收 + 抓放中/排队（容差≤2件）----
    pal_wip = len(plant.palletizer.inbound) + (1 if plant.palletizer._placing else 0)
    assert abs(v["ok"] - (p["boxes"] + pal_wip)) <= 2, \
        f"OK品物流不守恒: OK{v['ok']} vs 码箱{p['boxes']}+在制{pal_wip}"
    # 返修道 = NG 总数
    assert v["rework_len"] == v["ng"]

    # ---- 不变量4：托盘守恒（班次2修改：改用 Plant.pallet_balance() 权威分解，
    #      口径 = 在库+入库队列+AGV入库在途+出库暂存+AGV出库在途+已出厂）----
    lhs = p["pallets_done"]
    bal = plant.pallet_balance()
    rhs = (bal["stock"] + bal["wh_in_queue"] + bal["agv_inbound"]
           + bal["staging"] + bal["agv_outbound_inflight"] + bal["shipped"])
    assert lhs == rhs, \
        f"托盘守恒失败: 完成托{lhs} != 分解{bal}"

    # ---- 不变量5：事件账目（raised = cleared + 生效中）----
    raised = plant.bus.replay(lambda e: e["type"] == EventTypes.FAULT_RAISED)
    cleared = plant.bus.replay(lambda e: e["type"] == EventTypes.FAULT_CLEARED)
    assert len(raised) == len(cleared) + len(inj["active"]), \
        f"事件账目不平: raised={len(raised)}, cleared={len(cleared)}, active={len(inj['active'])}"

    # ---- 不变量6：脚本故障按时发生（120s 压装压力超限 / 300s 抓取失败）----
    types_raised = {e["data"]["fault_type"] for e in raised}
    assert "压装压力超限" in types_raised and "抓取失败" in types_raised, \
        f"脚本故障未触发: {types_raised}"

    # ---- 不变量7：事件 JSONL 落盘完整 ----
    plant.bus.close()
    with open(plant.bus.log_path, encoding="utf-8") as f:
        disk_lines = sum(1 for _ in f)
    assert disk_lines == plant.bus.total_published, \
        f"落盘{disk_lines}行 != 发布{plant.bus.total_published}条"

    detail = (
        f"{duration:.0f}s联跑: 装配流出{a['products_out']}件 | 视觉OK{v['ok']}/NG{v['ng']}"
        f"(NG率{v['ng_rate']*100:.1f}%) | 码箱{p['boxes']}/完成托{p['pallets_done']} | "
        f"在库{w['stock']}/{w['capacity']} | 故障注入{inj['injected_total']}次"
        f"(事件{plant.bus.total_published}条全部落盘)")
    return detail


# ======================================================================
# 主流程
# ======================================================================
def write_report(smoke_detail: str, report_path: str) -> None:
    """把结果写入 Markdown 风格的文本报告。"""
    passed = sum(1 for r in RESULTS if r[2])
    total = len(RESULTS)
    lines = [
        "=" * 78,
        "Virtual-Smart-Factory 班次3 全厂自检报告（含C组用例 C1~C4）",
        f"生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"环境: Python {sys.version.split()[0]} @ Windows | "
        f"依赖: numpy/flask/pymodbus(标准库+numpy+flask+pymodbus 本班次实际使用)",
        f"配置: dt={S.SIM_DT}s | 默认倍率{S.DEFAULT_SPEED}x | 种子{S.DEFAULT_SEED}"
        f" | AGV车队{S.AGV_COUNT}台 | SCADA:{S.SCADA_HTTP_PORT}/WS:{S.SCADA_WS_PORT}"
        f"/Modbus:{S.MODBUS_TCP_PORT}",
        f"班次3扩展: 视觉算法{'启用' if S.VISION_ALGO_ENABLE else '关闭'}"
        f"({S.VISION_TRAIN_N}样本训练) | MES追溯上限{S.MES_TRACE_MAX}"
        f" | 健康窗口{S.HEALTH_WINDOW_S:.0f}s",
        "=" * 78,
        "",
        "[一] 用例结果",
    ]
    for no, name, ok, detail in RESULTS:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {no} {name}")
        lines.append(f"        └ {detail}")
    lines += ["", "[二] 冒烟测试详情(所有指标为仿真验证值)", f"  {smoke_detail}", "",
              "[三] 结论",
              f"  通过 {passed}/{total} 项" + ("  → 全厂自检通过 ✔" if passed == total else "  → 存在失败项 ✘")]
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Virtual-Smart-Factory 全厂自检")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过600秒冒烟联跑")
    args = parser.parse_args()
    os.makedirs(S.REPORT_DIR, exist_ok=True)

    print("=" * 78)
    print("Virtual-Smart-Factory 班次3 全厂自检开始（A模块 → B Web/AGV/冒烟 → C 算法/MES/EMS）")
    print("=" * 78)

    run_case("A1", "仿真时钟引擎", case_clock)
    run_case("A2", "事件总线(JSONL)", case_bus)
    run_case("A3", "设备基类状态机", case_device)
    run_case("A4", "故障注入器", case_injector)
    run_case("A5", "装配单元顺控/联锁", case_assembly_logic)
    run_case("A6", "视觉质检规则", case_vision_rule)
    run_case("A7", "码垛垛型3×4×4", case_palletizing_pattern)
    run_case("A8", "立体库数据结构", case_warehouse_structs)
    print("\n[A9] 有限料仓：料空冻结于等待上料 → REST 补料续产 → 低水位滞回…")
    run_case("A9", "有限料仓(料空冻结+补料续产)", case_finite_feeder)

    smoke_detail = "（按要求跳过）"
    if not args.skip_smoke:
        print("\n[B2] Web API 冒烟：test_client 全端点 + 命令链路 + Modbus 映射…")
        run_case("B2", "Web API 冒烟(REST/命令/映射)", case_web_api)
        print("\n[B3] AGV 任务闭环：满托 agv.call → 车队搬运 → 入库 → 出库出厂…")
        run_case("B3", "AGV 任务闭环(入库+出库)", case_agv_loop)
        print("\n[B4] AGV 低电量回充排程：单工位互斥 / 任务优先 / 滞回轮换…")
        run_case("B4", "AGV 回充排程(低电触发)", case_agv_recharge)
        # ---- 班次3修改：C 组用例（视觉算法 / MES 追溯 / EMS 合理性 / 订单全生命周期）----
        print("\n[C1] 视觉算法指标：三方 A/B 对照达标 + judge 注入在线混淆矩阵…")
        run_case("C1", "视觉算法指标达标(A/B对照)", case_vision_algo)
        print("\n[C2] MES 追溯闭环：四级反查 + 手动开单 + JSONL 回放重建…")
        run_case("C2", "MES 追溯闭环(产品→托盘→库位)", case_mes_trace)
        print("\n[C3] EMS 合理性：能耗积分单调 + 健康扣分方向 + 维护命令闭环…")
        run_case("C3", "EMS 能耗/健康评分合理性", case_ems_energy_health)
        print("\n[C4] 指定数量订单全生命周期：REST开单50件→插单投产→满单关单→自动翻单…")
        run_case("C4", "指定数量订单全生命周期(50件)", case_mes_order_lifecycle)
        print("\n[B1] 系统级冒烟：fast 加速联跑 600 仿真秒（≈真实产线10分钟）…")
        try:
            smoke_detail = smoke_full_plant()
            record("B1", "600秒全厂加速联跑", True, smoke_detail)
        except AssertionError as exc:
            record("B1", "600秒全厂加速联跑", False, f"不变量被破坏: {exc}")
        except Exception as exc:
            record("B1", "600秒全厂加速联跑", False, f"异常: {exc}")
            traceback.print_exc()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(S.REPORT_DIR, f"selftest_report_{stamp}.txt")
    write_report(smoke_detail, report_path)

    passed = sum(1 for r in RESULTS if r[2])
    total = len(RESULTS)
    print("\n" + "=" * 78)
    verdict = "全厂自检通过 ✔（本报告所有指标均为仿真验证值）" if passed == total \
        else "存在失败项 ✘ —— 请根据上方 FAIL 详情修复后重跑"
    print(f"结论: {passed}/{total} 通过 —— {verdict}")
    print(f"报告已写入: {report_path}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

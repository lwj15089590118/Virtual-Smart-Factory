# -*- coding: utf-8 -*-
"""
selftest.py —— 全厂自检（逐模块自检 + 10 分钟加速联跑冒烟测试）
================================================================
自检内容：
    A. 模块级检查：时钟/总线/设备基类/注入器/装配/视觉/码垛/立体库
       （每项调用公开接口做行为断言，与各模块内置 __main__ 自检互补）；
    B. 系统级冒烟：用与 main.py 完全相同的 Plant 编排，fast 模式加速联跑
       600 仿真秒（=10 分钟），校验端到端物流守恒、事件落盘完整性；
    C. 输出自检报告到 reports/selftest_report_*.txt 并打印结论。

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


# ======================================================================
# B. 系统级冒烟：600 仿真秒加速联跑（与 main.py 同一编排路径）
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

    # ---- 不变量4：托盘守恒（完成托 = 在库 + 库口排队 + AGV在途）----
    lhs = p["pallets_done"]
    rhs = w["stock"] + w["in_queue"] + len(plant._agv_transit)
    assert lhs == rhs, f"托盘守恒失败: 完成托{lhs} != 库{w['stock']}+队{w['in_queue']}+途{len(plant._agv_transit)}"

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
        "Virtual-Smart-Factory 班次1 全厂自检报告",
        f"生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"环境: Python {sys.version.split()[0]} @ Windows | "
        f"依赖: numpy/flask/pymodbus(标准库+numpy 本班次实际使用)",
        f"配置: dt={S.SIM_DT}s | 默认倍率{S.DEFAULT_SPEED}x | 种子{S.DEFAULT_SEED}",
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
    print("Virtual-Smart-Factory 班次1 全厂自检开始（逐模块 → 10分钟加速冒烟）")
    print("=" * 78)

    run_case("A1", "仿真时钟引擎", case_clock)
    run_case("A2", "事件总线(JSONL)", case_bus)
    run_case("A3", "设备基类状态机", case_device)
    run_case("A4", "故障注入器", case_injector)
    run_case("A5", "装配单元顺控/联锁", case_assembly_logic)
    run_case("A6", "视觉质检规则", case_vision_rule)
    run_case("A7", "码垛垛型3×4×4", case_palletizing_pattern)
    run_case("A8", "立体库数据结构", case_warehouse_structs)

    smoke_detail = "（按要求跳过）"
    if not args.skip_smoke:
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

# -*- coding: utf-8 -*-
"""
soak_run.py —— 全厂长程稳定性压测（加速挂机 + 内存采样 CSV + 自动报告）
==========================================================================
目的：
    用加速批量模式连续运行多"天"的仿真时长，证明三件事并产出可写进
    作品集/简历的稳定性数据：
        1. 各防膨胀机制到顶后保持平稳（质检记录环/AGV完成档裁剪/追溯索引上限）；
        2. 进程内存无失控增长（工作集曲线 + 斜率外推）；
        3. 长跑结束后物料守恒与故障账目依然分毫不差。

设计要点：
    1. 与 main.py 完全同一编排路径（Plant fast 模式），随机故障开启、脚本故障
       照常触发；唯一的 soak 专用差异是开启料仓自动补料——否则初始 60 件毛坯
       耗尽后产线会按设计冻结在"等待上料"（那是功能正确性用例 A9 的剧情，
       不是压测想要的）。这等价于现场三班倒的定期补料作业，不改默认配置文件；
    2. 分块推进：每个采样间隔调一次 run_until，块间采样——tick 序列与一次性
       推进完全一致，确定性不受影响；
    3. 内存采样走 ctypes GetProcessMemoryInfo（Windows 自带，不引入 psutil）；
       墙钟只用于采样节拍与吞吐统计，绝不进入任何仿真状态计算（时间纪律不变）；
    4. 报告落 reports/soak_report_<tag>.txt，逐拍指标落 logs/soak_metrics_<tag>.csv
       （两目录均已被 .gitignore 忽略）。

用法：
    python soak_run.py                          # 默认 7 个仿真日，每 30 仿真分钟一拍
    python soak_run.py --days 3                 # 3 个仿真日
    python soak_run.py --sim-hours 0.5 --sample-min 10 --tag calib   # 快速标定吞吐
"""

import argparse
import csv
import ctypes
import os
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings as S
from core.event_bus import EventTypes
from main import Plant


# ----------------------------------------------------------------------
# 内存采样（Windows 工作集；不引入第三方依赖）
# ----------------------------------------------------------------------
_PMC_FIELDS = [
    ("cb", ctypes.c_uint32),
    ("PageFaultCount", ctypes.c_uint32),
    ("PeakWorkingSetSize", ctypes.c_size_t),
    ("WorkingSetSize", ctypes.c_size_t),
    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
    ("QuotaPagedPoolUsage", ctypes.c_size_t),
    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
    ("PagefileUsage", ctypes.c_size_t),
    ("PeakPagefileUsage", ctypes.c_size_t),
]


class _PMC(ctypes.Structure):
    """完整 PROCESS_MEMORY_COUNTERS（模块级唯一定义：argtypes 与实例必须同类）。"""
    _fields_ = _PMC_FIELDS

_psapi_ready = False


def _init_psapi():
    """一次性声明 GetProcessMemoryInfo 的完整原型。

    两个必须踩对的坑（否则 API 静默失败返回 0）：
      ① GetCurrentProcess 必须显式 restype=HANDLE——默认按 32 位 int 返回
         伪句柄 -1，x64 下高位不符号扩展，传给 API 即 ERROR_INVALID_HANDLE(6)；
      ② 结构体必须是完整的 PROCESS_MEMORY_COUNTERS（10 字段），cb 过小同样失败。
    """
    global _psapi_ready
    if os.name != "nt":
        return
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    k32.GetCurrentProcess.restype = wintypes.HANDLE          # 坑①
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _psapi_ready = True


def _rss_now():
    """返回 (当前工作集字节, 峰值工作集字节)；非 Windows 或调用失败返回 (None, None)。"""
    if not _psapi_ready:
        try:
            import resource
            peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return None, int(peak_kb) * 1024
        except Exception:
            return None, None
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)                              # 坑②：完整结构体尺寸
    h = ctypes.windll.kernel32.GetCurrentProcess()
    if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
        return int(pmc.WorkingSetSize), int(pmc.PeakWorkingSetSize)
    return None, None


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Virtual-Smart-Factory 长程稳定性压测")
    p.add_argument("--days", type=float, default=7.0,
                   help="仿真时长（天），默认 7 天")
    p.add_argument("--sim-hours", type=float, default=None,
                   help="以小时直接指定仿真时长（覆盖 --days，用于快速标定）")
    p.add_argument("--sample-min", type=float, default=30.0,
                   help="采样间隔（仿真分钟），默认 30 分钟")
    p.add_argument("--tag", type=str, default=None,
                   help="输出文件标签（缺省用启动时间戳）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tag = args.tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    sim_seconds = (args.sim_hours * 3600.0) if args.sim_hours else args.days * 86400.0
    chunk_s = max(args.sample_min * 60.0, 1.0)

    csv_path = os.path.join(S.LOG_DIR, f"soak_metrics_{tag}.csv")
    report_path = os.path.join(S.REPORT_DIR, f"soak_report_{tag}.txt")

    print("=" * 78)
    print(f"[SOAK] 启动长程压测：仿真时长 {sim_seconds:.0f}s（≈{sim_seconds/86400:.2f} 天），"
          f"采样间隔 {args.sample_min:.0f} 仿真分钟")
    print(f"[SOAK] 种子={S.DEFAULT_SEED} 随机故障=开 料仓自动补料=开(soak专用) dt={S.SIM_DT}s")
    print("=" * 78)

    # ---- 组装产线（与 main.py 同一路径；soak 专用：自动补料，等价三班倒补料作业）----
    _init_psapi()
    S.FEEDER_AUTO_REFILL = True
    plant = Plant(speed=S.DEFAULT_SPEED, mode="fast",
                  seed=S.DEFAULT_SEED, enable_random_faults=True)
    fault_tally = {"raised": 0, "cleared": 0}
    plant.bus.subscribe(EventTypes.FAULT_RAISED,
                        lambda e: fault_tally.__setitem__("raised", fault_tally["raised"] + 1))
    plant.bus.subscribe(EventTypes.FAULT_CLEARED,
                        lambda e: fault_tally.__setitem__("cleared", fault_tally["cleared"] + 1))
    plant.build()
    plant.start_up_all()

    maxima = {"rss": 0.0, "qc_len": 0, "agv_finished": 0,
              "trace_keys": 0, "orders": 0, "batches": 0}

    def sample(t0_wall: float):
        """采集一个数据点：写 CSV 行、更新极值、打印心跳。"""
        import gc
        gc.collect()                                   # 采样前回收，让 RSS 反映存活对象
        sim = plant.clock.now()
        wall = time.perf_counter() - t0_wall
        rss, peak = _rss_now()
        rss_mb = (rss / 1048576.0) if rss is not None else -1.0
        v = plant.vision.snapshot()
        pal_n = len(plant.palletizer.pallets_done)
        w = plant.warehouse.snapshot()
        fl = plant.agv_fleet.snapshot()
        fin_n = len(plant.agv_fleet.finished)
        mes = plant.mes
        trace_keys = len(mes.index.product_pallet)
        qc_rows = (mes.ledger.count_qc(run_id=mes.ledger.run_id)
                   if getattr(mes, "ledger", None) is not None else -1)
        row = {
            "ts_wall": datetime.now().isoformat(timespec="seconds"),
            "wall_s": round(wall, 1), "sim_s": round(sim, 1),
            "ticks": plant.clock.tick_count,
            "rss_mb": round(rss_mb, 1),
            "peak_rss_mb": round((peak / 1048576.0) if peak else -1.0, 1),
            "events_total": plant.bus.total_published,
            "faults_raised": fault_tally["raised"],
            "faults_cleared": fault_tally["cleared"],
            "products_out": plant.assembly.products_out_total,
            "ok": v["ok"], "ng": v["ng"],
            "vision_qc_len": len(plant.vision.qc_records),
            "pallets_done_list": pal_n, "boxes_total": plant.palletizer.boxes_total,
            "stock": w["stock"], "inbound_done": w["inbound_done"],
            "outbound_done": w["outbound_done"],
            "agv_pending": fl["pending"], "agv_active": fl["active"],
            "agv_finished_list": fin_n, "shipped": fl["shipped"],
            "mes_orders": len(mes.orders),
            "mes_orders_closed": sum(1 for o in mes.orders if o.status == "已完成"),
            "mes_batches": len(mes.batches),
            "trace_keys": trace_keys, "trace_overflow": mes.index.overflow,
            "sqlite_qc_rows": qc_rows,
        }
        writer.writerow(row)
        fh.flush()
        for key, val in (("rss", rss_mb), ("qc_len", row["vision_qc_len"]),
                         ("agv_finished", fin_n), ("trace_keys", trace_keys),
                         ("orders", row["mes_orders"]), ("batches", row["mes_batches"])):
            maxima[key] = max(maxima[key], val)
        print(f"[SOAK] sim {sim:>9.0f}s ({sim/86400:>4.2f}天) | wall {wall:>6.0f}s | "
              f"rss {rss_mb:>6.1f}MB | 事件 {row['events_total']:>7} | "
              f"流出 {row['products_out']:>5} | 托档 {pal_n:>4} | "
              f"追溯 {trace_keys:>6}(溢出{row['trace_overflow']}) | "
              f"DB行 {qc_rows:>6}", flush=True)
        return row

    os.makedirs(S.LOG_DIR, exist_ok=True)
    os.makedirs(S.REPORT_DIR, exist_ok=True)
    fh = open(csv_path, "w", newline="", encoding="utf-8")
    fieldnames = ["ts_wall", "wall_s", "sim_s", "ticks", "rss_mb", "peak_rss_mb",
                  "events_total", "faults_raised", "faults_cleared", "products_out",
                  "ok", "ng", "vision_qc_len", "pallets_done_list", "boxes_total",
                  "stock", "inbound_done", "outbound_done", "agv_pending",
                  "agv_active", "agv_finished_list", "shipped", "mes_orders",
                  "mes_orders_closed", "mes_batches", "trace_keys",
                  "trace_overflow", "sqlite_qc_rows"]
    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    writer.writeheader()

    t0 = time.perf_counter()
    first = sample(t0)
    aborted = False
    try:
        end_sim = first["sim_s"] + sim_seconds
        while True:
            target = min(plant.clock.now() + chunk_s, end_sim)
            plant.clock.run_until(target)
            row = sample(t0)
            if plant.clock.now() >= end_sim - 1e-9:
                break
    except KeyboardInterrupt:
        aborted = True
        print("\n[SOAK] 收到中断，按已完成的进度出报告…")
    finally:
        wall_total = time.perf_counter() - t0
        sim_total = plant.clock.now()
        # ---- 收尾：终报（含 MES/EMS 摘要）并关闭总线/台账 ----
        plant.print_status(force=True, final=True)
        plant.shutdown()

        # ---- 守恒与账目核验 ----
        # 修复记录：守恒左端不能用 len(palletizer.pallets_done)——环形化后它只
        # 计"最近100托"；改用 MES 事件驱动的累计满托数 stat_pallets_done
        # （与 JSONL 回放口径一致，C2 用例已验证）。
        checks = []
        bal = plant.pallet_balance()
        lhs = plant.mes.stat_pallets_done if plant.mes else \
            len(plant.palletizer.pallets_done)
        cons_ok = lhs == sum(bal.values())
        checks.append(("托盘守恒 累计完成托=分解之和", cons_ok,
                       f"{lhs} vs {sum(bal.values())} {bal}"))
        inj = plant.injector.snapshot()
        acc_ok = fault_tally["raised"] == fault_tally["cleared"] + len(inj["active"])
        checks.append(("故障账目 raised=cleared+active", acc_ok,
                       f"{fault_tally['raised']}={fault_tally['cleared']}+{len(inj['active'])}"))
        cap_ok = (maxima["qc_len"] <= S.VISION_RECORD_LIMIT
                  and maxima["agv_finished"] <= 500
                  and maxima["trace_keys"] <= S.MES_TRACE_MAX)
        checks.append(("容量上限未被击穿(qc/AGV档/追溯)", cap_ok,
                       f"qc≤{S.VISION_RECORD_LIMIT}:{maxima['qc_len']} | "
                       f"finished≤500:{maxima['agv_finished']} | "
                       f"trace≤{S.MES_TRACE_MAX}:{maxima['trace_keys']}"))
        rss_end = sample(t0)["rss_mb"]                    # 关停后再采一点作收尾值
        rss_available = (first["rss_mb"] > 0 and rss_end > 0)
        growth = (rss_end - first["rss_mb"]) if rss_available else -1.0
        mem_ok = rss_available and (growth < 250.0)
        checks.append(("内存净增长 <250MB（同机相对判据）", mem_ok,
                       (f"{first['rss_mb']:.1f} → {rss_end:.1f} MB（+{growth:+.1f}）")
                       if rss_available else "本平台无法采样工作集，跳过该项"))
        days = sim_total / 86400.0
        slope = (growth / days) if days > 0 else 0.0

        jsonl_mb = (os.path.getsize(plant.bus.log_path) / 1048576.0
                    if plant.bus.log_path and os.path.exists(plant.bus.log_path) else -1.0)
        db_base = S.MES_DB_PATH
        db_mb = sum(os.path.getsize(db_base + ext) / 1048576.0
                    for ext in ("", "-wal", "-shm")
                    if os.path.exists(db_base + ext))

        rep = plant.mes.report() if plant.mes else {}
        lines = [
            "=" * 78,
            "Virtual-Smart-Factory 长程稳定性压测报告（soak）",
            f"生成时间: {datetime.now().isoformat(timespec='seconds')}  标签: {tag}"
            + ("  [中断截断]" if aborted else ""),
            f"配置: 仿真时长 {sim_total:.0f}s ≈ {days:.2f} 天 | 采样 {args.sample_min:.0f} 仿真分钟/拍"
            f" | 种子 {S.DEFAULT_SEED} | dt {S.SIM_DT}s | 随机故障开 | 料仓自动补料开(soak)",
            "-" * 78,
            "[一] 吞吐与时钟",
            f"  墙钟耗时 {wall_total:.0f}s | tick 数 {plant.clock.tick_count}",
            f"  吞吐 ≈ {plant.clock.tick_count / max(wall_total, 1e-9):.0f} tick/s"
            f"（1 墙钟秒 ≈ {(sim_total / max(wall_total, 1e-9)):.0f} 仿真秒）",
            "-" * 78,
            "[二] 内存曲线（详见 CSV）",
            f"  起始 RSS {first['rss_mb']:.1f}MB → 收尾 {rss_end:.1f}MB"
            f" | 过程峰值 {maxima['rss']:.1f}MB | 净增 {growth:+.1f}MB"
            f" | 斜率 ≈ {slope:+.1f} MB/仿真日",
            "-" * 78,
            "[三] 防膨胀机制水位（过程最大值 vs 上限）",
            f"  质检记录环 ≤{S.VISION_RECORD_LIMIT}: {maxima['qc_len']}",
            f"  AGV 完成档 ≤500(裁剪至400+): {maxima['agv_finished']}",
            f"  追溯索引 ≤{S.MES_TRACE_MAX}: {maxima['trace_keys']}"
            f"（超限溢出丢弃 {plant.mes.index.overflow if plant.mes else 0} 键属预期保护）",
            f"  工单数(自动翻单累积) {maxima['orders']} | 批次 {maxima['batches']}",
            "-" * 78,
            "[四] 生产与设备台账（均为仿真验证值）",
            f"  流出 {row['products_out']} 件 | 判定 OK{rep.get('ok', 0)}/NG{rep.get('ng', 0)}"
            f"（NG率 {plant.vision.ng_rate()*100:.1f}%）| 满托 {len(plant.palletizer.pallets_done)} 托",
            f"  入库 {row['inbound_done']} / 出库 {row['outbound_done']} / 出厂 {row['shipped']} 托"
            f" | 在库 {bal['stock']} | 故障注入 {fault_tally['raised']} 次",
            f"  MES: 工单 {row['mes_orders']} 张（关单 {row['mes_orders_closed']}）| "
            f"OEE≈{rep.get('oee_pct', '-')}%",
            f"  落盘: 事件 JSONL {jsonl_mb:.1f}MB（{plant.bus.total_published} 条）"
            f" | SQLite 台账 {db_mb:.2f}MB（含 wal/shm）",
            "-" * 78,
            "[五] 校验结论",
        ]
        all_ok = True
        for name, ok, detail in checks:
            all_ok &= bool(ok)
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name} —— {detail}")
        verdict = "长跑稳定性压测通过 ✔（本报告所有指标均为仿真验证值）" if all_ok \
            else "存在失败项 ✘ —— 请结合 CSV 曲线定位"
        lines += ["", f"  结论: {'全部通过' if all_ok else '存在失败'} —— {verdict}",
                  "=" * 78]
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write("\n".join(lines) + "\n")
        fh.close()
        print("\n".join(lines))
        print(f"[SOAK] 指标CSV: {csv_path}\n[SOAK] 报告: {report_path}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

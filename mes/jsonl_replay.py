# -*- coding: utf-8 -*-
"""
mes/jsonl_replay.py —— 事件总线 JSONL 回放器（班次3新增）
==========================================================
职责（交付范围2 的"数据源=事件总线 JSONL 回放"落地点）：
    1. load_events(path)：逐行读取 logs/events_*.jsonl 并反序列化；
    2. replay_file(path)：把历史事件流灌入一台【离线 MES 引擎】
       （plant=None 模式，不订阅不发布），重建完整工单/追溯台账；
    3. 命令行入口：python mes/jsonl_replay.py [jsonl路径]
       缺省自动取 logs/ 下最新一份事件文件，打印报工报表与订单摘要，
       输出可直接作为作品集"离线数据分析"演示素材。

时间纪律：
    只读文件与事件内 ts_sim，不接触墙钟（打印生成时刻除外）。

假设记录：
    - JSONL 按事件 seq 天然有序，直接顺序回放即可等价重建在线台账。
"""

import glob
import json
import os
import sys
# 路径引导：直接运行本文件(python mes/jsonl_replay.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Iterator

from config import settings as S
from mes.mes_engine import MESEngine


def latest_log(log_dir: str = None) -> str:
    """取 logs/ 下最新的事件 JSONL 文件路径；找不到则抛 FileNotFoundError。"""
    log_dir = log_dir or S.LOG_DIR
    files = sorted(glob.glob(os.path.join(log_dir, "events_*.jsonl")))
    if not files:
        raise FileNotFoundError(f"{log_dir} 下没有 events_*.jsonl 事件文件")
    return files[-1]


def load_events(path: str) -> Iterator[dict]:
    """逐行读取事件文件（跳过空行/坏行，坏行计数告警）。"""
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print(f"[jsonl_replay] 警告: {bad} 行无法解析已跳过")


def replay_file(path: str) -> MESEngine:
    """把一份 JSONL 事件文件回放进离线 MES 引擎，返回重建后的引擎。"""
    eng = MESEngine(None)                 # 离线模式：无 bus/clock
    for ev in load_events(path):
        eng.ingest(ev)
    return eng


def format_summary(eng: MESEngine, path: str) -> str:
    """回放结果摘要文本（控制台/报告通用）。"""
    rep = eng.report()
    lines = [
        "=" * 70,
        f"MES 离线回放报告 —— 数据源: {path}",
        f"事件窗口: t=0 ~ {rep['window_s']}s（仿真验证值）",
        "-" * 70,
        f"装配流出 {rep['products_out']} 件 | 报工判定 {rep['judged']} 件"
        f"（OK {rep['ok']} / NG {rep['ng']}）",
        f"良品率 {rep['quality_pct']}% | 可用率 {rep['availability_pct']}% | "
        f"性能率 {rep['performance_pct']}% | OEE≈{rep['oee_pct']}%",
        f"满托 {rep['pallets_done']} 托 | 已出厂 {rep['shipped']} 托",
        "-" * 70,
        "工单台账:",
    ]
    for wo in eng.snapshot_orders():
        lines.append(f"  {wo['wo_id']} [{wo['status']}] 型号={wo['model']} "
                     f"进度 {wo['total']}/{wo['target_qty']}"
                     f"（OK{wo['ok']}/NG{wo['ng']}）良率{wo['yield_pct']}%")
    tr_count = len(eng.index.pallet_products)
    lines += ["-" * 70,
              f"追溯索引: 托盘 {tr_count} 个 / 产品 {len(eng.index.product_pallet)} 件"
              + (f"（索引溢出丢弃 {eng.index.overflow} 键）" if eng.index.overflow else "")]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    path = sys.argv[1] if len(sys.argv) > 1 else latest_log()
    engine = replay_file(path)
    print(format_summary(engine, path))
    # 自检断言：回放必须至少产出一张工单台账（否则数据源异常）
    assert len(engine.orders) >= 1, "回放后应至少有 1 张工单"
    print("[jsonl_replay 自检通过] 离线回放重建 MES 台账成功 (仿真验证值)")

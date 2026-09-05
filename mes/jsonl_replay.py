# -*- coding: utf-8 -*-
"""
mes/jsonl_replay.py —— 事件总线 JSONL 回放器（阶段3新增）
==========================================================
职责（交付范围2 的"数据源=事件总线 JSONL 回放"落地点）：
    1. run_segments(active_path)：按 run 收集**全部**事件段——活动文件 +
       轮转历史段（.1 ~ .N），按事件发生顺序（旧→新）返回；
    2. load_events(path)：逐行反序列化，行数/坏行计数进 stats 出参供对账；
    3. replay_file(path)：把一个 run 的全部事件段灌入一台【离线 MES 引擎】
       （plant=None 模式，不订阅不发布），重建完整工单/追溯台账，并给出
       "回放总数 vs 各段行数合计"的对账统计；
    4. 命令行入口：python mes/jsonl_replay.py [jsonl路径]
       缺省自动取 logs/ 下最新一份事件文件（自动携带其全部轮转历史段），
       打印报工报表与回放对账结果，输出可直接作为作品集"离线数据分析"
       演示素材。

时间纪律：
    只读文件与事件内 ts_sim，不接触墙钟（打印生成时刻除外）。

假设记录：
    - JSONL 按事件 seq 天然有序；同 run 内轮转段 .N 数字越大越旧
      （EventBus 链式改名 .1→.2→.3），按 ".N 降序 + 活动文件收尾" 顺序
      回放即等价重建在线台账（C2"回放=在线"口径，跨轮转也成立）。
    - 第四轮修补（复审报告13-P3-4）：EventBus 50MB×3 份轮转后，此前
      latest_log 只回放活动文件会静默丢掉最多 3 份历史段——段缺失/损坏
      一律显著告警并计入对账差异，绝不允许静默跳过。
"""

import glob
import json
import os
import re
import sys
# 路径引导：直接运行本文件(python mes/jsonl_replay.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Iterator, List, Optional

from config import settings as S
from mes.mes_engine import MESEngine


def latest_log(log_dir: str = None) -> str:
    """取 logs/ 下最新一份事件 JSONL（某 run 的活动文件）路径。

    注意：返回的是活动文件；轮转历史段由 replay_file/run_segments 自动补齐，
    调用方无需（也不应）自己拼段。找不到则抛 FileNotFoundError。
    """
    log_dir = log_dir or S.LOG_DIR
    files = sorted(glob.glob(os.path.join(log_dir, "events_*.jsonl")))
    if not files:
        raise FileNotFoundError(f"{log_dir} 下没有 events_*.jsonl 事件文件")
    return files[-1]


def run_segments(active_path: str) -> List[str]:
    """收集一个 run 的全部事件段（活动文件 + 轮转历史段），旧→新排序。

    第四轮修补（复审报告13-P3-4）：历史段是 `<活动文件>.1 ~ .N`，数字越大
    越旧；轮转为链式改名，段号应连续——发现缺口（如 .2 缺失）打印"严重告警"
    （回放结果不完整，消费方必须知情），绝不静默跳过。活动文件缺失分两种：
    有 .1 段=轮转收尾态（内容已整体轮转入 .1，无丢失，仅提示）；无 .1 段=
    活动文件内容疑似整体丢失，严重告警。

    :param active_path: 该 run 的活动文件路径（EventBus.log_path 同口径）
    :return: 按事件发生顺序（最旧段在前、活动文件收尾）的文件路径列表
    """
    seg_map = {}
    for p in glob.glob(active_path + ".*"):
        m = re.fullmatch(re.escape(active_path) + r"\.(\d+)", p)
        if m:
            seg_map[int(m.group(1))] = p
    active_exists = os.path.exists(active_path)
    if not seg_map and not active_exists:
        raise FileNotFoundError(f"{active_path} 不存在（也无轮转历史段）")
    segments = [seg_map[i] for i in sorted(seg_map, reverse=True)]   # 旧→新
    for i in range(1, (max(seg_map) + 1) if seg_map else 1):
        if i not in seg_map:
            print(f"[jsonl_replay] !!严重告警: 轮转历史段 {active_path}.{i} 缺失"
                  f"（被删除/轮转改名失败?）——回放将缺少该段事件，结果不完整！")
    if active_exists:
        segments.append(active_path)
    elif 1 in seg_map:
        # 轮转收尾态：最后一条事件恰好触发轮转，活动文件已改名 .1、新活动文件
        # 尚未生成——内容全部在留存段里，一条不丢。属正常状态，提示但不告警
        # （若活动文件被外部删除且之后再无写入，与本态不可区分，由对账行数兜底，
        #   消费方可结合 run 结束时间核查）。
        print(f"[jsonl_replay] 注意: 活动文件 {active_path} 不存在"
              f"（应为轮转收尾态：内容已整体轮转入 .1），本次回放全部 "
              f"{len(segments)} 份留存段")
    else:
        print(f"[jsonl_replay] !!严重告警: 活动文件 {active_path} 缺失且无 .1 历史段，"
              f"仅能回放 {len(segments)} 份更旧历史段——活动文件内容疑似整体丢失，"
              f"结果不完整！")
    return segments


def load_events(path: str, stats: Optional[dict] = None) -> Iterator[dict]:
    """逐行读取事件文件并反序列化（生成器）。

    行数与坏行数累计进 stats 出参（供回放对账）。损坏行无法回放只能跳过，
    但必须显著告警并体现在对账差异里——不允许静默丢失（第四轮修补）。
    """
    bad = 0
    n_lines = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print(f"[jsonl_replay] !!严重告警: {path} 有 {bad} 行损坏无法解析"
              f"（已跳过并计入对账差异）——事件段可能被截断/写坏！")
    if stats is not None:
        stats.setdefault("per_segment", []).append(
            {"path": path, "lines": n_lines, "bad": bad})
        stats["total_lines"] = stats.get("total_lines", 0) + n_lines
        stats["total_bad"] = stats.get("total_bad", 0) + bad


def replay_file(path: str, stats: Optional[dict] = None) -> MESEngine:
    """把一个 run 的**全部事件段**（活动文件+轮转历史段）回放进离线 MES 引擎。

    第四轮修补（复审报告13-P3-4）：此前只回放活动文件，50MB×3 轮转发生后
    最多 3 份历史段（~150MB）被静默丢弃，C2"回放=在线"在跨轮转场景失效——
    现按 run_segments() 的旧→新顺序回放全段。对账统计写入 stats 出参：
        segments     本次回放覆盖的事件段数
        total_lines  各段行数合计
        replayed     实际灌入引擎的事件数
        total_bad    损坏跳过行数
        consistent   对账结果：replayed + total_bad == total_lines（且无坏行）
    """
    eng = MESEngine(None)                 # 离线模式：无 bus/clock
    segments = run_segments(path)
    replayed = 0
    for seg in segments:
        for ev in load_events(seg, stats):
            eng.ingest(ev)
            replayed += 1
    if stats is not None:
        stats["segments"] = len(segments)
        stats["replayed"] = replayed
        stats["consistent"] = (replayed + stats.get("total_bad", 0)
                               == stats.get("total_lines", 0)
                               and stats.get("total_bad", 0) == 0)
    return eng


def format_summary(eng: MESEngine, path: str,
                   stats: Optional[dict] = None) -> str:
    """回放结果摘要文本（控制台/报告通用）；传入 stats 时附回放对账表。"""
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
    if stats is not None:
        # 第四轮修补（报告13-P3-4）：回放对账——回放总数 vs 各段行数合计
        verdict = "一致" if stats.get("consistent") else "不一致!!"
        lines += ["-" * 70,
                  f"回放对账: 事件段 {stats.get('segments', 0)} 个 | "
                  f"各段行数合计 {stats.get('total_lines', 0)} | "
                  f"实际回放 {stats.get('replayed', 0)} | "
                  f"损坏跳过 {stats.get('total_bad', 0)} —— [{verdict}]"]
        for seg in stats.get("per_segment", []):
            lines.append(f"    {seg['path']}: {seg['lines']} 行"
                         + (f"（坏行 {seg['bad']}）" if seg["bad"] else ""))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    path = sys.argv[1] if len(sys.argv) > 1 else latest_log()
    stats: dict = {}
    engine = replay_file(path, stats)
    print(format_summary(engine, path, stats))
    # 自检断言：回放必须至少产出一张工单台账（否则数据源异常）
    assert len(engine.orders) >= 1, "回放后应至少有 1 张工单"
    # 第四轮修补（报告13-P3-4）：对账断言——回放总数必须与各段行数合计吻合，
    # 段缺失/坏行已被 run_segments/load_events 显著告警，这里兜底不许带病通过
    assert stats["consistent"], f"回放对账不一致: {stats}"
    print("[jsonl_replay 自检通过] 离线回放重建 MES 台账成功 (仿真验证值)")

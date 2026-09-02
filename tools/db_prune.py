# -*- coding: utf-8 -*-
"""
tools/db_prune.py —— mes.db 保留策略工具（第四轮修补，复审报告13-P3残留项）
============================================================================
背景：
    mes.db（settings.MES_DB_PATH，默认 logs/mes.db）以 (run_id, wo_id) 联合
    主键按运行批次累积——跨运行只增不减，30 日 soak 单次即 ~96MB，演示机
    磁盘会被历史运行慢慢吃满。本工具提供"改动最小"的轻量保留策略：
    按 run_id（墙钟命名 YYYYMMDD_HHMMSS_xxxxxx，字典序=时间序）从最旧
    批次开始整批删除（qc_log 流水 + orders 档案同 run 一起删），把活数据
    压到 --max-mb 以内；至少保留最新一个 run；收尾 VACUUM 归还磁盘。

用法：
    python tools/db_prune.py                          # 默认 logs/mes.db，上限 200MB
    python tools/db_prune.py --max-mb 100 --dry-run   # 只报告清理计划，不写库
    python tools/db_prune.py --db path/to/mes.db --max-mb 50

设计要点：
    1. 零 schema 侵入：只 DELETE mes/sqlite_ledger.py 既有两表（orders/qc_log），
       标准库 sqlite3，无第三方依赖；
    2. 分批 = 逐 run 一个事务即时 commit，中途 Ctrl+C 不留"半删 run"；
    3. 进度判定用 PRAGMA page_count/freelist_count 计算活数据字节
       （页级口径，删除后 freelist 增长即实时反映），不依赖 VACUUM 中间态；
    4. 每删一个 run 打印一行审计日志（run_id/两表行数），可追溯；
    5. 最新 run 永不删除（可能正是当前运行在写的批次）；只剩一个 run 仍超限
       时明确告警，让用户提高上限或手动归档，绝不越权清空。
"""

import argparse
import os
import sqlite3
import sys

# 路径引导：直接运行本文件时把项目根加入 sys.path（与 mes/ 子模块同约定）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_MAX_MB = 200.0


def db_size_mb(db_path: str) -> float:
    """库文件 + wal/shm 侧车文件的总大小（MB）——与 soak 终报同口径。"""
    return sum(os.path.getsize(db_path + ext) / 1048576.0
               for ext in ("", "-wal", "-shm") if os.path.exists(db_path + ext))


def _live_mb(conn: sqlite3.Connection) -> float:
    """活数据字节（MB）：(总页数-空闲页数)×页大小。删除后空闲页实时增长，
    无需 VACUUM 即可反映真实数据量（VACUUM 只在收尾执行一次归还磁盘）。"""
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return (page_count - freelist) * page_size / 1048576.0


def _runs_oldest_first(conn: sqlite3.Connection) -> list:
    """全部 run_id 按字典序升序（= 墙钟时间序，最旧在前）。"""
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM qc_log "
        "UNION SELECT DISTINCT run_id FROM orders ORDER BY run_id ASC").fetchall()
    return [r[0] for r in rows]


def _run_row_counts(conn: sqlite3.Connection, run_id: str) -> tuple:
    qc = conn.execute("SELECT COUNT(*) FROM qc_log WHERE run_id=?",
                      (run_id,)).fetchone()[0]
    od = conn.execute("SELECT COUNT(*) FROM orders WHERE run_id=?",
                      (run_id,)).fetchone()[0]
    return qc, od


def prune_db(db_path: str, max_mb: float = DEFAULT_MAX_MB,
             dry_run: bool = False) -> dict:
    """把 mes.db 的活数据清理到 max_mb 以内（按最旧 run 整批删除）。

    :param db_path: SQLite 文件路径（不存在则抛 FileNotFoundError）
    :param max_mb:  活数据上限（MB），必须 >0
    :param dry_run: True 时只打印清理计划，不做任何写操作
    :return: 统计 dict（size_before_mb/size_after_mb/runs_deleted/
             qc_rows_deleted/orders_deleted/dry_run）
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"{db_path} 不存在（尚未生成 MES 台账?）")
    if max_mb <= 0:
        raise ValueError("--max-mb 必须为正数")

    result = {"size_before_mb": db_size_mb(db_path), "runs_deleted": 0,
              "qc_rows_deleted": 0, "orders_deleted": 0, "dry_run": dry_run}
    print(f"[db_prune] 库文件 {db_path}（含 wal/shm）当前 "
          f"{result['size_before_mb']:.2f}MB，保留上限 {max_mb}MB")

    conn = sqlite3.connect(db_path)
    try:
        runs = _runs_oldest_first(conn)
        if not runs:
            print("[db_prune] 空库（两表均无数据），无需清理")
            result["size_after_mb"] = result["size_before_mb"]
            return result
        live = _live_mb(conn)
        # 最新 run 永不删除（可能正被当前运行写入）；其余按最旧优先逐 run 清理
        for run_id in runs[:-1]:
            if live <= max_mb:
                break
            qc, od = _run_row_counts(conn, run_id)
            if dry_run:
                print(f"[db_prune] [dry-run] 将删除 run {run_id}: "
                      f"qc_log {qc} 行 + orders {od} 行")
                result["runs_deleted"] += 1
                result["qc_rows_deleted"] += qc
                result["orders_deleted"] += od
                continue
            with conn:                            # 单 run 一个事务，不留半删态
                cur_q = conn.execute("DELETE FROM qc_log WHERE run_id=?", (run_id,))
                cur_o = conn.execute("DELETE FROM orders WHERE run_id=?", (run_id,))
            qc, od = cur_q.rowcount, cur_o.rowcount
            result["runs_deleted"] += 1
            result["qc_rows_deleted"] += max(qc, 0)
            result["orders_deleted"] += max(od, 0)
            live = _live_mb(conn)
            print(f"[db_prune] 已删除 run {run_id}: qc_log {qc} 行 + "
                  f"orders {od} 行 → 活数据 {live:.2f}MB")
        if live > max_mb:
            print(f"[db_prune] !!告警: 保留最新 run {runs[-1]} 后活数据仍 "
                  f"{live:.2f}MB > 上限 {max_mb}MB——最新批次不予删除，"
                  f"请提高 --max-mb 或手动归档")
        elif result["runs_deleted"] == 0:
            print(f"[db_prune] 活数据 {live:.2f}MB 已在上限内，无需清理")
        if not dry_run and result["runs_deleted"] > 0:
            print("[db_prune] 执行 VACUUM 归还磁盘空间…")
            conn.execute("VACUUM")
    finally:
        conn.close()
    result["size_after_mb"] = db_size_mb(db_path)
    tag = "[dry-run] " if dry_run else ""
    print(f"[db_prune] {tag}完成: 删除 {result['runs_deleted']} 个 run"
          f"（qc_log {result['qc_rows_deleted']} 行 / orders "
          f"{result['orders_deleted']} 行），"
          f"{result['size_before_mb']:.2f}MB → {result['size_after_mb']:.2f}MB")
    return result


# ----------------------------------------------------------------------
# 命令行入口 + 自检（构造 3 个 run 的小库验证"最旧优先+保最新+VACUUM"）
# ----------------------------------------------------------------------
def parse_args():
    from config import settings as S
    p = argparse.ArgumentParser(description="mes.db 保留策略：按最旧 run 分批清理")
    p.add_argument("--db", default=S.MES_DB_PATH, help="SQLite 文件路径（默认 logs/mes.db）")
    p.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB,
                   help=f"活数据上限 MB（默认 {DEFAULT_MAX_MB:.0f}）")
    p.add_argument("--dry-run", action="store_true", help="只打印清理计划，不写库")
    return p.parse_args()


def _selftest() -> None:
    """临时库构造 3 个 run：阈值压到极小 → 应恰好删掉最旧 2 个、保留最新。"""
    import tempfile
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from mes.sqlite_ledger import _SCHEMA

    tmpdir = tempfile.mkdtemp(prefix="vsf_prune_")
    db = os.path.join(tmpdir, "t.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for i, run in enumerate(("20260801_000000_aaa", "20260802_000000_bbb",
                             "20260803_000000_ccc")):
        payload = "X" * 4096                       # 撑大行，让 3 个 run 超过极小阈值
        for j in range(4):
            conn.execute("INSERT INTO qc_log (run_id, ts_sim, product_id, result,"
                         " detail_json) VALUES (?,?,?,?,?)",
                         (run, float(j), f"P{i}{j}", "OK", payload))
        conn.execute("INSERT INTO orders (run_id, wo_id, status) VALUES (?,?,?)",
                     (run, f"WO-{i}", "已完成"))
    conn.commit()
    conn.close()

    st = prune_db(db, max_mb=0.0001)               # 阈值极小 → 清到只剩最新 run
    assert st["runs_deleted"] == 2 and not st["dry_run"], f"应删最旧2个run: {st}"
    assert st["qc_rows_deleted"] == 8 and st["orders_deleted"] == 2, f"行数不符: {st}"
    conn = sqlite3.connect(db)
    left = _runs_oldest_first(conn)
    qc_left = conn.execute("SELECT COUNT(*) FROM qc_log").fetchone()[0]
    conn.close()
    assert left == ["20260803_000000_ccc"] and qc_left == 4, \
        f"最新 run 必须完整保留: {left}, qc={qc_left}"

    st0 = prune_db(db, max_mb=0.0001, dry_run=True)  # 已在上限内 → dry-run 无动作
    assert st0["runs_deleted"] == 0, f"仅剩最新 run 时不得再删: {st0}"
    assert prune_db(db, max_mb=50)["runs_deleted"] == 0, "在上限内应零删除"
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("[db_prune 自检通过] 最旧优先×保最新×VACUUM×dry-run 语义正确 (仿真验证值)")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        _selftest()
        sys.exit(0)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        prune_db(args.db, max_mb=args.max_mb, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[db_prune] 终止: {exc}")
        sys.exit(1)

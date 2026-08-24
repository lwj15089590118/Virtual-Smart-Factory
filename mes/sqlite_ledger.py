# -*- coding: utf-8 -*-
"""
mes/sqlite_ledger.py —— MES 台账 SQLite 落库（标准库 sqlite3，零第三方依赖）
==============================================================================
职责：
    把 MESEngine 内存台账中的两类核心数据持久化到本地 SQLite 文件
    （路径 settings.MES_DB_PATH，默认 logs/mes.db，已被 .gitignore 忽略）：
        1. orders 表 —— 工单档案：开单建档、报工计数、满单关单，
           以 (run_id, wo_id) 联合主键增量 UPSERT；
        2. qc_log 表 —— 质检判定流水：每件判定一行，含归属工单号与算法明细，
           自增 id 全局单调，按 id 倒序即"最新在前"。
    数据可直接用任意 SQLite 工具 SELECT 分析；Web 端 /api/mes/qc_log 与
    selftest C4 断言均经由本类的查询接口。

设计要点：
    1. 单连接 + RLock 串行化：写入发生在仿真推进线程（时钟线程或主线程），
       读取可能来自 Flask 工作线程 —— check_same_thread=False + 全操作加锁，
       演示规模下吞吐绰绰有余；
    2. WAL 日志模式（不支持时静默退回默认日志）：REST 轮询读与仿真写互不阻塞；
    3. run_id 运行批次键：同一进程内自检会先后创建多台引擎（各自都有 WO-0001），
       联合主键让各次运行数据互不覆盖，历史运行天然保留可对比；
    4. 故障隔离：任何落库异常只打印告警绝不向上抛 —— 持久层故障不允许拖垮仿真
       （与事件总线"订阅者隔离"同款纪律；写入均为即时 commit，进程被杀也不丢账）。

时间纪律：
    表内时刻一律为仿真秒（事件 ts_sim / clock.now()）；墙钟仅用于 run_id 命名
    （与 EventBus 的 events_时间戳.jsonl 文件名同口径），不参与任何仿真计时。

假设记录：
    - 离线回放（MESEngine(None)，无总线）不落库：回放是"内存重建口径"
      （C2 用例比对在线/离线一致性），避免回放数据与在线运行混写同一文件；
    - 只做两表最小 schema，复杂分析直接对 .db 写 SQL 即可（作品集定位）。
"""

import json
import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime
from typing import List, Optional

# 路径引导：直接运行本文件(python mes/sqlite_ledger.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 两表最小 schema（orders:run_id↔wo_id 一档一案；qc_log 流水只增不改）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    run_id      TEXT NOT NULL,              -- 运行批次号（引擎实例级）
    wo_id       TEXT NOT NULL,              -- 工单号，如 WO-0001
    model       TEXT,                       -- 产品型号
    target_qty  INTEGER,                    -- 计划数量（件）
    ok_count    INTEGER DEFAULT 0,          -- 已报工合格数
    ng_count    INTEGER DEFAULT 0,          -- 已报工不合格数
    total       INTEGER DEFAULT 0,          -- 累计报工 = ok+ng
    status      TEXT,                       -- 执行中 / 已完成
    created_at  REAL,                       -- 开立时刻（仿真秒）
    closed_at   REAL,                       -- 关闭时刻（仿真秒；未关为 NULL）
    PRIMARY KEY (run_id, wo_id)
);
CREATE TABLE IF NOT EXISTS qc_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,            -- 运行批次号（引擎实例级）
    ts_sim        REAL,                     -- 判定时刻（仿真秒）
    product_id    TEXT,                     -- 产品号
    result        TEXT,                     -- OK / NG
    dim_mm        REAL,                     -- 关键尺寸观测值 mm
    wo_id         TEXT,                     -- 报工归属工单号
    algo          TEXT,                     -- 判定算法名（规则法路径为 NULL）
    clf_p_ng      REAL,                     -- 模型 P(NG)（规则法路径为 NULL）
    rule_result   TEXT,                     -- 规则法对照结论（规则法路径为 NULL）
    hidden_defect TEXT,                     -- 仿真真值缺陷类型（健康件="无"）
    detail_json   TEXT                      -- 其余明细整体 JSON（特征向量等）
);
CREATE INDEX IF NOT EXISTS idx_qc_run_ts  ON qc_log (run_id, ts_sim);
CREATE INDEX IF NOT EXISTS idx_qc_product ON qc_log (product_id);
"""


class MesSqliteLedger:
    """MES 台账落库器：orders UPSERT + qc_log 追加 + 参数化查询。

    与 MESEngine 的关系：引擎在 开单/关单/判定报工 三类事件处理点调用本类，
    本类不订阅总线、不持有时钟——纯被动持久层，可独立单测。
    """

    # record_qc 提升为独立列的字段；其余字段整体进 detail_json（保真不丢信息）
    _QC_COLUMNS = ("product_id", "ts_sim", "result", "dim_mm",
                   "algo", "clf_p_ng", "rule_result", "hidden_defect")

    def __init__(self, db_path: str):
        """
        :param db_path: SQLite 文件路径（相对路径按启动时工作目录解析，
                        与 EventBus 的 logs/ 目录同约定）；父目录自动创建。
        """
        self.db_path = db_path
        # run_id：墙钟命名（仅作批次标识；同秒多实例以短随机后缀去重）
        self.run_id = "{}_{}".format(
            datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
        self._lock = threading.RLock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            # WAL：读写互不阻塞（部分文件系统不支持 → 静默退回默认回滚日志）
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    # 写入（由 MESEngine 在事件处理线程调用；异常内部消化不外抛）
    # ------------------------------------------------------------------
    def upsert_order(self, wo) -> None:
        """工单档案 UPSERT：开单 / 报工计数变化 / 满单关单 时由引擎调用。

        :param wo: mes.order_model.WorkOrder 实例（鸭子类型：只读其公开属性）。
        """
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO orders "
                    "(run_id, wo_id, model, target_qty, ok_count, ng_count,"
                    " total, status, created_at, closed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, wo.wo_id, wo.model, int(wo.target_qty),
                     int(wo.ok_count), int(wo.ng_count), int(wo.total_count),
                     wo.status, float(wo.created_at),
                     None if wo.closed_at is None else float(wo.closed_at)))
                self._conn.commit()
        except sqlite3.Error as exc:
            print(f"[MesSqlite] 工单落库失败 {wo.wo_id}: {exc}")

    def record_qc(self, data: dict, wo_id: Optional[str], ts: float) -> None:
        """追加一条质检判定流水。

        :param data:  vision.ok / vision.ng 事件的负载字典（质检记录）。
        :param wo_id: 报工归属工单号（引擎 _open_order() 的当前工单）。
        :param ts:    判定时刻（仿真秒，取事件自带 ts_sim）。
        """
        try:
            vals = {k: data.get(k) for k in self._QC_COLUMNS}
            rest = {k: v for k, v in data.items() if k not in self._QC_COLUMNS}
            try:
                detail = json.dumps(rest, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail = None
            with self._lock:
                self._conn.execute(
                    "INSERT INTO qc_log (run_id, ts_sim, product_id, result,"
                    " dim_mm, wo_id, algo, clf_p_ng, rule_result, hidden_defect,"
                    " detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, round(float(ts), 3),
                     None if vals["product_id"] is None else str(vals["product_id"]),
                     None if vals["result"] is None else str(vals["result"]),
                     None if vals["dim_mm"] is None else float(vals["dim_mm"]),
                     wo_id, vals["algo"],
                     None if vals["clf_p_ng"] is None else float(vals["clf_p_ng"]),
                     vals["rule_result"], vals["hidden_defect"], detail))
                self._conn.commit()
        except sqlite3.Error as exc:
            print(f"[MesSqlite] 判定流水落库失败: {exc}")

    # ------------------------------------------------------------------
    # 查询（Flask REST 线程 / 自检断言调用；全部参数化防注入）
    # ------------------------------------------------------------------
    @staticmethod
    def _qc_where(result: Optional[str], wo_id: Optional[str],
                  product_id: Optional[str], run_id: Optional[str]) -> tuple:
        """把可选过滤条件拼成 WHERE 子句与参数表（None 条件不过滤）。"""
        sql, args = "", []
        for col, val in (("result", result), ("wo_id", wo_id),
                         ("product_id", product_id), ("run_id", run_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        return sql, args

    def query_qc(self, limit: int = 50, result: Optional[str] = None,
                 wo_id: Optional[str] = None, product_id: Optional[str] = None,
                 run_id: Optional[str] = None) -> List[dict]:
        """按条件查判定流水（id 倒序 = 最新在前），供 /api/mes/qc_log。"""
        where, args = self._qc_where(result, wo_id, product_id, run_id)
        args.append(max(1, min(int(limit), 10000)))
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM qc_log WHERE 1=1" + where +
                " ORDER BY id DESC LIMIT ?", args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return rows

    def count_qc(self, result: Optional[str] = None,
                 wo_id: Optional[str] = None, product_id: Optional[str] = None,
                 run_id: Optional[str] = None) -> int:
        """按同一组过滤条件计流水行数（对账用：应等于内存报工判定总数）。"""
        where, args = self._qc_where(result, wo_id, product_id, run_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM qc_log WHERE 1=1" + where, args).fetchone()
        return int(row[0])

    def query_orders(self, run_id: Optional[str] = None,
                     status: Optional[str] = None) -> List[dict]:
        """查工单档案（开立时刻倒序）。"""
        sql, args = "", []
        for col, val in (("run_id", run_id), ("status", status)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM orders WHERE 1=1" + sql +
                " ORDER BY created_at DESC, wo_id DESC", args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return rows

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接（每次写入均已即时 commit，此处仅规范收尾，幂等）。"""
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass


# ----------------------------------------------------------------------
# 自模块快速自检：python mes/sqlite_ledger.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import shutil
    import sys
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from config import settings as S
    from mes.order_model import WorkOrder

    tmpdir = tempfile.mkdtemp(prefix="vsf_ledger_")
    db = os.path.join(tmpdir, "t.db")
    led = MesSqliteLedger(db)

    # --- 1) 工单 UPSERT：先建档，再改计数/关单覆盖写 ---
    wo = WorkOrder("WO-9001", S.MES_PRODUCT_MODEL, 100, created_at=12.5)
    led.upsert_order(wo)
    rows = led.query_orders(run_id=led.run_id)
    assert len(rows) == 1 and rows[0]["status"] == "执行中" \
        and rows[0]["closed_at"] is None and rows[0]["total"] == 0, f"建档行异常: {rows}"
    for i in range(7):
        if i < 5:
            wo.ok_count += 1
        else:
            wo.ng_count += 1
        led.upsert_order(wo)                       # 每次报工都整行覆盖
    wo.status, wo.closed_at = "已完成", 88.0
    led.upsert_order(wo)
    rows = led.query_orders(run_id=led.run_id)
    assert len(rows) == 1 and rows[0]["status"] == "已完成" \
        and rows[0]["ok_count"] == 5 and rows[0]["ng_count"] == 2 \
        and rows[0]["total"] == 7 and abs(rows[0]["closed_at"] - 88.0) < 1e-9, \
        f"UPSERT 覆盖后不一致: {rows}"

    # --- 2) 判定流水：算法明细路径 + 规则法路径混排，逐字段/过滤核对 ---
    for i in range(5):
        result = "OK" if i < 3 else "NG"
        data = {"product_id": f"P{i:08d}", "result": result,
                "dim_mm": round(10.0 + i * 0.001, 4)}
        if i % 2 == 0:                             # 算法路径：带明细
            data.update({"algo": "LR-v3", "clf_p_ng": 0.02 * i,
                         "rule_result": result,
                         "hidden_defect": "无" if result == "OK" else "表面划痕",
                         "features": {"尺寸偏差mm": 0.01 * i}})
        led.record_qc(data, wo_id=wo.wo_id, ts=100.0 + i)
    assert led.count_qc(run_id=led.run_id) == 5
    assert led.count_qc(run_id=led.run_id, result="NG") == 2
    assert led.count_qc(run_id=led.run_id, wo_id=wo.wo_id) == 5
    assert led.count_qc(run_id=led.run_id, product_id="P00000004") == 1
    latest = led.query_qc(limit=2, run_id=led.run_id)
    assert [r["product_id"] for r in latest] == ["P00000004", "P00000003"], \
        "id 倒序应为最新在前"
    algo_row = next(r for r in led.query_qc(limit=10, run_id=led.run_id)
                    if r["algo"] is not None)
    assert algo_row["clf_p_ng"] is not None and "features" in json.loads(algo_row["detail_json"])
    rule_row = next(r for r in led.query_qc(limit=10, run_id=led.run_id)
                    if r["algo"] is None)
    assert rule_row["clf_p_ng"] is None and rule_row["result"] == "NG"

    # --- 3) 跨 run 隔离：第二个实例同工单号互不覆盖 ---
    led2 = MesSqliteLedger(db)
    assert led2.run_id != led.run_id
    wo_b = WorkOrder("WO-9001", "型号B", 5, created_at=1.0)
    led2.upsert_order(wo_b)
    mine = [r for r in led.query_orders() if r["run_id"] == led.run_id]
    theirs = [r for r in led.query_orders() if r["run_id"] == led2.run_id]
    assert len(mine) == 1 and mine[0]["target_qty"] == 100 \
        and len(theirs) == 1 and theirs[0]["target_qty"] == 5, "run_id 隔离失败"

    # --- 4) 收尾与文件落盘 ---
    led.close()
    led2.close()
    assert os.path.exists(db)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("[sqlite_ledger 自检通过] orders UPSERT×9/qc_log×5 行, 过滤与倒序正确, "
          "跨 run 隔离 OK (仿真验证值)")

# -*- coding: utf-8 -*-
"""
mes/mes_engine.py —— MES 引擎：自动报工 + 追溯 + OEE 近似口径（阶段3新增）
==========================================================================
设计要点：
    1. 数据源 = 事件总线（交付要求的唯一通道）：订阅通配符 "*"，
       按事件类型增量维护 工单/批次/托盘/产品 台账与故障停机台账；
       离线模式（plant=None，供 JSONL 回放）复用同一 ingest() 逻辑，
       保证"在线台账 == 回放重建台账"；
       增强：在线模式下同步落库 SQLite 台账（mes/sqlite_ledger.py，
       orders 工单档案 + qc_log 判定流水；S.MES_SQLITE_ENABLE 可关闭）。
    2. 报工口径（全部为仿真验证值）：
       - 良率 Q = 质检 OK / (OK+NG)（以 vision 判定事件为准）；
       - 可用率 A = 1 - Σ故障停机时长/窗口时长（fault.raised/cleared 配对记账）；
       - 性能率 P = 实际报工产量 / (装配单元运行时长/节拍) 的理论产量，截断≤1；
       - OEE ≈ A × P × Q（装配单元近似口径，假设记录见 report() 注释）；
    3. 自动翻单：工单报满 → 关闭并发 MES_ORDER_CLOSED 事件 → 自动开下一张
       （贴近真实产线"后拉式"连续排产）；也支持 Web 命令手动开单。

时间纪律：
    全部使用事件 ts_sim 与 clock.now()/回放窗口端点，不接触墙钟。

假设记录：
    - 单一产线单一型号，工单按 FIFO 顺序投产；性能率以装配单元节拍为基准。
"""

import os
import sys
from collections import Counter
# 路径引导：直接运行本文件(python mes/mes_engine.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, List, Optional

from core.event_bus import EventTypes
from config import settings as S
from mes.order_model import WorkOrder, Batch, TraceabilityIndex

# 报工相关的事件类型集合（路由表：type -> 处理方法名后缀）
_KEY_UNITS = (S.ASSEMBLY_ID, S.VISION_ID, S.PALLETIZER_ID, S.WAREHOUSE_ID)


class MESEngine:
    """MES 引擎：可挂接 Plant 在线运行，也可离线吃 JSONL 事件流。"""

    def __init__(self, plant=None):
        self.plant = plant
        self.bus = getattr(plant, "bus", None) if plant is not None else None
        self.clock = getattr(plant, "clock", None) if plant is not None else None
        # ---- 台账 ----
        self.orders: List[WorkOrder] = []
        self.batches: Dict[str, Batch] = {}
        self.index = TraceabilityIndex(S.MES_TRACE_MAX)
        self._wo_seq = 0
        self._batch_seq: Dict[str, int] = {}            # wo_id -> 批次序号
        # ---- 总量统计（跨工单累计，报工报表用）----
        self.stat_products_out = 0                      # 装配流出总数
        self.stat_ok = 0                                # 质检 OK 总数
        self.stat_ng = 0                                # 质检 NG 总数
        self.stat_pallets_done = 0                      # 满托总数
        self.stat_shipped = 0                           # 出厂托数
        # ---- 故障停机台账（可用率数据源）----
        self._fault_open: Dict[str, float] = {}         # dev_id -> 故障开始时刻
        self.downtime_s: Dict[str, float] = {k: 0.0 for k in _KEY_UNITS}
        self.fault_counts: Counter = Counter()
        # ---- 离线模式的窗口端点 ----
        self._max_ts = 0.0
        # ---- 订阅（在线模式）----
        self._token = None
        # ---- 增强：SQLite 台账落库句柄（无条件先置 None：
        #      离线回放/自动补单路径的 create_order 也要安全引用该属性）----
        self.ledger = None
        if self.bus is not None:
            # 增强：在线模式且开关开启时装配落库器；MESEngine(None) 无总线
            #      → 恒为 None 不落库（见 sqlite_ledger 假设记录）
            if S.MES_SQLITE_ENABLE:
                from mes.sqlite_ledger import MesSqliteLedger   # 局部导入：离线模式零开销
                try:
                    self.ledger = MesSqliteLedger(S.MES_DB_PATH)
                    print(f"[MES 台账] SQLite 落库已启用 → {S.MES_DB_PATH} "
                          f"(run_id={self.ledger.run_id})")
                except Exception as exc:        # 初始化失败降级为纯内存台账，不阻断仿真
                    print(f"[MES 台账] SQLite 初始化失败（退回内存台账）: {exc}")
            self._token = self.bus.subscribe("*", self.ingest, "MES引擎")
            self.create_order(S.MES_DEFAULT_ORDER_QTY)   # 开局首张工单（含审计事件）

    # ==================================================================
    # 时间取值：在线用时钟；离线用已见事件最大 ts
    # ==================================================================
    def _now(self) -> float:
        if self.clock is not None:
            return round(self.clock.now(), 3)
        return round(self._max_ts, 3)

    # ==================================================================
    # 工单管理
    # ==================================================================
    def create_order(self, qty: int,
                     model: str = S.MES_PRODUCT_MODEL) -> WorkOrder:
        """开立一张工单并广播 mes.order_created（在线模式才发事件）。"""
        self._wo_seq += 1
        wo = WorkOrder(f"WO-{self._wo_seq:04d}", model, int(qty), self._now())
        self.orders.append(wo)
        if self.ledger is not None:
            self.ledger.upsert_order(wo)            # 增强：开单即落库建档
        if self.bus is not None:
            self.bus.publish("MES-ENGINE", EventTypes.MES_ORDER_CREATED,
                             dict(wo.to_dict()), severity="INFO")
        return wo

    def _open_order(self) -> WorkOrder:
        """
        取当前执行中的工单；没有则自动补开（防停产无单可报）。
        语义（增强）：倒序取"最新一张"执行中工单——后开单优先投产，
        对应真实产线"插单"习惯，也保证 Web/REST 手动指定数量的新单
        立即成为当前报工对象（旧 FIFO 口径下手动单会永远排在大单之后）。
        """
        for wo in reversed(self.orders):
            if wo.status == "执行中":
                return wo
        return self.create_order(S.MES_DEFAULT_ORDER_QTY)

    def _close_order(self, wo: WorkOrder) -> None:
        """关单 + 广播审计事件。"""
        wo.status = "已完成"
        wo.closed_at = self._now()
        if self.ledger is not None:
            self.ledger.upsert_order(wo)            # 增强：满单关单状态落库
        if self.bus is not None:
            self.bus.publish("MES-ENGINE", EventTypes.MES_ORDER_CLOSED,
                             dict(wo.to_dict()), severity="INFO")

    def _current_batch(self, wo: WorkOrder) -> Batch:
        """取工单下未满的批次；没有则新开一批（每 MES_BATCH_PALLETS 托一批）。"""
        for b in reversed(wo.batch_ids):
            batch = self.batches[b]
            if not batch.full(S.MES_BATCH_PALLETS):
                return batch
        n = self._batch_seq.get(wo.wo_id, 0) + 1
        self._batch_seq[wo.wo_id] = n
        batch = Batch(f"{wo.wo_id}-B{n:02d}", wo.wo_id, self._now())
        self.batches[batch.batch_id] = batch
        wo.batch_ids.append(batch.batch_id)
        self.index.bind_batch_order(batch.batch_id, wo.wo_id)
        return batch

    # ==================================================================
    # 事件入口（在线订阅与离线回放共用）
    # ==================================================================
    def ingest(self, event: dict) -> None:
        """处理一条事件（总线回调 / JSONL 回放统一入口）。"""
        t = event.get("type")
        d = event.get("data") or {}
        ts = float(event.get("ts_sim", 0.0))
        if ts > self._max_ts:
            self._max_ts = ts

        # ---- 质检判定 → 报工 + QC 档案 ----
        if t in (EventTypes.VISION_OK, EventTypes.VISION_NG):
            pid = d.get("product_id")
            wo = self._open_order()
            if t == EventTypes.VISION_OK:
                wo.ok_count += 1
                self.stat_ok += 1
            else:
                wo.ng_count += 1
                self.stat_ng += 1
            self.index.record_qc(pid, d, ts)
            if self.ledger is not None:
                # 增强：判定流水落库（含归属工单号；行数应恒等于 stat_ok+stat_ng）
                self.ledger.record_qc(d, wo.wo_id, ts)
            if wo.total_count >= wo.target_qty and wo.status == "执行中":
                self._close_order(wo)
                self.create_order(S.MES_DEFAULT_ORDER_QTY)   # 自动翻单
            return
        # ---- 码垛放箱 → 产品归托 ----
        if t == EventTypes.BOX_PLACED:
            pid, pallet = d.get("product_id"), d.get("pallet_id")
            if pid and pallet:
                self.index.bind_product_pallet(pid, pallet)
            return
        # ---- 满托 → 归批 + 流转档案 ----
        if t == EventTypes.PALLET_FULL:
            pallet = d.get("pallet_id")
            self.stat_pallets_done += 1
            if pallet:
                wo = self._open_order()
                batch = self._current_batch(wo)
                batch.pallet_ids.append(pallet)
                self.index.bind_pallet_batch(pallet, batch.batch_id)
                self.index.pallet_event(pallet, ts, "码垛完成",
                                        f"{d.get('box_count', '?')}箱")
            return
        # ---- 立体库出入库 → 位置档案 ----
        if t == EventTypes.WH_INBOUND_DONE:
            pallet, loc = d.get("pallet_id"), d.get("loc_id")
            self.index.pallet_event(pallet, ts, "入库上架", loc or "")
            self.index.set_location(pallet, loc)
            return
        if t == EventTypes.WH_OUTBOUND_DONE:
            pallet = d.get("pallet_id")
            self.index.pallet_event(pallet, ts, "出库下架", d.get("from_loc", ""))
            self.index.set_location(pallet, None)
            return
        # ---- AGV 出厂闭环 ----
        if t == EventTypes.AGV_TASK_DONE and d.get("task_type") == "出库":
            self.stat_shipped += 1
            self.index.pallet_event(d.get("pallet_id"), ts, "已出厂", "出货口")
            return
        # ---- 装配流出（性能率分母侧统计）----
        if t == EventTypes.PRODUCT_OUT:
            self.stat_products_out += 1
            return
        # ---- 故障配对记账（可用率数据源）----
        if t == EventTypes.FAULT_RAISED:
            dev = event.get("source")
            self.fault_counts[dev] += 1
            if dev not in self._fault_open:
                self._fault_open[dev] = ts
            return
        if t == EventTypes.FAULT_CLEARED:
            dev = event.get("source")
            t0 = self._fault_open.pop(dev, None)
            if dev in self.downtime_s and t0 is not None:
                self.downtime_s[dev] = round(
                    self.downtime_s[dev] + max(ts - t0, 0.0), 3)
            return

    # ==================================================================
    # 报工统计（OEE 近似口径）
    # ==================================================================
    def report(self) -> dict:
        """
        阶段报工报表。OEE 口径（装配单元近似，全部为仿真验证值）：
            A 可用率 = 1 - 停机时长/窗口（fault.raised/cleared 配对）
            P 性能率 = 报工产量 / (装配运行时长 ÷ 节拍)，截断≤1
            Q 良品率 = OK / (OK+NG)
            OEE     = A × P × Q
        """
        window = max(self._now(), 1e-6)
        asm_dt = self.downtime_s.get(S.ASSEMBLY_ID, 0.0)
        availability = max(0.0, 1.0 - asm_dt / window)
        asm_uptime = max(window - asm_dt, 0.0)
        theoretical = asm_uptime / max(getattr(S, "ASSEMBLY_TAKT", 32.0), 1e-6)
        judged = self.stat_ok + self.stat_ng
        performance = min(judged / max(theoretical, 1e-6), 1.0)
        quality = self.stat_ok / max(judged, 1)
        oee = availability * performance * quality
        return {
            "note": "所有指标均为仿真验证值",
            "window_s": round(window, 1),
            "products_out": self.stat_products_out,
            "judged": judged, "ok": self.stat_ok, "ng": self.stat_ng,
            "quality_pct": round(quality * 100, 2),
            "availability_pct": round(availability * 100, 2),
            "performance_pct": round(performance * 100, 2),
            "oee_pct": round(oee * 100, 2),
            "pallets_done": self.stat_pallets_done,
            "shipped": self.stat_shipped,
            "downtime_s": {k: round(v, 1) for k, v in self.downtime_s.items()},
            "fault_counts": dict(self.fault_counts),
        }

    # ==================================================================
    # 查询接口（Web REST 直接调用）
    # ==================================================================
    def snapshot_orders(self, n: int = 20) -> List[dict]:
        """最近 n 张工单摘要（新单在前）。"""
        return [wo.to_dict() for wo in reversed(self.orders[-n:])]

    def snapshot_batches(self, wo_id: Optional[str] = None, n: int = 20) -> List[dict]:
        """批次摘要（可按工单过滤）。"""
        out = []
        for b in self.batches.values():
            if wo_id is None or b.wo_id == wo_id:
                out.append(b.to_dict())
        return list(reversed(out[-n:]))

    def trace(self, query: str) -> Optional[dict]:
        """
        全链路追溯反查：输入 产品号/托盘号 均可。
        返回带层级的完整链路（订单→批次→托盘→产品→位置历史）。
        """
        query = (query or "").strip()
        if not query:
            return None
        hit = self.index.lookup_product(query)          # 先按产品查
        kind = "产品"
        if hit is None:
            hit = self.index.lookup_pallet(query)       # 再按托盘查
            kind = "托盘"
        if hit is None:
            return None
        return {"kind": kind, "chain": hit,
                "status": self.index.pallet_status(
                    hit["pallet_id"] if kind == "产品" else query)}

    def close(self) -> None:
        """退订总线并关闭 SQLite 台账连接（程序退出时调用）。"""
        if self.bus is not None and self._token is not None:
            self.bus.unsubscribe(self._token)
            self._token = None
        if getattr(self, "ledger", None) is not None:
            self.ledger.close()
            self.ledger = None


# ----------------------------------------------------------------------
# 自模块快速自检：python mes/mes_engine.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.sim_clock import SimClock
    from core.event_bus import EventBus

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)

    class _FakePlant:
        """最小 Plant 替身：只为引擎提供 bus/clock。"""
        bus = bus
        clock = clock

    eng = MESEngine(_FakePlant())
    assert len(eng.orders) == 1, "开局应自动建首张工单"

    # 模拟一段事件流：30件OK + 10件NG + 1满托入库出厂 + 一次故障配对
    for i in range(40):
        et = EventTypes.VISION_OK if i < 30 else EventTypes.VISION_NG
        bus.publish("VIS-T", et, {"product_id": f"P{i:08d}",
                                  "result": "OK" if i < 30 else "NG",
                                  "dim_mm": 10.0})
    for k in range(48):
        bus.publish("PAL-T", EventTypes.BOX_PLACED,
                    {"product_id": f"Q{k:08d}", "pallet_id": "PLT000001"})
    bus.publish("PAL-T", EventTypes.PALLET_FULL,
                {"pallet_id": "PLT000001", "box_count": 48})
    bus.publish("WH-T", EventTypes.WH_INBOUND_DONE,
                {"pallet_id": "PLT000001", "loc_id": "A-01-01-01"})
    bus.publish("AGV-T", EventTypes.AGV_TASK_DONE,
                {"task_type": "出库", "pallet_id": "PLT000001"})
    # 故障配对：t=10 发生，t=30 清除 → 停机 20s / 窗口 60s
    clock.run_until(10.0)
    bus.publish("ASM-01", EventTypes.FAULT_RAISED, {"fault_type": "伺服过载"})
    clock.run_until(30.0)
    bus.publish("ASM-01", EventTypes.FAULT_CLEARED,
                {"fault_type": "伺服过载", "duration_s": 20.0})
    clock.run_until(60.0)

    rep = eng.report()
    assert rep["ok"] == 30 and rep["ng"] == 10, f"报工数错误: {rep}"
    assert abs(rep["quality_pct"] - 75.0) < 0.01, "良率口径错误"
    assert rep["pallets_done"] == 1 and rep["shipped"] == 1
    assert rep["oee_pct"] > 0 and rep["oee_pct"] <= 100
    assert abs(eng.downtime_s[S.ASSEMBLY_ID] - 20.0) < 0.05, \
        f"故障配对应记 20s 停机: {eng.downtime_s[S.ASSEMBLY_ID]}"
    # 追溯链路
    tr = eng.trace("Q00000000")
    assert tr and tr["kind"] == "产品" and tr["chain"]["pallet_id"] == "PLT000001"
    assert tr["chain"]["wo_id"] == eng.orders[0].wo_id
    tp = eng.trace("PLT000001")
    assert tp["status"] == "已出厂" and len(tp["chain"]["events"]) == 3
    assert eng.trace("NO-SUCH-ID") is None
    print(f"[mes_engine 自检通过] 报工OK{rep['ok']}/NG{rep['ng']}, "
          f"良率{rep['quality_pct']}%, 追溯四级链路+出厂状态闭环 (仿真验证值)")

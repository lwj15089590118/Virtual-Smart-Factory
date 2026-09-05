# -*- coding: utf-8 -*-
"""
mes/order_model.py —— 工单 / 批次数据模型 + 全链路追溯索引（阶段3新增）
========================================================================
三级制造层级（与真实 MES 的层级口径一致）：
    工单 WorkOrder（计划数量/型号/状态）
      └─ 批次 Batch（工单内按托盘分组，每 MES_BATCH_PALLETS 托为一个批次）
           └─ 托盘 Pallet（48 件产品）→ 产品 Product

TraceabilityIndex 追溯索引（全部由事件流增量构建，支持正查/反查）：
    产品 → 托盘 → 批次 → 工单（自下而上反查）
    工单 → 批次 → 托盘 → 产品（自上而下正查）
    托盘位置历史：码垛完成 → 入库上架(库位) → 出库下架 → AGV出厂
    产品质检档案：判定结果/尺寸/时刻（来自 vision.ok / vision.ng 事件）

时间纪律：
    只使用事件自带的 ts_sim 时间戳，不接触墙钟。

假设记录：
    - 索引容量有上限（settings.MES_TRACE_MAX），超限后停止记录新键并计数溢出，
      防止作品集长跑演示内存膨胀；已记录的键不受影响。
"""

import os
import sys
# 路径引导：直接运行本文件(python mes/order_model.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, List, Optional

from config import settings as S


class WorkOrder:
    """一张工单：MES 最小排产单元。"""

    def __init__(self, wo_id: str, model: str, target_qty: int, created_at: float):
        self.wo_id = wo_id                  # 工单号，如 WO-0001
        self.model = model                  # 产品型号
        self.target_qty = int(target_qty)   # 计划数量（件）
        self.created_at = created_at        # 开立时刻（仿真秒）
        self.closed_at: Optional[float] = None
        self.status = "执行中"               # 执行中 / 已完成
        self.ok_count = 0                   # 已报工合格数
        self.ng_count = 0                   # 已报工不合格数
        self.batch_ids: List[str] = []      # 名下批次号

    @property
    def total_count(self) -> int:
        """累计报工总数。"""
        return self.ok_count + self.ng_count

    @property
    def progress_pct(self) -> float:
        """完成进度百分比。"""
        return round(min(self.total_count / max(self.target_qty, 1) * 100, 100.0), 1)

    def to_dict(self) -> dict:
        return {
            "wo_id": self.wo_id, "model": self.model,
            "target_qty": self.target_qty, "ok": self.ok_count,
            "ng": self.ng_count, "total": self.total_count,
            "progress_pct": self.progress_pct, "status": self.status,
            "created_at": self.created_at, "closed_at": self.closed_at,
            "batch_ids": list(self.batch_ids),
            "yield_pct": round(self.ok_count / max(self.total_count, 1) * 100, 2),
        }


class Batch:
    """一个生产批次：工单内按托盘分组的中间层（每 BATCH_PALLETS 托一批）。"""

    def __init__(self, batch_id: str, wo_id: str, created_at: float):
        self.batch_id = batch_id            # 批次号，如 WO-0001-B01
        self.wo_id = wo_id                  # 所属工单
        self.created_at = created_at
        self.pallet_ids: List[str] = []     # 名下托盘号

    def full(self, max_pallets: int) -> bool:
        return len(self.pallet_ids) >= max_pallets

    def to_dict(self) -> dict:
        return {"batch_id": self.batch_id, "wo_id": self.wo_id,
                "created_at": self.created_at, "pallet_ids": list(self.pallet_ids),
                "pallet_count": len(self.pallet_ids)}


class TraceabilityIndex:
    """全链路追溯索引：产品↔托盘↔批次↔工单 + 位置/质检档案。"""

    def __init__(self, capacity: int = S.MES_TRACE_MAX):
        self.capacity = int(capacity)
        self.overflow = 0                                   # 超限丢弃的键数（诊断）
        # ---- 层级映射（自下而上 / 自上而下共用）----
        self.product_pallet: Dict[str, str] = {}            # 产品 → 托盘
        self.pallet_products: Dict[str, List[str]] = {}     # 托盘 → [产品]
        self.pallet_batch: Dict[str, str] = {}              # 托盘 → 批次
        self.batch_order: Dict[str, str] = {}               # 批次 → 工单
        # ---- 档案 ----
        self.product_qc: Dict[str, dict] = {}               # 产品 → 质检档案
        self.pallet_events: Dict[str, List[tuple]] = {}     # 托盘 → [(ts,阶段,说明)]
        self.pallet_location: Dict[str, Optional[str]] = {} # 托盘 → 当前库位(None=不在库)

    # ------------------------------------------------------------------
    def _allow(self, key: str) -> bool:
        """容量守卫：新键超上限则丢弃并计数（防长跑爆内存）。"""
        if key in self.product_pallet or key in self.pallet_products \
                or key in self.pallet_batch or len(self.product_pallet) < self.capacity:
            return True
        self.overflow += 1
        return False

    # ------------------------------------------------------------------
    def record_qc(self, product_id: str, qc_event_data: dict, ts: float) -> None:
        """记录产品质检档案（vision.ok/ng 事件）。"""
        if not self._allow(product_id):
            return
        self.product_qc[product_id] = {
            "product_id": product_id, "result": qc_event_data.get("result"),
            "dim_mm": qc_event_data.get("dim_mm"), "ts_sim": ts,
        }

    def bind_product_pallet(self, product_id: str, pallet_id: str) -> None:
        """码垛放箱事件：建立 产品→托盘 双向映射。"""
        if not (self._allow(product_id) and self._allow(pallet_id)):
            return
        self.product_pallet[product_id] = pallet_id
        self.pallet_products.setdefault(pallet_id, []).append(product_id)

    def bind_pallet_batch(self, pallet_id: str, batch_id: str) -> None:
        """满托事件：托盘归批。"""
        if not self._allow(pallet_id):
            return
        self.pallet_batch[pallet_id] = batch_id

    def bind_batch_order(self, batch_id: str, wo_id: str) -> None:
        """批次归单。"""
        self.batch_order[batch_id] = wo_id

    def pallet_event(self, pallet_id: str, ts: float, stage: str, note: str = "") -> None:
        """托盘流转事件追加（码垛完成/入库上架/出库下架/出厂）。"""
        if not self._allow(pallet_id):
            return
        self.pallet_events.setdefault(pallet_id, []).append(
            (round(ts, 3), stage, note))

    def set_location(self, pallet_id: str, loc_id: Optional[str]) -> None:
        """更新托盘当前位置（库位号或 None 表示已离开立体库）。"""
        if pallet_id in self.pallet_location or self._allow(pallet_id):
            self.pallet_location[pallet_id] = loc_id

    # ------------------------------------------------------------------
    # 反查接口
    # ------------------------------------------------------------------
    def lookup_product(self, product_id: str) -> Optional[dict]:
        """产品反查：产品 → 托盘 → 批次 → 工单 + QC 档案。"""
        pallet_id = self.product_pallet.get(product_id)
        if pallet_id is None:
            return None
        batch_id = self.pallet_batch.get(pallet_id)
        return {
            "product": self.product_qc.get(product_id, {"product_id": product_id}),
            "pallet_id": pallet_id,
            "batch_id": batch_id,
            "wo_id": self.batch_order.get(batch_id) if batch_id else None,
            "location": self.pallet_location.get(pallet_id),
            "pallet_events": self.pallet_events.get(pallet_id, []),
        }

    def lookup_pallet(self, pallet_id: str) -> Optional[dict]:
        """托盘查询：托盘 → 批次 → 工单 + 产品清单 + 流转历史。"""
        if pallet_id not in self.pallet_products and pallet_id not in self.pallet_batch:
            return None
        batch_id = self.pallet_batch.get(pallet_id)
        return {
            "pallet_id": pallet_id,
            "batch_id": batch_id,
            "wo_id": self.batch_order.get(batch_id) if batch_id else None,
            "products": list(self.pallet_products.get(pallet_id, [])),
            "location": self.pallet_location.get(pallet_id),
            "events": list(self.pallet_events.get(pallet_id, [])),
            "status": self.pallet_status(pallet_id),
        }

    def pallet_status(self, pallet_id: str) -> str:
        """托盘当前状态文案（追溯面板展示用）。"""
        evs = self.pallet_events.get(pallet_id, [])
        stages = [e[1] for e in evs]
        if "已出厂" in stages:
            return "已出厂"
        loc = self.pallet_location.get(pallet_id)
        if loc:
            return f"在库 {loc}"
        if "出库下架" in stages:
            return "出库暂存/运输中"
        if "码垛完成" in stages:
            return "待入库搬运"
        return "未知"


# ----------------------------------------------------------------------
# 自模块快速自检：python mes/order_model.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from core.event_bus import EventTypes

    idx = TraceabilityIndex()
    # 构造一条完整链路：WO-0001 → B01 → PLT1 → P1/P2
    wo = WorkOrder("WO-0001", S.MES_PRODUCT_MODEL, 96, 0.0)
    b = Batch("WO-0001-B01", wo.wo_id, 1.0)
    b.pallet_ids.append("PLT000001")
    wo.batch_ids.append(b.batch_id)
    idx.bind_batch_order(b.batch_id, wo.wo_id)
    idx.bind_pallet_batch("PLT000001", b.batch_id)
    for i in range(2):
        pid = f"P{i:08d}"
        idx.record_qc(pid, {"result": "OK", "dim_mm": 10.001}, ts=5.0)
        idx.bind_product_pallet(pid, "PLT000001")
    idx.pallet_event("PLT000001", 10.0, EventTypes.PALLET_FULL.split(".")[1], "48箱")
    idx.pallet_event("PLT000001", 40.0, "入库上架", "A-01-01-01")
    idx.set_location("PLT000001", "A-01-01-01")

    # 反查产品
    r = idx.lookup_product("P00000000")
    assert r["pallet_id"] == "PLT000001" and r["batch_id"] == "WO-0001-B01"
    assert r["wo_id"] == "WO-0001" and r["location"] == "A-01-01-01"
    assert r["product"]["result"] == "OK"
    # 查询托盘
    p = idx.lookup_pallet("PLT000001")
    assert p["status"].startswith("在库") and len(p["products"]) == 2
    assert p["events"][0][1] == "full"
    assert idx.lookup_product("NO-SUCH") is None
    print("[order_model 自检通过] 产品→托盘→批次→工单 四级反查闭环 (仿真验证值)")

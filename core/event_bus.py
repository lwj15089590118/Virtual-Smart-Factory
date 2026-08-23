# -*- coding: utf-8 -*-
"""
core/event_bus.py —— 全厂事件总线（发布/订阅 + JSONL 追加持久化）
==================================================================
设计要点：
    1. 进程内同步发布/订阅：publish() 按主题分发给订阅者，异常互相隔离；
    2. 每条事件以一行 JSON 追加写入 logs/events_*.jsonl —— 这是后续班次
       SCADA 历史库、MES 报工、健康模块特征提取的统一数据入口；
    3. 支持精确主题与通配符 "*" 订阅；提供 recent()/replay() 查询接口，
       班次2 的 Web 端将直接复用。
事件结构（统一 schema，全部可 JSON 序列化）：
    {"seq": int, "ts_sim": float, "ts_wall": str, "source": str,
     "type": str, "severity": str, "data": dict}

假设记录：
    - 本班次单进程内存总线即可满足吞吐；班次2 如需跨进程，可在本类外再包一层。
"""

import json
import os
import threading
from collections import deque
from datetime import datetime
from typing import Callable, Dict, List, Optional


class EventTypes:
    """全厂统一事件类型常量（后续班次只允许在此追加，禁止散落字符串）。"""
    DEVICE_STATE = "device.state"          # 设备状态迁移
    FAULT_RAISED = "fault.raised"          # 故障产生（随机/脚本/联锁/人工）
    FAULT_CLEARED = "fault.cleared"        # 故障清除/复位
    PRODUCT_OUT = "flow.product_out"       # 装配单元流出一件产品
    VISION_OK = "vision.ok"                # 质检判定 OK
    VISION_NG = "vision.ng"                # 质检判定 NG（分流返修）
    BOX_PLACED = "pallet.box_placed"       # 码垛机放置一箱（含垛内坐标）
    PALLET_FULL = "pallet.full"            # 托盘垛满 3×4×4
    AGV_CALL = "agv.call"                  # AGV 呼叫（班次2 接管）
    WH_INBOUND_DONE = "wh.inbound_done"    # 立体库入库完成
    WH_OUTBOUND_DONE = "wh.outbound_done"  # 立体库出库完成
    CLOCK_PAUSE = "clock.pause"            # 时钟暂停（预留班次2 UI）
    CLOCK_RESUME = "clock.resume"          # 时钟恢复
    DOOR_HOLD = "assembly.door_hold"       # 安全门开 → 装配单元顺控保持
    DOOR_RESUME = "assembly.door_resume"   # 安全门关 → 顺控恢复


class EventBus:
    """进程内发布/订阅总线 + JSONL 落盘。"""

    def __init__(self, clock, log_dir: str = "logs", persist: bool = True):
        """
        :param clock:   SimClock 实例，用于给每条事件盖仿真时间戳
        :param log_dir: JSONL 事件文件目录
        :param persist: 是否落盘（自检的纯逻辑用例可关闭）
        """
        self._clock = clock
        self._subs: Dict[str, List[dict]] = {}      # topic -> [{token,name,handler}]
        self._seq = 0                                # 全局事件序号（单调递增）
        self._lock = threading.RLock()
        self._recent = deque(maxlen=1000)            # 最近事件环形缓冲（UI 快照用）
        self._persist = persist
        self._fh = None                              # JSONL 文件句柄（懒打开）
        self._log_path: Optional[str] = None
        if persist:
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = os.path.join(log_dir, f"events_{stamp}.jsonl")

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def subscribe(self, topic: str, handler: Callable[[dict], None],
                  subscriber_name: str = "") -> int:
        """
        订阅主题。topic 可为精确主题或 "*"（接收全部事件，SCADA 网关就用它）。
        返回 token，用于 unsubscribe。
        """
        with self._lock:
            self._subs.setdefault(topic, []).append(
                {"token": self._next_token(), "name": subscriber_name or topic,
                 "handler": handler})
            return self._subs[topic][-1]["token"]

    def unsubscribe(self, token: int) -> None:
        """按 token 退订（幂等）。"""
        with self._lock:
            for topic, lst in list(self._subs.items()):
                self._subs[topic] = [s for s in lst if s["token"] != token]
                if not self._subs[topic]:
                    del self._subs[topic]

    @staticmethod
    def _next_token() -> int:
        """生成退订令牌（类级计数即可，无需严格全局唯一到重启之后）。"""
        if not hasattr(EventBus, "_token_counter"):
            EventBus._token_counter = 1000
        EventBus._token_counter += 1
        return EventBus._token_counter

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, source: str, etype: str, data: Optional[dict] = None,
                severity: str = "INFO") -> dict:
        """
        发布一条事件：写环形缓冲 → 落盘 JSONL → 分发给订阅者。
        订阅者异常被捕获打印，绝不影响仿真主流程（订阅者隔离原则）。
        """
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts_sim": round(self._clock.now(), 3),          # 仿真时间戳
                "ts_wall": datetime.now().isoformat(timespec="milliseconds"),
                "source": source,
                "type": etype,
                "severity": severity,
                "data": data or {},
            }
            self._recent.append(event)
        if self._persist:
            self._append_jsonl(event)
        # 分发（锁外执行回调，防止订阅者再 publish 造成死锁）
        with self._lock:
            handlers = list(self._subs.get(etype, [])) + list(self._subs.get("*", []))
        for sub in handlers:
            try:
                sub["handler"](event)
            except Exception as exc:  # 订阅者隔离：坏订阅者不拖垮全厂
                print(f"[EventBus] 订阅者 {sub['name']} 处理 {etype} 异常: {exc}")
        return event

    def _append_jsonl(self, event: dict) -> None:
        """把一条事件追加写入 JSONL 文件（每条即一行，追加模式）。"""
        with self._lock:
            if self._fh is None and self._log_path:
                # 行缓冲由 flush 保证；encoding 固定 UTF-8 避免中文乱码
                self._fh = open(self._log_path, "a", encoding="utf-8")
            if self._fh is not None:
                # default=str 兜底 numpy 标量等非常规类型
                self._fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                self._fh.flush()

    # ------------------------------------------------------------------
    # 查询接口（班次2 Web/API 直接复用）
    # ------------------------------------------------------------------
    def recent(self, n: int = 50, etype: Optional[str] = None) -> List[dict]:
        """取最近 n 条事件（可按类型过滤），供控制台/UI 展示。"""
        with self._lock:
            items = list(self._recent)
        if etype is not None:
            items = [e for e in items if e["type"] == etype]
        return items[-n:]

    def replay(self, filter_fn: Optional[Callable[[dict], bool]] = None) -> List[dict]:
        """按条件重放缓冲区中的历史事件（健康模块特征提取的入口）。"""
        with self._lock:
            items = list(self._recent)
        return [e for e in items if (filter_fn is None or filter_fn(e))]

    @property
    def log_path(self) -> Optional[str]:
        """当前 JSONL 文件路径（自检报告引用）。"""
        return self._log_path

    @property
    def total_published(self) -> int:
        """累计发布事件数。"""
        with self._lock:
            return self._seq

    def close(self) -> None:
        """关闭文件句柄（程序退出时调用）。"""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                finally:
                    self._fh = None


# ----------------------------------------------------------------------
# 自模块快速自检：python core/event_bus.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys
    # 路径引导：直接运行本文件时把项目根加入 sys.path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tempfile
    from core.sim_clock import SimClock

    tmpdir = tempfile.mkdtemp(prefix="vsf_bus_")
    clock = SimClock(dt=0.1)
    bus = EventBus(clock, log_dir=tmpdir)

    got_exact, got_all = [], []
    bus.subscribe(EventTypes.FAULT_RAISED, lambda e: got_exact.append(e), "测试-精确订阅")
    bus.subscribe("*", lambda e: got_all.append(e), "测试-通配订阅")

    e1 = bus.publish("TST-01", EventTypes.FAULT_RAISED, {"code": "E101"})
    clock.run_until(1.0)
    e2 = bus.publish("TST-01", EventTypes.VISION_NG, {"pid": "P00000001"})

    assert len(got_exact) == 1 and got_exact[0]["data"]["code"] == "E101"
    assert len(got_all) == 2, "通配订阅应收到全部事件"
    assert e2["seq"] > e1["seq"] and e2["ts_sim"] >= 1.0 - 1e-6, "时间戳/序号错误"

    # JSONL 落盘验证：行数应等于发布数，且能反序列化回字典
    bus.close()
    with open(bus.log_path, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert len(lines) == 2 and lines[-1]["type"] == EventTypes.VISION_NG, "JSONL 内容不符"
    assert len(bus.recent()) == 2 and len(bus.replay(lambda e: e["severity"] == "INFO")) == 2
    print(f"[event_bus 自检通过] 发布={bus.total_published} 条, "
          f"落盘={len(lines)} 行 → {bus.log_path} (仿真验证值)")

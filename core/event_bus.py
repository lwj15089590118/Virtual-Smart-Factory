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
    # ---- 班次2修改：追加 AGV 车队任务事件（只允许在此处扩展事件类型）----
    AGV_TASK_CREATED = "agv.task_created"  # AGV 任务建档（入库/出库）
    AGV_PHASE = "agv.phase"                # AGV 任务阶段迁移（空闲→去取货→装载→运输→交货→回位）
    AGV_TASK_DONE = "agv.task_done"        # AGV 任务闭环完成（入库交付库口/出库送达出货口）
    UI_COMMAND = "ui.command"              # 班次2：Web 大屏按钮命令审计（REST→Plant）

    # ---- 班次3修改：追加 MES / EMS 事件类型（仍遵守"只在此处扩展"的约定）----
    MES_ORDER_CREATED = "mes.order_created"  # 工单开立（含计划量，MES 报工数据源之一）
    MES_ORDER_CLOSED = "mes.order_closed"    # 工单满单关闭（自动翻单前落一条审计）
    EMS_HEALTH_ALERT = "ems.health_alert"    # 健康分跌破阈值告警（含评分与维护建议）
    EMS_MAINTENANCE = "ems.maintenance"      # 维护动作执行审计（enter_maintenance 触发留痕）

    # ---- 增强：AGV 回充排程事件（低电量调度全程可观测）----
    AGV_LOW_BATTERY = "agv.low_battery"      # 低电量触发回充决策（含当前电量%）
    AGV_CHARGE_START = "agv.charge_start"    # 到达充电位开始充电
    AGV_CHARGE_DONE = "agv.charge_done"      # 充电达标离开充电位

    # ---- 增强：装配单元有限料仓事件（原料供给侧全程可观测）----
    FEEDER_LOW = "feeder.low"                # 低水位告警（滞回：补到阈上才复位）
    FEEDER_EMPTY = "feeder.empty"            # 料仓清空，装配冻结于等待上料
    FEEDER_REFILL = "feeder.refill"          # 补料完成（含 added/stock/auto 标记）


class EventBus:
    """进程内发布/订阅总线 + JSONL 落盘（按大小轮转，保留最近 N 份）。"""

    def __init__(self, clock, log_dir: str = "logs", persist: bool = True,
                 rotate_mb: float = 50, keep: int = 3):
        """
        :param clock:   SimClock 实例，用于给每条事件盖仿真时间戳
        :param log_dir: JSONL 事件文件目录
        :param persist: 是否落盘（自检的纯逻辑用例可关闭）
        :param rotate_mb: 单文件大小上限（MB），超过即轮转；<=0 关闭轮转。
                          审查修复（报告13-P1-2）：此前无轮转，30 日 soak 单文件 155MB、
                          历史运行累计 498MB——现场系统必须有保留策略。
        :param keep:    轮转后保留的历史文件份数（events_x.jsonl.1 ~ .keep），
                        超出自动删除最旧。
        """
        self._clock = clock
        self._subs: Dict[str, List[dict]] = {}      # topic -> [{token,name,handler}]
        self._seq = 0                                # 全局事件序号（单调递增）
        self._lock = threading.RLock()
        self._recent = deque(maxlen=1000)            # 最近事件环形缓冲（UI 快照用）
        self._persist = persist
        self._rotate_mb = float(rotate_mb)           # 允许小数 MB（自检用极小阈值触发轮转）
        self._keep = max(1, int(keep))
        self._fh = None                              # JSONL 文件句柄（懒打开）
        self._log_path: Optional[str] = None
        self._io_warned = False                      # 落盘 IO 异常只告警一次（防刷屏）
        if persist:
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = os.path.join(log_dir, f"events_{stamp}.jsonl")
            # 班次2修改：自检B2/B3/冒烟会在同一秒内连续创建多个总线实例，
            # 追加模式会撞名混写——存在同名文件时追加序号后缀，保证实例独占文件。
            _n = 1
            while os.path.exists(self._log_path):
                self._log_path = os.path.join(log_dir,
                                              f"events_{stamp}_{_n}.jsonl")
                _n += 1

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
        """把一条事件追加写入 JSONL 文件（每条即一行，追加模式）。

        审查修复（报告13-P1-2）：整段落盘动作做异常兜底——磁盘满/IO 错误只降级为
        丢弃该条并告警一次，绝不允许异常抛回 publish() 杀死仿真线程；
        单文件超过 rotate_mb 即轮转（必须持锁调用，保证线程安全）。
        """
        with self._lock:
            try:
                if self._fh is None and self._log_path:
                    # 行缓冲由 flush 保证；encoding 固定 UTF-8 避免中文乱码
                    self._fh = open(self._log_path, "a", encoding="utf-8")
                if self._fh is not None:
                    # default=str 兜底 numpy 标量等非常规类型
                    self._fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                    self._fh.flush()
                    if (self._rotate_mb > 0
                            and self._fh.tell() >= self._rotate_mb * 1048576):
                        self._rotate_locked()
            except Exception as exc:
                self._on_io_error(exc)

    def _rotate_locked(self) -> None:
        """按大小轮转（前置条件：已持有 self._lock 且当前文件超限）：
        历史份依次后移（.2→.3 …），当前文件改名 .1；超出 keep 份的最旧文件被覆盖删除。
        轮转失败只打印告警——磁盘满时宁缺日志不可停仿真。"""
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
        try:
            for i in range(self._keep - 1, 0, -1):
                src, dst = f"{self._log_path}.{i}", f"{self._log_path}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)            # 覆盖式改名：最旧一份随之被淘汰
            os.replace(self._log_path, f"{self._log_path}.1")
        except OSError as exc:
            print(f"[EventBus] 日志轮转失败(磁盘满/文件占用?): {exc}")

    def _on_io_error(self, exc: Exception) -> None:
        """落盘 IO 异常兜底：关句柄并只告警一次（磁盘恢复后下一条事件自动重开续写）。"""
        self._fh = None
        if not self._io_warned:
            self._io_warned = True
            print(f"[EventBus] 事件落盘异常已降级(仅告警一次，不影响仿真): "
                  f"{exc.__class__.__name__}: {exc}")

    # ------------------------------------------------------------------
    # 查询接口（班次2 Web/API 直接复用）
    # ------------------------------------------------------------------
    def recent(self, n: int = 50, etype: Optional[str] = None) -> List[dict]:
        """取最近 n 条事件（可按类型过滤），供控制台/UI 展示。
        防御性钳制（审查修复 报告13-P2-2）：n<1 会使 items[-n:] 负负得正
        泄出几乎全量环形缓冲，这里统一钳到 1（Web 层再把 <=0 归位默认值）。"""
        n = max(1, int(n))
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

    # 轮转回归（审查修复 P1-2）：阈值压到约 512B，连发 20 条应触发多次轮转，
    # 磁盘上最多保留 活动文件 + keep 份历史
    bus_rot = EventBus(clock, log_dir=tmpdir, persist=True,
                       rotate_mb=0.0005, keep=2)
    for i in range(20):
        bus_rot.publish("TST-ROT", EventTypes.DEVICE_STATE,
                        {"i": i, "pad": "X" * 40})
    bus_rot.close()
    import glob as _glob
    segs = sorted(_glob.glob(bus_rot.log_path + "*"))
    assert len(segs) == 3, f"轮转应保留 活动文件+2 份历史: {segs}"
    with open(bus_rot.log_path, encoding="utf-8") as f:
        rot_lines = [json.loads(x) for x in f if x.strip()]
    assert 1 <= len(rot_lines) < 20, f"活动文件应只含最近一批事件: {len(rot_lines)}"
    total_rot = sum(1 for s in segs
                    for x in open(s, encoding="utf-8") if x.strip())
    assert 0 < total_rot <= 20, f"轮转份内事件数异常: {total_rot}"
    assert rot_lines[-1]["data"]["i"] == 19, "活动文件应包含最近一条事件"

    # 负数 n 防御（审查修复 P2-2）：recent(-5) 不得负负得正泄出全量缓冲
    assert len(bus.recent(-5)) <= 1, "recent 负数 n 未被钳制"

    print(f"[event_bus 自检通过] 发布={bus.total_published} 条, "
          f"落盘={len(lines)} 行, 轮转={len(segs)}文件 → {bus.log_path} (仿真验证值)")

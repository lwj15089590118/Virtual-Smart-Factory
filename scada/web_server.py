# -*- coding: utf-8 -*-
"""
scada/web_server.py —— SCADA Web 服务（Flask REST + WebSocket 实时推送）
=========================================================================
职责（班次2 交付项1）：
    1. REST API（端口 settings.SCADA_HTTP_PORT）：
       GET  /api/status                 全厂状态快照（各单元/故障/AGV/时钟）
       GET  /api/kpi                    KPI 指标 + 产量/NG 趋势序列（仿真验证值）
       GET  /api/events?n=50&type=      最近事件查询（EventBus.recent）
       GET  /api/pallet3d               当前垛型毫米坐标（ECharts bar3D 数据源）
       GET  /api/warehouse/locations    全库位表（热力图数据源，200 格）
       GET  /api/modbus/map             Modbus 保持寄存器映射表（点表文档化）
       POST /api/command {"cmd":...}    大屏按钮命令 → Plant 公开方法（带审计事件）
       ---- 班次3修改：MES/EMS 扩展路由 ----
       GET  /api/mes/orders             工单台账 + 报工报表（产量/良率/OEE 仿真验证值）
       GET  /api/mes/batches            批次台账（?wo_id= 过滤）
       GET  /api/mes/trace?query=       产品/托盘全链路追溯反查
       GET  /api/ems/energy             全厂能耗快照（kWh/电费/CO₂ 仿真验证值）
       GET  /api/ems/health             设备健康评分 + 维护建议
    2. WebSocket 推送（端口 settings.SCADA_WS_PORT，scada/ws_hub.py 实现）：
       订阅 EventBus 通配符 "*"，每条事件实时 JSON 群发给在线大屏；
    3. 静态页面：web/static/index.html + app.js + style.css（ECharts CDN）。

线程模型假设（一行记录）：
    - Flask 开发服务器以 daemon 线程运行(threaded=True)，作品集演示足够；
    - 命令接口只调用 Plant 公开方法做原子动作，与仿真 tick 的竞态由
      GIL 与幂等设计兜底——这是全软件仿真的稳妥取舍。
"""

import json
import os
import sys
import threading
from collections import OrderedDict
from typing import Dict, List, Optional

# 路径引导：直接运行本文件时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, send_from_directory

from core.event_bus import EventBus, EventTypes
from config import settings as S
from scada.ws_hub import WsHub

# 项目根目录（web/static 就在里面；不依赖启动时的工作目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_STATIC_DIR = os.path.join(PROJECT_ROOT, "web", "static")


# ======================================================================
# KPI 趋势记录器：把事件流按仿真时间桶聚合（产量/OK/NG/故障 四条曲线）
# ======================================================================
class TrendRecorder:
    """按 KPI_BUCKET_S 仿真秒分桶累计关键计数，供前端折线图。"""

    _FIELD_OF = {
        EventTypes.PRODUCT_OUT: "out",
        EventTypes.VISION_OK: "ok",
        EventTypes.VISION_NG: "ng",
        EventTypes.FAULT_RAISED: "faults",
    }

    def __init__(self, bus: EventBus):
        self._buckets: "OrderedDict[float, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self._tokens = [bus.subscribe(etype, self._on_event, "KPI趋势")
                        for etype in self._FIELD_OF]

    def _on_event(self, event: dict) -> None:
        field = self._FIELD_OF.get(event["type"])
        if field is None:
            return
        bucket_t = int(event["ts_sim"] // S.KPI_BUCKET_S) * S.KPI_BUCKET_S
        with self._lock:
            if bucket_t not in self._buckets:
                self._buckets[bucket_t] = {"t": bucket_t, "out": 0,
                                           "ok": 0, "ng": 0, "faults": 0}
                self._buckets.move_to_end(bucket_t)
                while len(self._buckets) > S.KPI_BUCKET_MAX:   # 滚动窗口防膨胀
                    self._buckets.popitem(last=False)
            self._buckets[bucket_t][field] += 1

    def series(self) -> List[dict]:
        """导出趋势序列（按时间升序）。"""
        with self._lock:
            return list(self._buckets.values())

    def close(self) -> None:
        for tk in self._tokens:
            pass                    # token 已由总线持有；停服随进程结束即可


# ======================================================================
# Web 服务主体
# ======================================================================
class ScadaWebServer:
    """把一个 Plant 实例暴露为 SCADA Web 端（REST + WS 推送）。"""

    def __init__(self, plant):
        self.plant = plant
        self.bus: EventBus = plant.bus
        # ---- WebSocket 推送网关（独立端口）----
        ws_host = "127.0.0.1" if S.SCADA_HTTP_HOST == "127.0.0.1" else "0.0.0.0"
        self.hub = WsHub(ws_host, S.SCADA_WS_PORT)
        # ---- 事件通配符订阅 → WS 广播（交付要求：订阅事件总线通配符）----
        self._ws_token = self.bus.subscribe("*", self._on_any_event,
                                            "WS推送网关")
        # ---- KPI 趋势 ----
        self.trend = TrendRecorder(self.bus)
        # ---- Flask 应用 ----
        self.app = Flask(__name__, static_folder=WEB_STATIC_DIR,
                         static_url_path="/static")
        self._register_routes()
        self._flask_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 WS 网关与 Flask 线程（重复调用安全）。"""
        if self._running:
            return
        self._running = True
        self.hub.start()
        self._flask_thread = threading.Thread(
            target=self._serve_http, name="ScadaWebThread", daemon=True)
        self._flask_thread.start()

    def stop(self) -> None:
        """停机：退订总线、关 WS 网关（Flask daemon 线程随主进程退出）。"""
        self._running = False
        self.bus.unsubscribe(self._ws_token)
        self.hub.stop()

    def _serve_http(self) -> None:
        try:
            # 假设记录：Flask 内置服务器(threaded=True)满足演示并发；生产应换 waitress/gunicorn
            self.app.run(host=S.SCADA_HTTP_HOST, port=S.SCADA_HTTP_PORT,
                         debug=False, use_reloader=False, threaded=True)
        except OSError as exc:
            print(f"[SCADA-Web] HTTP 服务启动失败(端口{S.SCADA_HTTP_PORT}被占用?): {exc}")

    def info(self) -> str:
        """启动横幅信息。"""
        return (f"SCADA Web: http://127.0.0.1:{S.SCADA_HTTP_PORT}  |  "
                f"WebSocket: ws://127.0.0.1:{S.SCADA_WS_PORT}{self.hub.path}  |  "
                f"Modbus TCP: 127.0.0.1:{S.MODBUS_TCP_PORT}")

    # ------------------------------------------------------------------
    # WS 广播回调
    # ------------------------------------------------------------------
    def _on_any_event(self, event: dict) -> None:
        self.hub.broadcast({"kind": "event", "event": event})

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        app = self.app
        plant = self.plant

        # ---------- 页面 ----------
        @app.route("/")
        def page_index():
            return send_from_directory(WEB_STATIC_DIR, "index.html")

        # ---------- 全厂状态 ----------
        @app.route("/api/status")
        def api_status():
            p = plant
            fleet_snap = (p.agv_fleet.snapshot()
                          if getattr(p, "agv_fleet", None) else None)
            return jsonify({
                "ok": True,
                "ts_sim": p.clock.now(),
                "mode": p.mode,
                "line_estop": p.line_estop_latched,
                "clock": {
                    "now": p.clock.now(), "speed": p.clock.speed,
                    "paused": p.clock.is_paused(),
                    "dt": p.clock.dt, "ticks": p.clock.tick_count,
                },
                "units": {
                    "assembly": p.assembly.snapshot(),
                    "vision": p.vision.snapshot(),
                    "palletizer": p.palletizer.snapshot(),
                    "warehouse": p.warehouse.snapshot(),
                },
                "injector": p.injector.snapshot(),
                "agv_fleet": fleet_snap,
                "ws_clients": self.hub.client_count(),
            })

        # ---------- KPI 指标 + 趋势（全部为仿真验证值）----------
        @app.route("/api/kpi")
        def api_kpi():
            a = plant.assembly.snapshot()
            v = plant.vision.snapshot()
            pal = plant.palletizer.snapshot()
            w = plant.warehouse.snapshot()
            inj = plant.injector.snapshot()
            fleet = (plant.agv_fleet.snapshot()
                     if getattr(plant, "agv_fleet", None) else {})
            now = plant.clock.now()
            judged = v["ok"] + v["ng"]
            kpi = {
                "ts_sim": now,
                "note": "所有指标均为仿真验证值",
                "products_out": a["products_out"],          # 装配流出（件）
                "judged": judged,                            # 已判定（件）
                "ok": v["ok"], "ng": v["ng"],
                "ng_rate_pct": round(v["ng_rate"] * 100, 2),  # NG率 %
                "boxes_total": pal["boxes"],                 # 累计码箱
                "pallets_done": pal["pallets_done"],         # 完成托
                "current_fill": pal["current_fill"],         # 当前垛 n/48
                "stock": w["stock"], "capacity": w["capacity"],
                "inbound_done": w.get("inbound_done", 0),
                "outbound_done": w.get("outbound_done", 0),
                "shipped": fleet.get("shipped", 0),          # AGV 送抵出货口托数
                "agv_done": fleet.get("done", {}),
                "agv_pending": fleet.get("pending", 0),
                "faults_total": inj.get("injected_total", 0),
                "faults_active": len(inj.get("active", [])),
                "takt_s": a["takt_s"],
                "uptime_s": round(now, 1),
            }
            # 单元可用率（近似口径）：运行秒数 / 开机秒数（仿真验证值）
            kpi["availability"] = {}
            for key, snap in (("assembly", a), ("vision", v),
                              ("palletizer", pal), ("warehouse", w)):
                kpi["availability"][key] = round(
                    snap["run_seconds"] / now * 100, 1) if now > 0 else 100.0
            return jsonify({"ok": True, "kpi": kpi,
                            "trend": self.trend.series()})

        # ---------- 事件查询 ----------
        @app.route("/api/events")
        def api_events():
            try:
                n = min(int(request.args.get("n", 50)), 500)
            except ValueError:
                n = 50
            etype = request.args.get("type") or None
            return jsonify({"ok": True,
                            "events": self.bus.recent(n, etype)})

        # ---------- 垛型 3D 数据 ----------
        @app.route("/api/pallet3d")
        def api_pallet3d():
            pal = plant.palletizer
            done = pal.pallets_done[-1] if pal.pallets_done else None
            return jsonify({
                "ok": True,
                "pitch_mm": list(S.BOX_PITCH_MM),
                "dims": [S.PALLET_X, S.PALLET_Y, S.PALLET_Z],
                "capacity": pal.pallet_capacity,
                "current_pallet_id": pal.current_pallet_id(),
                "grid": pal.current_grid(),               # 正在码的垛（实时生长）
                "last_completed": done,                   # 最近满托档案
            })

        # ---------- 库位热力图数据 ----------
        @app.route("/api/warehouse/locations")
        def api_wh_locations():
            w = plant.warehouse
            return jsonify({
                "ok": True,
                "rows": S.WH_ROWS, "bays": S.WH_BAYS, "levels": S.WH_LEVELS,
                "capacity": w.capacity, "stock": w.stock_count,
                "locations": w.locations(),
            })

        # ---------- Modbus 寄存器映射表 ----------
        @app.route("/api/modbus/map")
        def api_modbus_map():
            from scada.modbus_server import build_register_map  # 局部导入避免环依赖
            return jsonify({"ok": True, "port": S.MODBUS_TCP_PORT,
                            "unit_id": S.MODBUS_UNIT_ID,
                            "map": build_register_map(plant.devices)})

        # ---------- 命令入口（大屏按钮 → Plant 公开方法）----------
        @app.route("/api/command", methods=["POST"])
        def api_command():
            body = request.get_json(silent=True) or {}
            cmd = str(body.get("cmd", ""))
            params = body.get("params") or {}
            result = plant.execute_command(cmd, params if isinstance(params, dict) else {})
            # 命令审计落总线（JSONL 同步留痕，班次3/MES 可追溯操作者动作）
            self.bus.publish("SCADA-WEB", EventTypes.UI_COMMAND,
                             {"cmd": cmd, "params": params,
                              "ok": bool(result.get("ok"))},
                             severity="INFO")
            code = 200 if result.get("ok") else 400
            return jsonify(result), code

        # ==============================================================
        # 班次3修改：MES / EMS 扩展路由（沿用现有 jsonify+ok 字段风格）
        # ==============================================================
        @app.route("/api/mes/orders")
        def api_mes_orders():
            """工单台账 + 报工报表（MES 未启用时返回空台账而非报错）。"""
            mes = getattr(plant, "mes", None)
            if mes is None:
                return jsonify({"ok": True, "enabled": False,
                                "orders": [], "report": None})
            return jsonify({"ok": True, "enabled": True,
                            "orders": mes.snapshot_orders(),
                            "report": mes.report()})

        @app.route("/api/mes/batches")
        def api_mes_batches():
            """批次台账（可 ?wo_id= 过滤）。"""
            mes = getattr(plant, "mes", None)
            if mes is None:
                return jsonify({"ok": True, "enabled": False, "batches": []})
            wo_id = request.args.get("wo_id") or None
            return jsonify({"ok": True, "enabled": True,
                            "batches": mes.snapshot_batches(wo_id=wo_id)})

        @app.route("/api/mes/trace")
        def api_mes_trace():
            """全链路追溯反查：?query=产品号或托盘号。"""
            mes = getattr(plant, "mes", None)
            query = request.args.get("query", "")
            if mes is None:
                return jsonify({"ok": False, "msg": "MES 引擎未启用"}), 400
            hit = mes.trace(query)
            if hit is None:
                return jsonify({"ok": False,
                                "msg": f"未找到与 '{query}' 相关的追溯记录"}), 404
            return jsonify({"ok": True, **hit})

        @app.route("/api/ems/energy")
        def api_ems_energy():
            """全厂能耗快照（kWh/电费/CO₂，仿真验证值）。"""
            em = getattr(plant, "ems_energy", None)
            if em is None:
                return jsonify({"ok": True, "enabled": False, "devices": []})
            return jsonify({"ok": True, "enabled": True, **em.snapshot()})

        @app.route("/api/ems/health")
        def api_ems_health():
            """全厂健康评分（0~100 + 维护建议，仿真验证值）。"""
            hm = getattr(plant, "ems_health", None)
            if hm is None:
                return jsonify({"ok": True, "enabled": False, "devices": []})
            return jsonify({"ok": True, "enabled": True, **hm.snapshot()})

        # 静态资源禁用强缓存：前端迭代期保证浏览器刷新即得最新脚本/样式
        @app.after_request
        def _no_store(resp):
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp

        # 兜底：未知 API 返回 JSON 404（避免前端拿到 HTML 报错难排查）
        @app.errorhandler(404)
        def not_found(_e):
            return jsonify({"ok": False, "msg": "接口不存在"}), 404


# ----------------------------------------------------------------------
# 独立冒烟入口：python scada/web_server.py
# 起 3 秒真实 HTTP+WS 服务，用 urllib 自访 REST（不依赖第三方测试库）
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import time as wt
    import urllib.request

    from main import Plant

    plant = Plant(speed=60, mode="fast", seed=S.DEFAULT_SEED)
    plant.build()
    plant.start_up_all()
    srv = ScadaWebServer(plant)
    srv.start()
    print(f"[SCADA-Web 冒烟] {srv.info()}")
    plant.clock.run_until(40.0)                       # 先跑出一点数据

    base = f"http://127.0.0.1:{S.SCADA_HTTP_PORT}"
    deadline = wt.time() + 5.0                        # 墙钟仅用于等待服务就绪
    while True:
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
                assert r.status == 200
            break
        except Exception:
            if wt.time() > deadline:
                raise
            wt.sleep(0.2)

    for path in ("/api/kpi", "/api/events?n=10", "/api/pallet3d",
                 "/api/warehouse/locations", "/api/modbus/map",
                 "/api/mes/orders", "/api/mes/batches",
                 "/api/ems/energy", "/api/ems/health"):   # 班次3修改：新路由纳入冒烟
        with urllib.request.urlopen(base + path, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            assert data.get("ok") is True, f"{path} 返回异常"
    req = urllib.request.Request(
        base + "/api/command",
        data=json.dumps({"cmd": "door_open"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        ret = json.loads(r.read().decode("utf-8"))
    assert ret.get("ok") is True, f"命令下发失败: {ret}"
    print("[SCADA-Web 冒烟通过] REST 十端点 + 命令链路全部 200 (仿真验证值)")
    srv.stop()

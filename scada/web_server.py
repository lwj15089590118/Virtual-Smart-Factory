# -*- coding: utf-8 -*-
"""
scada/web_server.py —— SCADA Web 服务（Flask REST + WebSocket 实时推送）
=========================================================================
职责（班次2 交付项1）：
    1. REST API（端口 settings.SCADA_HTTP_PORT）：
       GET  /api/config                 前端探测是否启用命令口令（公开布尔位，无口令本体）
       GET  /api/status                 全厂状态快照（各单元/故障/AGV/时钟）
       GET  /api/kpi                    KPI 指标 + 产量/NG 趋势序列（仿真验证值）
       GET  /api/events?n=50&type=      最近事件查询（EventBus.recent）
       GET  /api/pallet3d               当前垛型毫米坐标（ECharts bar3D 数据源）
       GET  /api/warehouse/locations    全库位表（热力图数据源，200 格）
       GET  /api/modbus/map             Modbus 保持寄存器映射表（点表文档化）
       POST /api/command {"cmd":...}    大屏按钮命令 → Plant 公开方法（带审计事件；
                                        审查修复：命令口令校验，见 SCADA_API_TOKEN）
       ---- 班次3修改：MES/EMS 扩展路由 ----
       GET  /api/mes/orders             工单台账 + 报工报表（产量/良率/OEE 仿真验证值）
       GET  /api/mes/batches            批次台账（?wo_id= 过滤）
       GET  /api/mes/trace?query=       产品/托盘全链路追溯反查
       GET  /api/mes/qc_log             质检判定流水（增强：SQLite 台账查询，可组合过滤）
       GET  /api/ems/energy             全厂能耗快照（kWh/电费/CO₂ 仿真验证值）
       GET  /api/ems/health             设备健康评分 + 维护建议
       （复审修补 复审报告13 二轮 P2-2：非回环绑定+已配置口令时，全部 GET 数据
         端点要求 X-Auth-Token 头，由 before_request 守卫统一拦截，401 口径一致）
    2. WebSocket 推送（端口 settings.SCADA_WS_PORT，scada/ws_hub.py 实现）：
       订阅 EventBus 通配符 "*"，每条事件实时 JSON 群发给在线大屏；
    3. 静态页面：web/static/index.html + app.js + style.css（ECharts CDN）。

线程模型假设（一行记录）：
    - Flask 开发服务器以 daemon 线程运行(threaded=True)，作品集演示足够；
    - 命令接口只调用 Plant 公开方法做原子动作，与仿真 tick 的竞态由
      GIL 与幂等设计兜底——这是全软件仿真的稳妥取舍。
"""

import hmac
import ipaddress
import json
import os
import sys
import threading
from collections import OrderedDict
from typing import List, Optional

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
        self._bus = bus
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
        """退订总线（审查修复 报告13-P2-6：此前空实现从未退订——单次运行无碍，
        进程内重建 Web 服务会重复累计趋势计数）。幂等，可重复调用。"""
        for tk in self._tokens:
            self._bus.unsubscribe(tk)
        self._tokens.clear()


# ======================================================================
# Web 服务主体
# ======================================================================
class ScadaWebServer:
    """把一个 Plant 实例暴露为 SCADA Web 端（REST + WS 推送）。"""

    def __init__(self, plant, host: Optional[str] = None):
        """
        :param host: 三协议(HTTP/WS)监听地址；None=跟随 settings.SCADA_HTTP_HOST。
            审查修复（报告13-P1-1）：默认 127.0.0.1 仅本机；局域网演示由 CLI
            --host 0.0.0.0 显式开放（main.py 传参，不在运行期改写 config）。
        """
        self.plant = plant
        self.host = host or S.SCADA_HTTP_HOST
        self.bus: EventBus = plant.bus
        # ---- 审查修复：命令口令（环境变量 SCADA_API_TOKEN 优先，其次 settings）----
        self.command_token = os.environ.get("SCADA_API_TOKEN") or S.SCADA_API_TOKEN
        # ---- 复审修补（复审报告13 二轮 P2-2）：读面鉴权开关 ----
        # 非回环绑定(0.0.0.0/局域网IP/空串) 且已配置口令 → WS 握手与全部 /api
        # GET 数据端点一并要求凭证；回环绑定（默认）保持旧行为——GET/WS 匿名，
        # 仅 POST /api/command 受口令保护，确保既有部署/selftest 行为不变。
        self.loopback_bind = self._is_loopback_host(self.host)
        self.read_auth_required = bool(self.command_token) and not self.loopback_bind
        # ---- WebSocket 推送网关（独立端口；与 HTTP 同地址绑定）----
        # 需要读面鉴权时把口令交给 WS 握手校验（?token= 查询参数或 X-Auth-Token 头）
        self.hub = WsHub(self.host, S.SCADA_WS_PORT,
                         auth_token=(self.command_token
                                     if self.read_auth_required else ""))
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

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        """绑定地址是否为回环（127.x/::1/localhost）；空串/0.0.0.0/:: 视为全网卡开放。"""
        h = (host or "").strip().lower()
        if h in ("", "0.0.0.0", "::", "*"):
            return False
        if h == "localhost":
            return True
        try:
            return ipaddress.ip_address(h).is_loopback
        except ValueError:
            return False

    def _print_security_notices(self) -> None:
        """启动横幅安全提示（start() 调用；独立成方法便于测试断言）。"""
        if not self.command_token:
            # 审查修复（报告13-P1-1）：未配置口令时明确告知暴露面收窄策略
            print("[SCADA-Web] 安全提示: 未配置命令口令(SCADA_API_TOKEN)，"
                  "POST /api/command 仅允许本机(127.0.0.1)请求，远程主机一律拒绝；"
                  "如需远程下发命令请设置环境变量 SCADA_API_TOKEN 并携带 X-Auth-Token 头")
            # 复审修补（复审报告13 二轮 P2-2）：显式开放监听却无口令 → 读面裸奔，显著警告
            if not self.loopback_bind:
                print(f"[SCADA-Web] ⚠ 安全警告: 绑定地址 {self.host} 为非回环且未配置口令——"
                      "WS 事件流与全部只读 API(/api/*) 对局域网匿名可读，"
                      "仅限隔离演示网络使用；建议设置 SCADA_API_TOKEN 启用全接口鉴权")
        elif self.read_auth_required:
            # 复审修补（复审报告13 二轮 P2-2）：全接口鉴权已启用的明示
            print(f"[SCADA-Web] 安全提示: 已启用命令口令且绑定非回环地址({self.host})——"
                  "GET/WS 读取面同样要求凭证（HTTP 头 X-Auth-Token；"
                  "WS 握手用 ?token= 查询参数或同名请求头）")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 WS 网关与 Flask 线程（重复调用安全）。"""
        if self._running:
            return
        self._running = True
        self._print_security_notices()
        self.hub.start()
        self._flask_thread = threading.Thread(
            target=self._serve_http, name="ScadaWebThread", daemon=True)
        self._flask_thread.start()

    def stop(self) -> None:
        """停机：退订总线、关 WS 网关（Flask daemon 线程随主进程退出）。"""
        self._running = False
        self.bus.unsubscribe(self._ws_token)
        self.trend.close()                  # 审查修复：KPI 趋势记录器一并退订
        self.hub.stop()

    def _serve_http(self) -> None:
        try:
            # 假设记录：Flask 内置服务器(threaded=True)满足演示并发；生产应换 waitress/gunicorn
            self.app.run(host=self.host, port=S.SCADA_HTTP_PORT,
                         debug=False, use_reloader=False, threaded=True)
        except OSError as exc:
            print(f"[SCADA-Web] HTTP 服务启动失败(端口{S.SCADA_HTTP_PORT}被占用?): {exc}")

    def info(self) -> str:
        """启动横幅信息。"""
        return (f"SCADA Web: http://{self.host}:{S.SCADA_HTTP_PORT}  |  "
                f"WebSocket: ws://{self.host}:{S.SCADA_WS_PORT}{self.hub.path}  |  "
                f"Modbus TCP: {self.host}:{S.MODBUS_TCP_PORT}")

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

        # ---------- 前端配置探测（公开，复审修补 复审报告13 二轮 P2-1）----------
        @app.route("/api/config")
        def api_config():
            """大屏加载时先探测是否启用了命令口令：启用则提示输入一次令牌
            （localStorage 记忆）并随请求携带 X-Auth-Token。
            只暴露"是否启用"布尔位与凭证形态，绝不含口令本体。"""
            return jsonify({"ok": True,
                            "auth_required": bool(self.command_token),
                            "token_header": "X-Auth-Token"})

        # ---------- 读面鉴权守卫（复审修补 复审报告13 二轮 P2-2）----------
        # 仅当「已配置口令 且 绑定非回环」时生效：全部 /api GET 数据端点要求
        # X-Auth-Token；静态页面放行（浏览器须先加载页面才能输入令牌）；
        # /api/config 放行（公开探测端点）。回环绑定（默认）不进入此分支，
        # 行为与既往完全一致；POST /api/command 由路由内部自行鉴权。
        @app.before_request
        def _guard_read_face():
            if not self.read_auth_required:
                return None
            if request.method not in ("GET", "HEAD"):
                return None
            if not request.path.startswith("/api/") or request.path == "/api/config":
                return None
            supplied = request.headers.get("X-Auth-Token", "")
            if not supplied or not hmac.compare_digest(supplied, self.command_token):
                return jsonify({"ok": False,
                                "msg": "读取被拒绝：服务以口令+非回环绑定运行，"
                                       "GET 数据端点要求 X-Auth-Token 头"}), 401
            return None

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
            # 审查修复（报告13-P2-2）：n<=0 钳制回默认值 50——此前负数穿透到
            # EventBus.recent 的 items[-n:] 负负得正，几乎泄出全量环形缓冲
            try:
                n = int(request.args.get("n", 50))
            except ValueError:
                n = 50
            if n <= 0:
                n = 50
            n = min(n, 500)
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
            # ---- 审查修复（报告13-P1-1）：命令鉴权 ----
            # 口令已配置（环境变量 SCADA_API_TOKEN 或 settings）：所有请求（含本机）
            # 必须携带 X-Auth-Token 头，口径统一无旁路；
            # 口令未配置：仅信任本机(127.0.0.1/::1)请求，远程一律 403（启动横幅有提示）。
            # 复审修补（复审报告13 二轮 P2-2）：改 hmac.compare_digest 常数时间比较，
            # 防令牌逐字节时序侧信道（与非回环读面鉴权同一口径）。
            if self.command_token:
                supplied = request.headers.get("X-Auth-Token", "")
                if not supplied or not hmac.compare_digest(supplied, self.command_token):
                    return jsonify({"ok": False,
                                    "msg": "命令被拒绝：X-Auth-Token 缺失或不匹配"}), 401
            elif request.remote_addr not in ("127.0.0.1", "::1"):
                return jsonify({"ok": False,
                                "msg": "命令被拒绝：服务端未配置 SCADA_API_TOKEN，"
                                       "仅允许本机下发命令"}), 403
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

        # ---- 增强：质检判定流水查询（SQLite 台账，mes/sqlite_ledger.py）----
        @app.route("/api/mes/qc_log")
        def api_mes_qc_log():
            """
            质检判定流水（每件一行，id 倒序=最新在前）。过滤参数（均可组合）：
                ?limit=50        返回条数上限（1~500，默认50）
                ?result=OK|NG    按判定结果过滤
                ?wo_id=WO-0002   按报工归属工单过滤
                ?product_id=P..  按产品号精确过滤
                ?run_id=...      按运行批次过滤（缺省跨批次取最新）
            台账未启用/未落库时返回 enabled=False 空列表（与 /api/ems/* 风格一致）。
            """
            mes = getattr(plant, "mes", None)
            led = getattr(mes, "ledger", None) if mes is not None else None
            if led is None:
                return jsonify({"ok": True, "enabled": False,
                                "rows": [], "count": 0})
            try:
                limit = int(request.args.get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 500))
            rows = led.query_qc(
                limit=limit,
                result=request.args.get("result") or None,
                wo_id=request.args.get("wo_id") or None,
                product_id=request.args.get("product_id") or None,
                run_id=request.args.get("run_id") or None)
            return jsonify({"ok": True, "enabled": True,
                            "count": len(rows), "rows": rows,
                            "db_path": led.db_path, "run_id": led.run_id})

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

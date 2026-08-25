# -*- coding: utf-8 -*-
"""
plc_refill_bridge.py —— OpenPLC 梯形图 → 虚拟产线 的补料请求桥接器
====================================================================
角色（配合 docs/TUTORIAL_MCGS_OPENPLC.md 第二章/第三章）：

    OpenPLC Runtime(从站:5020)          本脚本(主站)            产线 REST
    ┌─────────────────────┐   FC1读线圈   ┌────────────┐  POST      ┌─────────┐
    │ %QX0.0 补料请求线圈  │ ◀────────── │ plc_refill  │ ────────▶ │ /api/   │
    │ (梯形图低料位置位)    │              │ _bridge.py  │ feeder_   │ command │
    └─────────────────────┘              └────────────┘ refill    └─────────┘

行为：
    每秒轮询一次 OpenPLC 线圈 %QX0.0（地址 0）；检测【上升沿】（FALSE→TRUE）
    即向产线 REST 发一次 feeder_refill 命令——补料走正式命令链路，
    自动落 ui.command 审计事件，与 Web 按钮/MCGS 写入同源可追溯。

用法：
    python plc_refill_bridge.py                 # 默认参数常驻运行
    python plc_refill_bridge.py --once          # 只跑一轮轮询（调试链路用）
    python plc_refill_bridge.py --plc-port 5020 --coil 0 --rest http://127.0.0.1:5080

依赖：pymodbus（requirements.txt 已含）。墙钟仅用于轮询节拍——本脚本是
独立的外部设备模拟器，不触碰任何仿真状态计算，时间纪律不受影响。

假设记录：
    - OpenPLC 从站端口默认 502 与特权端口冲突，教程要求改为 5020；
    - 线圈语义由梯形图决定：置位=请求补料，撤销=料位已恢复（滞回在梯形图里实现）。
"""

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pymodbus.client import ModbusTcpClient


def parse_args():
    p = argparse.ArgumentParser(description="OpenPLC→VSF 补料请求桥接器")
    p.add_argument("--plc-ip", default="127.0.0.1", help="OpenPLC Runtime 地址")
    p.add_argument("--plc-port", type=int, default=5020,
                   help="OpenPLC Modbus 从站端口（教程约定 5020）")
    p.add_argument("--unit", type=int, default=1, help="OpenPLC 从站单元号")
    p.add_argument("--coil", type=int, default=0,
                   help="补料请求线圈地址（梯形图 %%QX0.0 → 0）")
    p.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒")
    p.add_argument("--rest", default="http://127.0.0.1:5080",
                   help="产线 REST 根地址")
    p.add_argument("--once", action="store_true", help="只执行一轮轮询后退出")
    return p.parse_args()


def read_coil(cli, args):
    """FC1 读线圈；连接失败返回 None（不抛出，交由主循环打印告警）。"""
    try:
        if not cli.connected and not cli.connect():
            return None
        rr = cli.read_coils(args.coil, count=1, slave=args.unit)
        if rr is None or getattr(rr, "isError", lambda: True)():
            return None
        return bool(rr.bits[0])
    except Exception as exc:
        print(f"[bridge] 读线圈异常: {exc}", flush=True)
        return None


def send_refill(rest_base: str) -> tuple:
    """POST feeder_refill 命令；返回 (是否成功, 回显文本)。"""
    body = json.dumps({"cmd": "feeder_refill", "params": {}}).encode("utf-8")
    req = urllib.request.Request(
        rest_base + "/api/command", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            ret = json.loads(r.read().decode("utf-8"))
        return bool(ret.get("ok")), ret.get("msg", "")
    except Exception as exc:
        return False, f"REST 异常: {exc}"


def main() -> int:
    args = parse_args()
    cli = ModbusTcpClient(args.plc_ip, port=args.plc_port)
    print("=" * 70)
    print("[plc-bridge] OpenPLC 补料请求桥接器启动")
    print(f"  OpenPLC : {args.plc_ip}:{args.plc_port} 单元{args.unit} "
          f"线圈%QX0.{args.coil}")
    print(f"  产线REST: {args.rest}/api/command (cmd=feeder_refill)")
    print(f"  轮询间隔: {args.interval}s （Ctrl+C 退出）")
    print("=" * 70)

    last_state = False
    fired = 0
    try:
        while True:
            state = read_coil(cli, args)
            if state is None:
                print("[plc-bridge] OpenPLC 未就绪（等待 Runtime/Slaves 启动…）",
                      flush=True)
                cli.close()
            elif state and not last_state:
                fired += 1
                print(f"[plc-bridge] ⚡ 检测到补料请求（上升沿，第 {fired} 次）"
                      f" → 下发 feeder_refill …", flush=True)
                ok, msg = send_refill(args.rest)
                mark = "✓" if ok else "✗"
                print(f"[plc-bridge]   {mark} 产线回执: {msg}", flush=True)
            last_state = bool(state) if state is not None else last_state
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[plc-bridge] 收到 Ctrl+C，退出。")
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

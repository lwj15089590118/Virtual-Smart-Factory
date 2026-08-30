# -*- coding: utf-8 -*-
"""
tests/test_scada_network.py —— SCADA 真实网络链路冒烟（审查报告13 P1-3 修复）
=============================================================================
此前 WS 握手/帧编解码（scada/ws_hub.py）与 pymodbus 客户端读写
（scada/modbus_server.py）只活在各模块 __main__ 自检里，CI 从不执行；
本文件把这两个网络场景改写为 pytest 用例：起本地临时端口 → 真实客户端
断言 → 跑完即关，不依赖任何外部服务。

端口策略：用"临时占位后释放"方式选空闲端口（epidemic 撞口概率可忽略，
且各用例串行执行）；全部绑定 127.0.0.1，CI 安全。
标记：真实 socket 有墙钟等待，统一打 smoke（pytest -m "not smoke" 可跳过）。
"""

import base64
import hashlib
import json
import socket
import time

import pytest

from config import settings as S
from scada.ws_hub import _WS_GUID, WsHub

pytestmark = pytest.mark.smoke


def _free_tcp_port() -> int:
    """取一个当前空闲的 TCP 端口（占位-查询-释放）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_until(sock: socket.socket, tail: bytes) -> bytes:
    """读到指定结尾（握手响应头）为止。"""
    buf = b""
    while tail not in buf:
        chunk = sock.recv(512)
        assert chunk, "对端提前断开"
        buf += chunk
    return buf


def _read_one_frame(sock: socket.socket) -> bytes:
    """读取一个文本帧载荷（测试客户端侧，服务端帧不带掩码）。"""
    header = sock.recv(2)
    ln = header[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(sock.recv(2), "big")
    payload = b""
    while len(payload) < ln:
        payload += sock.recv(ln - len(payload))
    return payload


def test_ws_hub_handshake_frames_security():
    """WS 全链路：握手/Accept 密钥/中文与长帧广播/PingPong/Close +
    审查修复回归（P2-5）：路径精确匹配、跨站 Origin 拒绝。"""
    hub = WsHub("127.0.0.1", _free_tcp_port())
    hub.start()
    time.sleep(0.3)
    try:
        # --- 握手（带同源 Origin，模拟浏览器大屏）---
        cli = socket.create_connection(("127.0.0.1", hub.port), timeout=3)
        key = base64.b64encode(b"\x01" * 16).decode()
        req = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{hub.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Origin: http://127.0.0.1:{S.SCADA_HTTP_PORT}\r\n\r\n")
        cli.sendall(req.encode())
        resp = _read_until(cli, b"\r\n\r\n")
        assert b"101" in resp.split(b"\r\n")[0], f"同源握手失败: {resp[:80]}"
        expect_acc = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        assert expect_acc.encode() in resp, "Accept 密钥计算错误"
        time.sleep(0.2)
        assert hub.client_count() == 1, f"在线数应为1: {hub.client_count()}"

        # --- 审查修复回归：伪装/越权路径 404，跨站 Origin 403 ---
        for evil, code in ((b"GET /other HTTP/1.1\r\nHost: x\r\n\r\n", b"404"),
                           (b"GET /wsX HTTP/1.1\r\nHost: x\r\n\r\n", b"404"),
                           (b"GET /api/xx?x=/ws HTTP/1.1\r\nHost: x\r\n\r\n", b"404"),
                           (b"GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                            b"Origin: http://evil.example.com\r\n\r\n", b"403")):
            bad = socket.create_connection(("127.0.0.1", hub.port), timeout=3)
            bad.sendall(evil)
            assert code in bad.recv(64), f"应拒绝 {evil[:40]}: 未返回 {code.decode()}"
            bad.close()

        # --- 广播：小帧(中文) + 大帧(16bit 长度档) ---
        hub.broadcast({"seq": 1, "msg": "你好SCADA"})
        m1 = json.loads(_read_one_frame(cli).decode("utf-8"))
        assert m1["msg"] == "你好SCADA", "中文帧往返乱码"
        hub.broadcast({"seq": 2, "blob": "X" * 500})
        m2 = json.loads(_read_one_frame(cli).decode("utf-8"))
        assert m2["blob"] == "X" * 500, "长帧负载错误"

        # --- Ping(带掩码) → Pong ---
        mask = b"\x11\x22\x33\x44"
        payload = b"hb"
        cli.sendall(bytes([0x89, 0x80 | len(payload)]) + mask
                    + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        pong = cli.recv(2)
        assert pong[0] & 0x0F == 10, f"未收到 Pong: {pong}"

        # --- Close 优雅下线 ---
        mask2 = b"\xaa\xbb\xcc\xdd"
        cli.sendall(bytes([0x88, 0x80]) + mask2)
        time.sleep(0.3)
        assert hub.client_count() == 0, "Close 后在线数应为0"
        cli.close()
    finally:
        hub.stop()
    assert not hub.running


def test_modbus_tcp_read_and_writeback():
    """Modbus TCP 全链路（真实 pymodbus 客户端）：读状态/写 DO 写回设备/
    只读区纠偏；并回归审查修复（P1-1）只读开关默认拒绝写回。"""
    from pymodbus.client import ModbusTcpClient

    from main import Plant
    from scada.modbus_server import ModbusServer

    plant = Plant(speed=60, mode="fast", seed=S.DEFAULT_SEED,
                  enable_random_faults=False)
    plant.build()
    plant.start_up_all()
    plant.clock.run_until(40.0)                    # 跑出一点真实工艺数据
    mb = ModbusServer(plant, host="127.0.0.1", port=_free_tcp_port(),
                      allow_write=True)            # 本用例验证写回链路，显式开放
    assert mb.map and len(mb.map) >= 5, "设备映射块数量异常"
    asm_block = next(b for b in mb.map if b["device"] == S.ASSEMBLY_ID)
    mb.start()

    cli = ModbusTcpClient("127.0.0.1", port=mb.port)
    deadline = time.time() + 5.0
    while not cli.connect():                       # 等服务端线程就绪
        if time.time() > deadline:
            raise AssertionError("Modbus 服务端 5s 内未就绪")
        time.sleep(0.1)
    try:
        # --- 读装配状态码：启动后应为 待机(1)/运行(2) ---
        rr = cli.read_holding_registers(asm_block["state_reg"], count=2,
                                        slave=S.MODBUS_UNIT_ID)
        assert not rr.isError(), f"读状态失败: {rr}"
        assert rr.registers[0] in (1, 2), f"装配状态码异常: {rr.registers}"

        # --- 写 DO（FC6）→ 经 CallbackDataBlock 回调真实写回设备 ---
        stack_entry = next(p for p in asm_block["do"] if p["name"] == "do_stack_g")
        wr = cli.write_register(stack_entry["reg"], 1, slave=S.MODBUS_UNIT_ID)
        assert not wr.isError(), "写 DO 失败"
        time.sleep(0.3)
        assert plant.assembly.get_io("do_stack_g") == 1, \
            "外部写 DO 未写回设备（网络链路/回调链断裂）"

        # --- 只读开关回归（P1-1）：未开放写回时 handle_write 必须拒绝 ---
        ro = ModbusServer(plant, allow_write=False)
        ret = ro.handle_write(stack_entry["reg"], 0)
        assert "只读" in ret, f"只读模式应拒绝写回: {ret}"
        assert plant.assembly.get_io("do_stack_g") == 1, "只读模式意外改写了设备"

        # --- 只读区纠偏（真实网络写 + 镜像线程）：伪写状态码下一周期被真值覆盖 ---
        wr = cli.write_register(asm_block["state_reg"], 3, slave=S.MODBUS_UNIT_ID)
        assert not wr.isError(), "只读区写入请求失败"
        time.sleep(S.MODBUS_REFRESH_S * 2 + 0.5)   # 等至少两个镜像周期（墙钟，仅测试等待）
        rr = cli.read_holding_registers(asm_block["state_reg"], count=1,
                                        slave=S.MODBUS_UNIT_ID)
        expect = {0: "停止", 1: "待机", 2: "运行", 3: "故障", 4: "维护"}
        assert not rr.isError() and rr.registers[0] in (1, 2), \
            f"只读区误写未被纠偏: {rr.registers}（设备态 {expect.get(plant.assembly.state)}）"
    finally:
        cli.close()
        mb.stop()

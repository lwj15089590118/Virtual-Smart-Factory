# -*- coding: utf-8 -*-
"""
scada/ws_hub.py —— WebSocket 实时推送网关（纯标准库实现 RFC 6455）
====================================================================
为什么自研：
    硬性约束只允许 标准库 + numpy + flask + pymodbus，Flask/Werkzeug 原生
    不支持 WebSocket 升级，flask-socketio 属于额外依赖——因此按 RFC 6455
    用 socket/hashlib/base64/threading 手写一个最小可用的推送网关。

职责：
    1. 监听独立端口（settings.SCADA_WS_PORT），完成 HTTP Upgrade 握手；
    2. 每个客户端一条发送队列（有界，慢客户端自动丢帧不阻塞总线）+
       一条独立发送线程；接收方向只处理 关闭/心跳 帧（保活与优雅退出）；
    3. broadcast(dict)：把事件总线通配符订阅收到的事件 JSON 群发给所有在线端，
       由 scada/web_server.py 订阅 "*" 后调用——这就是"WebSocket 实时推送"通道。

协议覆盖范围（演示够用且完整）：
    - 文本帧收发（opcode 1）、Ping/Pong（9/10）、连接关闭（8）；
    - 负载长度 7bit / 16bit / 64bit 三档全支持；客户端帧必掩码（RFC 强制）。
    不支持：分片续帧(continuation)、二进制帧、服务端掩码（RFC 规定服务端不发掩码）。

线程模型假设（一行记录）：
    - accept 主循环 + 每 client 一条收/一发共两条 daemon 线程，
      广播侧只入队不碰 socket —— 天然线程安全，仿真 tick 零阻塞。
"""

import base64
import hashlib
import json
import queue
import socket
import threading

# RFC 6455 固定魔串：Sec-WebSocket-Accept = base64(sha1(key + GUID))
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_HANDSHAKE_TMPL = (
    "HTTP/1.1 101 Switching Protocols\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Accept: {accept}\r\n"
    "\r\n"
)
_QUEUE_LIMIT = 256          # 每客户端发送队列上限（满则丢弃最旧，防慢客户端拖垮广播）


class _Client:
    """一个在线 WS 客户端的会话载体。"""

    __slots__ = ("conn", "addr", "outq", "alive")

    def __init__(self, conn: socket.socket, addr):
        self.conn = conn
        self.addr = addr
        self.outq: "queue.Queue[bytes]" = queue.Queue(maxsize=_QUEUE_LIMIT)
        self.alive = True


class WsHub:
    """WebSocket 推送网关：start() 后即可 broadcast()。"""

    def __init__(self, host: str, port: int, path: str = "/ws"):
        self.host = host
        self.port = int(port)
        self.path = path                    # 只接受该路径的升级请求（其余 404）
        self._srv: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._clients: list = []            # 在线会话列表（受锁保护）
        self._lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """绑定端口并启动 accept 循环（重复调用安全）。"""
        if self._running:
            return
        self._srv.bind((self.host, self.port))
        self._srv.listen(8)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="WsHubAccept", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        """停机：关监听、断开全部客户端（daemon 线程随之自然结束）。"""
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for c in list(self._clients):
                self._drop(c)

    @property
    def running(self) -> bool:
        return self._running

    def client_count(self) -> int:
        """当前在线浏览器端数量（大屏右上角'在线终端'指标）。"""
        with self._lock:
            return sum(1 for c in self._clients if c.alive)

    # ------------------------------------------------------------------
    # 对外广播接口（web_server 订阅事件总线 "*" 后调用）
    # ------------------------------------------------------------------
    def broadcast(self, payload: dict) -> None:
        """
        把一条消息群发到所有在线客户端。
        - 序列化一次、逐端入队；队列满则丢弃最旧一帧再入队（慢端限流策略）；
          假设记录：监控事件允许偶发丢帧换取零阻塞——状态真相始终以 REST 轮询为准。
        """
        try:
            frame = _encode_text_frame(json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError):
            return                                  # 不可序列化内容直接放弃
        with self._lock:
            targets = [c for c in self._clients if c.alive]
        for c in targets:
            try:
                c.outq.put_nowait(frame)
            except queue.Full:
                try:                                # 丢最旧补最新
                    c.outq.get_nowait()
                    c.outq.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass

    # ------------------------------------------------------------------
    # accept 主循环
    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._srv.accept()
            except OSError:
                break                               # stop() 关闭监听后正常退出
            t = threading.Thread(target=self._serve_client,
                                 args=(conn, addr), daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # 单客户端会话：握手 → 收发双线程
    # ------------------------------------------------------------------
    def _serve_client(self, conn: socket.socket, addr) -> None:
        try:
            if not self._handshake(conn):
                conn.close()
                return
        except OSError:
            conn.close()
            return
        client = _Client(conn, addr)
        with self._lock:
            self._clients.append(client)
        # 发送线程：从本端队列取帧写 socket
        sender = threading.Thread(target=self._sender_loop,
                                  args=(client,), daemon=True)
        sender.start()
        # 接收方向由本线程亲自处理（关闭/心跳帧），退出即清理
        try:
            self._reader_loop(client)
        finally:
            self._drop(client)

    def _handshake(self, conn: socket.socket) -> bool:
        """HTTP Upgrade 握手：校验路径/头，回 101 与 Accept 密钥。"""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(1024)
            if not chunk:
                return False
            buf += chunk
            if len(buf) > 8192:                     # 异常超大握手头，拒绝
                return False
        head = buf.decode("latin-1", errors="replace")
        request_line = head.split("\r\n", 1)[0]
        parts = request_line.split(" ")
        if len(parts) < 2 or not parts[1].startswith(self.path):
            conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
            return False
        key = None
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "sec-websocket-key":
                    key = v.strip()
                if k.strip().lower() == "upgrade" and v.strip().lower() != "websocket":
                    return False
        if key is None:                             # 非 WS 的普通 HTTP 请求
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        conn.sendall(_HANDSHAKE_TMPL.format(accept=accept).encode("ascii"))
        return True

    def _reader_loop(self, client: _Client) -> None:
        """解析客户端→服务器帧：只关心 Close/Ping（文本上行忽略）。"""
        conn = client.conn
        try:
            while client.alive and self._running:
                header = _recv_exact(conn, 2)
                if header is None:
                    return
                fin_op, mask_len = header
                opcode = fin_op & 0x0F
                masked = bool(mask_len & 0x80)
                length = mask_len & 0x7F
                if length == 126:
                    ext = _recv_exact(conn, 2)
                    if ext is None:
                        return
                    length = int.from_bytes(ext, "big")
                elif length == 127:
                    ext = _recv_exact(conn, 8)
                    if ext is None:
                        return
                    length = int.from_bytes(ext, "big")
                mask_key = _recv_exact(conn, 4) if masked else None
                payload = _recv_exact(conn, length) if length else b""
                if length and payload is None:
                    return
                if masked and payload is not None and mask_key is not None:
                    payload = bytes(b ^ mask_key[i % 4]
                                    for i, b in enumerate(payload))
                if opcode == 8:                     # Close：礼貌回关后退出
                    try:
                        conn.sendall(b"\x88\x00")   # 关闭帧(0x88)+空负载
                    except OSError:
                        pass
                    return
                if opcode == 9 and payload is not None:   # Ping → Pong
                    try:
                        conn.sendall(bytes([0x8A, len(payload)]) + payload)
                    except OSError:
                        return
                # 其余 opcode（文本/pong 等）：网关无需处理，直接丢弃
        except (OSError, ValueError):
            return                                  # 连接断开属正常生命周期

    def _sender_loop(self, client: _Client) -> None:
        """发送线程：阻塞取队首帧写 socket；写失败即标记下线。"""
        while client.alive and self._running:
            try:
                frame = client.outq.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                client.conn.sendall(frame)
            except OSError:
                client.alive = False
                return

    def _drop(self, client: _Client) -> None:
        """下线清理：标记死亡、关 socket、移出花名册（幂等）。"""
        client.alive = False
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        try:
            client.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.conn.close()
        except OSError:
            pass


# ======================================================================
# 帧编解码工具（模块级函数，便于单测）
# ======================================================================
def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    """精确读取 n 字节；对端关闭返回 None。"""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _encode_text_frame(text: str) -> bytes:
    """把一段 UTF-8 文本封装为服务器→客户端 Text 帧（FIN=1, opcode=1, 无掩码）。"""
    payload = text.encode("utf-8")
    n = len(payload)
    if n < 126:
        header = bytes([0x81, n])
    elif n <= 0xFFFF:
        header = bytes([0x81, 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + n.to_bytes(8, "big")
    return header + payload


def _decode_frame(data: bytes) -> tuple:
    """
    解析一个完整客户端帧（自检用）：
    返回 (opcode:int, payload:bytes)；data 必须是恰好一帧。
    """
    fin_op, mask_len = data[0], data[1]
    opcode = fin_op & 0x0F
    masked = bool(mask_len & 0x80)
    length = mask_len & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(data[2:4], "big")
        offset = 4
    elif length == 127:
        length = int.from_bytes(data[2:10], "big")
        offset = 10
    if masked:
        mask_key = data[offset:offset + 4]
        offset += 4
        payload = bytes(b ^ mask_key[i % 4]
                        for i, b in enumerate(data[offset:offset + length]))
    else:
        payload = data[offset:offset + length]
    return opcode, payload


# ----------------------------------------------------------------------
# 自模块快速自检：python scada/ws_hub.py
# （用标准库 socket 扮演浏览器客户端，走真实 TCP 全链路）
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import time as wt

    hub = WsHub("127.0.0.1", 5091)
    hub.start()
    wt.sleep(0.3)

    # --- 客户端握手 ---
    cli = socket.create_connection(("127.0.0.1", 5091), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:5091\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    cli.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += cli.recv(512)
    assert b"101" in resp.split(b"\r\n")[0], f"握手失败: {resp[:80]}"
    expect_acc = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
    assert expect_acc.encode() in resp, "Accept 密钥计算错误"
    wt.sleep(0.2)
    assert hub.client_count() == 1, f"在线数应为1: {hub.client_count()}"

    # --- 错误路径应被拒绝 ---
    bad = socket.create_connection(("127.0.0.1", 5091), timeout=3)
    bad.sendall(b"GET /other HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"404" in bad.recv(64)
    bad.close()

    # --- 大小两档文本帧广播 + 客户端解码校验 ---
    received = []

    def read_one_frame(sock) -> bytes:
        h = sock.recv(2)
        ln = h[1] & 0x7F
        off = 2
        if ln == 126:
            ln = int.from_bytes(sock.recv(2), "big")
            off += 0                                     # 已读2字节扩展长度
        payload = b""
        while len(payload) < ln:
            payload += sock.recv(ln - len(payload))
        return payload

    hub.broadcast({"seq": 1, "msg": "你好SCADA"})
    received.append(read_one_frame(cli))
    big = {"seq": 2, "blob": "X" * 500}                  # 触发 16bit 长度档
    hub.broadcast(big)
    received.append(read_one_frame(cli))
    m1 = json.loads(received[0].decode("utf-8"))
    m2 = json.loads(received[1].decode("utf-8"))
    assert m1["msg"] == "你好SCADA", "中文帧往返乱码"
    assert m2["blob"] == "X" * 500 and len(received[1]) > 125, "长帧长度档错误"

    # --- Ping/Pong 保活（客户端帧必须带掩码）---
    ping_payload = b"hb"
    mask = b"\x11\x22\x33\x44"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(ping_payload))
    cli.sendall(bytes([0x89, 0x80 | len(ping_payload)]) + mask + masked)
    pong_hdr = cli.recv(2)
    assert pong_hdr[0] & 0x0F == 10, f"未收到 Pong: {pong_hdr}"
    cli.recv(pong_hdr[1] & 0x7F)                     # 读走 Pong 载荷，避免残留字节

    # --- Close 优雅下线 ---
    mask2 = b"\xaa\xbb\xcc\xdd"
    cli.sendall(bytes([0x88, 0x80]) + mask2)             # 空负载掩码 Close 帧
    wt.sleep(0.3)
    assert hub.client_count() == 0, "Close 后在线数应为0"
    cli.close()
    hub.stop()
    print(f"[ws_hub 自检通过] 握手/中文帧/长帧/PingPong/Close 全部通过 "
          f"(端口5091, 仿真验证值)")

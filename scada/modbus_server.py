# -*- coding: utf-8 -*-
"""
scada/modbus_server.py —— Modbus TCP 从站（pymodbus，班次2 交付项2）
=====================================================================
职责：
    把 Plant.devices[*].io_table（DI/DO/AI/AO 点表）映射为保持寄存器区，
    供组态软件/第三方 SCADA（如 组态王、Ignition、Modbus Poll）演示读写。
    端口 settings.MODBUS_TCP_PORT(1502)，单元号 settings.MODBUS_UNIT_ID。

寄存器映射规则（4x 保持寄存器，0 基地址 + zero_mode）：
    每台设备一个连续块，按 device_id 升序紧凑排布：
        base+0   状态码    0停止/1待机/2运行/3故障/4维护（只读）
        base+1   故障标志  0无故障/1有故障（只读）
        base+2.. DI 点序   0/1（只读）
        接着     DO 点序   0/1（可写！写回设备 set_io —— 演示远程控制）
        接着     AI 点序   工程值×100 取整（只读；如 50.00kN→5000）
        接着     AO 点序   工程值×100 取整（可写回 set_io）
    完整点表可通过 GET /api/modbus/map 获取（web_server 提供），亦可用
    build_register_map() 独立导出——组态软件组点时的"说明书"。

线程模型假设（一行记录）：
    - StartTcpServer 阻塞运行在 daemon 线程；IO 镜像线程每 MODBUS_REFRESH_S
      墙钟秒把 io_table 快照刷进寄存器（仅读侧镜像，不参与任何仿真计时，
      时间纪律不受影响）；DO/AO 写回经 devices 字典原子替换单值，竞态无害。
修复记录：外部写入现由 CallbackDataBlock 拦截并回调 handle_write——
    DO/AO 可写区写回设备真实生效；只读区误写被忽略并在下一刷新周期纠偏
    （此前 handle_write 未挂接数据块，可写区写不进设备、语义自洽但不完整）。
"""

import threading
from typing import Dict, List

import os
import sys
# 路径引导：直接运行本文件时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.server import StartTcpServer, ServerStop
from pymodbus.device import ModbusDeviceIdentification

try:
    from pymodbus.datastore import (ModbusServerContext,
                                    ModbusSequentialDataBlock,
                                    ModbusSlaveContext)
except ImportError as exc:  # 修复记录（CI 实战）：pymodbus 3.7+ 移除 ModbusSlaveContext，
    # 3.15 起旧数据存储整体弃用（服务层只走 SimDevice 拷贝，镜像/写回双通道失效）。
    # 本模块按 3.6.x 实测口径开发，requirements.txt 已锁定 pymodbus>=3.6,<3.7；
    # 这里把晦涩的 ImportError 翻译成可执行的行动指令。
    raise ImportError(
        "检测到不兼容的 pymodbus 版本（缺少 ModbusSlaveContext，应为 3.7+ 新版）。"
        "本仓库按 pymodbus 3.6.x 口径开发，请执行: pip install 'pymodbus>=3.6,<3.7'"
    ) from exc

from core.device_base import DeviceState
from config import settings as S

# 状态中文名 → 寄存器码（组态软件里配文本域即可还原中文显示）
_STATE_CODE = {
    DeviceState.STOPPED: 0,
    DeviceState.STANDBY: 1,
    DeviceState.RUNNING: 2,
    DeviceState.FAULT: 3,
    DeviceState.MAINTENANCE: 4,
}
_AI_SCALE = 100          # AI/AO 定标：工程值×100 存整数（保留两位小数精度）
_HEADER_REGS = 2         # 状态码+故障标志 占用的头部寄存器数


# ======================================================================
# 纯函数：寄存器映射表构建（无副作用，Web /api/modbus/map 与从站共用）
# ======================================================================
def build_register_map(devices: Dict[str, object]) -> List[dict]:
    """
    依据各设备 io_table 的声明顺序生成寄存器分配表。
    返回列表元素结构：
      {"device","name","base","state_reg","fault_reg",
       "di":[{"reg","name","desc"}...], "do":[...], "ai":[...], "ao":[...]}
    """
    blocks = []
    base = 0
    for dev_id in sorted(devices.keys()):
        dev = devices[dev_id]
        block = {"device": dev_id, "name": dev.name, "base": base,
                 "state_reg": base, "fault_reg": base + 1,
                 "di": [], "do": [], "ai": [], "ao": []}
        addr = base + _HEADER_REGS
        # io_table 是普通 dict（Python 3.7+ 保序）：按注册顺序 DI→DO→AI→AO 分节排布
        for point in dev.io_table.values():
            entry = {"reg": addr, "name": point.name,
                     "desc": point.desc, "unit": point.unit}
            if point.direction == "DI":
                block["di"].append(entry)
            elif point.direction == "DO":
                block["do"].append(entry)
            elif point.direction == "AI":
                block["ai"].append(entry)
            else:
                block["ao"].append(entry)
            addr += 1
        blocks.append(block)
        base = addr
    return blocks


class CallbackDataBlock(ModbusSequentialDataBlock):
    """
    写入拦截数据块（修复记录：原版 handle_write 只是"待接入"的空挂点，
    pymodbus 默认数据块允许任意写但不通知业务层——外部写 DO/AO 实际到不了设备）。
    现在外部写寄存器（FC6/FC16）时逐地址回调 ModbusServer.handle_write：
        - 命中 DO/AO 可写区 → 真实写回设备 set_io（远程控制生效）；
        - 只读区（状态/故障/DI/AI）→ handle_write 忽略，寄存器由 sync_once 纠偏。
    竞态修复记录：原实现用跨线程裸标志 _mirroring 在镜像期间整体旁路回调——
    外部写恰落镜像窗口即被丢弃且回写被覆盖（DO/AO 远程写静默丢失）。
    现改为双通道：setValues() 恒为外部写语义、任何时刻都回调；
    镜像线程改走专用 setValuesMirror() 直写存储（镜像值本就是设备真值，
    走回调也仅是无害幂等回显，专用通道只是更干净并省去逐寄存器扫描开销）。
    寻址口径（pymodbus 3.6 实测）：zero_mode=True 时 slave 直通地址，
    数据块收到的 address 与 build_register_map 的 reg 字段同一坐标系（块基址 0）。
    """

    def __init__(self, address: int, values: list, server: "ModbusServer"):
        super().__init__(address, values)
        self._server = server

    def setValues(self, address, values):
        """外部写入口（pymodbus 请求线程调用）：先逐地址业务回调，再落存。"""
        if not isinstance(values, (list, tuple)):      # FC6 单寄存器写为标量
            values = [values]
        base = int(address)
        for i, v in enumerate(values):
            try:
                self._server.handle_write(base + i, int(v))
            except Exception as exc:   # 回调异常不允许打断 pymodbus 请求线程
                print(f"[Modbus] 写回调异常 @ reg {base + i}: {exc}")
        super().setValues(address, values)

    def setValuesMirror(self, address, values):
        """镜像专用入口（sync_once 调用）：直接落存，不触发业务回调。"""
        super().setValues(address, values)


class ModbusServer:
    """把 Plant 设备点表暴露为 Modbus TCP 保持寄存器。"""

    def __init__(self, plant):
        self.plant = plant
        self.map = build_register_map(plant.devices)
        # ---- 数据存储：单从站模式 + zero_mode（客户端地址即块内偏移，最直观）----
        # 修复记录：hr 块换为写入拦截版 CallbackDataBlock（外部写 DO/AO 真正写回设备）；
        # 保留块引用供 sync_once 走 setValuesMirror 直写通道（不经 pymodbus 上下文）。
        self._hr_block = CallbackDataBlock(0, [0] * S.MODBUS_REG_COUNT, self)
        self.store = ModbusSlaveContext(zero_mode=True, hr=self._hr_block)
        self.context = ModbusServerContext(slaves=self.store, single=True)
        self.identity = ModbusDeviceIdentification(info_name={
            "VendorName": "Virtual-Smart-Factory",
            "ProductName": "VSF-Sim SCADA Gateway",
            "MajorMinorRevision": "2.0",
            "UserApplicationName": "Shift2-Demo",
        })
        self._sync_thread: threading.Thread | None = None
        self._server_thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 IO 镜像线程与 Modbus TCP 监听（重复调用安全）。"""
        if self._running:
            return
        self._running = True
        self.sync_once()                       # 先刷一版初值再开闸
        self._server_thread = threading.Thread(
            target=self._serve, name="ModbusTcpThread", daemon=True)
        self._server_thread.start()
        self._sync_thread = threading.Thread(
            target=self._sync_loop, name="ModbusSyncThread", daemon=True)
        self._sync_thread.start()

    def stop(self) -> None:
        """停机：停镜像并请求 pymodbus 服务器退出（daemon 兜底）。"""
        self._running = False
        try:
            ServerStop()
        except Exception:
            pass                               # 未启动/已退出均视为成功

    def _serve(self) -> None:
        try:
            StartTcpServer(context=self.context, address=("0.0.0.0", S.MODBUS_TCP_PORT),
                           identity=self.identity)
        except OSError as exc:
            print(f"[Modbus] 服务启动失败(端口{S.MODBUS_TCP_PORT}被占用?): {exc}")

    # ------------------------------------------------------------------
    # IO → 寄存器 镜像
    # ------------------------------------------------------------------
    def sync_once(self) -> None:
        """把全部设备的当前值刷进保持寄存器（每次全量覆盖，幂等）。

        竞态修复记录：镜像改走 setValuesMirror 专用直写通道，不再用跨线程
        裸标志旁路外部写——外部写任何时刻到达都会先回调业务层再落存，
        最坏只延迟一个刷新周期（≤MODBUS_REFRESH_S）在寄存器上收敛为设备真值，
        不存在被静默丢弃的窗口。
        """
        for block in self.map:
            dev = self.plant.devices.get(block["device"])
            if dev is None:
                continue
            values = [_STATE_CODE.get(dev.state, 0),
                      1 if dev.current_fault else 0]
            values += [int(bool(dev.get_io(p["name"]))) for p in block["di"]]
            values += [int(bool(dev.get_io(p["name"]))) for p in block["do"]]
            values += [int(round(float(dev.get_io(p["name"])) * _AI_SCALE))
                       for p in block["ai"]]
            values += [int(round(float(dev.get_io(p["name"])) * _AI_SCALE))
                       for p in block["ao"]]
            # fc=3 保持寄存器；镜像直写通道（不触发 handle_write 回调）
            self._hr_block.setValuesMirror(block["base"], values)

    def _sync_loop(self) -> None:
        import time as wt
        while self._running:
            try:
                self.sync_once()
            except Exception as exc:           # 镜像异常不拖垮服务线程
                print(f"[Modbus] 寄存器刷新异常: {exc}")
            wt.sleep(S.MODBUS_REFRESH_S)       # 墙钟仅控制刷新节拍（见文件头假设）

    # ------------------------------------------------------------------
    # 写回调：由 pymodbus 数据存储在请求线程触发
    # ------------------------------------------------------------------
    def handle_write(self, address: int, value: int) -> str:
        """
        外部对单个保持寄存器的写意图处理（由 CallbackDataBlock 在请求线程回调）：
        - 命中某设备 DO/AO 点 → 真实写回 set_io（远程控制生效）；
        - 其余（状态/故障/DI/AI 只读区）→ 忽略并返回说明，
          寄存器旧值由 sync_once 在下一刷新周期纠偏回真值。
        """
        for block in self.map:
            rel = address - block["base"]
            if rel < 0:
                continue
            for section in ("do", "ao"):
                entries = block[section]
                for idx, p in enumerate(entries):
                    if p["reg"] == address:
                        dev = self.plant.devices[block["device"]]
                        raw = value / _AI_SCALE if section == "ao" else int(bool(value))
                        dev.set_io(p["name"], raw)
                        return f"{block['device']}.{p['name']}={raw}"
            if rel < _HEADER_REGS:
                return f"{block['device']} 头部区为只读镜像"
        return "未映射地址，忽略"


# ----------------------------------------------------------------------
# 自模块快速自检：python scada/modbus_server.py
# 起 从站(1502)+Plant 快跑，用 pymodbus 客户端做真实读写闭环
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import time as wt

    from pymodbus.client import ModbusTcpClient

    from main import Plant

    plant = Plant(speed=60, mode="fast", seed=S.DEFAULT_SEED)
    plant.build()
    plant.start_up_all()
    mb = ModbusServer(plant)
    assert mb.map and len(mb.map) >= 5, "设备映射块数量异常"
    asm_block = next(b for b in mb.map if b["device"] == S.ASSEMBLY_ID)
    print(f"[映射表] 共{len(mb.map)}块 | 装配块基址={asm_block['base']} "
          f"DI={len(asm_block['di'])} DO={len(asm_block['do'])} "
          f"AI={len(asm_block['ai'])}")

    mb.start()
    wt.sleep(1.2)                          # 等待端口就绪（墙钟，仅测试等待）
    plant.clock.run_until(40.0)            # 跑一段产生真实工艺数据
    mb.sync_once()

    cli = ModbusTcpClient("127.0.0.1", port=S.MODBUS_TCP_PORT)
    assert cli.connect(), "客户端连接失败"
    # --- 读装配单元状态码（base+0）：启动后应为 待机(1)/运行(2) ---
    rr = cli.read_holding_registers(asm_block["state_reg"], count=2, slave=S.MODBUS_UNIT_ID)
    assert not rr.isError(), f"读状态失败: {rr}"
    state_code, fault_flag = rr.registers
    assert state_code in (1, 2), f"装配状态码异常: {state_code}"
    assert fault_flag in (0, 1)
    # --- 读压装力 AI（×100 定标）：跑过压装步后应出现过非零力值快照 ---
    press_entry = next(p for p in asm_block["ai"] if p["name"] == "ai_press_force")
    rr = cli.read_holding_registers(press_entry["reg"], count=1, slave=S.MODBUS_UNIT_ID)
    print(f"[读取] ai_press_force 寄存器={rr.registers[0]} (工程值÷100 kN)")
    # --- 写 DO 演示：置位 do_stack_g 三色灯绿（纯 TCP 写，经 CallbackDataBlock 回调写回设备）---
    stack_entry = next(p for p in asm_block["do"] if p["name"] == "do_stack_g")
    wr = cli.write_register(stack_entry["reg"], 1, slave=S.MODBUS_UNIT_ID)
    assert not wr.isError(), "写 DO 失败"
    wt.sleep(0.3)                          # 等请求线程完成回调（墙钟，仅测试等待）
    assert plant.assembly.get_io("do_stack_g") == 1, \
        "外部写 DO 未写回设备（CallbackDataBlock 回调链路断）"
    ret = mb.handle_write(stack_entry["reg"], 1)
    assert "do_stack_g=1" in ret, f"写回处理异常: {ret}"

    # --- 双通道语义回归（竞态修复点）：镜像直写不得触发业务回调；外部写任何时刻都生效 ---
    blk = mb._hr_block
    blk.setValuesMirror(stack_entry["reg"], [0])   # 模拟镜像写 0：设备值应保持 1 不变
    assert plant.assembly.get_io("do_stack_g") == 1, "镜像直写误触发了业务回调"
    blk.setValues(stack_entry["reg"], [0])         # 模拟外部写 0：应立即写回设备
    assert plant.assembly.get_io("do_stack_g") == 0, "外部写通道在镜像并存下失效"
    blk.setValues(stack_entry["reg"], [1])         # 恢复现场
    assert plant.assembly.get_io("do_stack_g") == 1

    # --- 只读区纠偏演示：向立体库状态寄存器伪造"故障码3"，下一刷新周期应被真值覆盖 ---
    wh_block = next(b for b in mb.map if b["device"] == S.WAREHOUSE_ID)
    wr = cli.write_register(wh_block["state_reg"], 3, slave=S.MODBUS_UNIT_ID)
    assert not wr.isError(), "只读区写入请求失败"
    wt.sleep(S.MODBUS_REFRESH_S * 2 + 0.5)         # 等至少两个镜像周期（墙钟，仅测试等待）
    rr = cli.read_holding_registers(wh_block["state_reg"], count=1, slave=S.MODBUS_UNIT_ID)
    expect_code = _STATE_CODE.get(plant.warehouse.state, 0)
    assert not rr.isError() and rr.registers[0] == expect_code, \
        f"只读区误写未被纠偏: {rr.registers[0]} vs 真值{expect_code}"
    cli.close()

    print(f"[modbus_server 自检通过] 映射{len(mb.map)}块, "
          f"读状态={state_code}, DO写回链路OK, 只读区纠偏OK, "
          f"镜像/外部双通道语义OK (仿真验证值)")
    mb.stop()

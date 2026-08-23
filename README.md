# Virtual-Smart-Factory 虚拟智能工厂一体化仿真平台

> 旗舰作品集项目 · 全软件仿真、零硬件依赖 · 所有运行指标均为 **仿真验证值**
> 分三个班次开发：**班次1 仿真内核与产线层（已完成）** → **班次2 SCADA监控层+AGV物流+Web可视化（当前已完成）** → 班次3 视觉算法/MES/EMS

## 一、班次1 已实现能力

| 子系统 | 文件 | 能力 |
|---|---|---|
| 仿真时钟 | `core/sim_clock.py` | 全厂唯一时间源；加速倍率 1x/10x/60x（任意正数）；暂停/恢复；实时与加速批量双模式同路径推进 |
| 事件总线 | `core/event_bus.py` | 发布/订阅（精确+通配）；JSONL 追加持久化；环形缓冲查询 `recent()/replay()` |
| 设备基类 | `core/device_base.py` | 五态状态机（停止/待机/运行/故障/维护）；DI/DO/AI/AO 点表；运行秒数/循环数/停机原因统计；故障注入接口 |
| 故障注入器 | `core/fault_injector.py` | 随机故障（泊松率可配）+ 脚本故障（设备/时刻/类型）+ 急停人工复位；全部产生事件 |
| 装配单元 | `lines/unit_assembly.py` | PLC 顺控 8 步状态机；节拍可配（默认 32s）；安全门开→保持、急停→全线停 |
| 视觉质检 | `lines/unit_vision.py` | 尺寸规则判定 OK/NG（理论 NG 率≈4.6%）；NG 分流返修道；质检记录输出（班次3 换真算法） |
| 码垛单元 | `lines/unit_palletizing.py` | 3×4×4=48 箱/托垛型；毫米坐标随事件输出；垛满→托盘输出→AGV 呼叫事件 |
| 立体库 | `lines/warehouse.py` | 200 库位表（4排×10列×5层）；出入库队列；堆垛机任务模型；库位回收复用 |

## 二、班次2 新增能力（SCADA 监控层 + AGV 物流 + Web 可视化）

| 子系统 | 文件 | 能力 |
|---|---|---|
| WebSocket 网关 | `scada/ws_hub.py` | 纯标准库 RFC6455 实现（握手/文本帧/PingPong/Close，长帧三档长度）；每客户端有界发送队列；慢客户端丢帧不阻塞仿真 |
| SCADA Web 服务 | `scada/web_server.py` | Flask REST：`/api/status /api/kpi /api/events /api/pallet3d /api/warehouse/locations /api/modbus/map`；POST `/api/command` 命令入口（带 ui.command 审计事件）；订阅总线通配符 "*" 实时 WS 推送；KPI 趋势分桶聚合 |
| Modbus TCP 从站 | `scada/modbus_server.py` | pymodbus 把全部设备 io_table 映射为保持寄存器（状态码/故障标志/DI/DO/AI×100 定标）；DO/AO 支持写回设备；寄存器映射表可经 `/api/modbus/map` 导出给组态软件 |
| AGV 车队 | `agv/agv_fleet.py` | ≥2 台车六阶段任务状态机（空闲→去取货→装载→运输→交货→回位）；入库任务接码垛 agv.call、出库任务接 out_staging 运抵出货口；二维平面位置/里程/电量模型；车辆纳入全线急停与随机故障体系 |
| 监控大屏 | `web/static/*` | ECharts(CDN 多源回退)：工厂流程图(状态色块)、产量趋势、NG率仪表盘、垛型3D(bar3D 毫米坐标)、库位热力图、AGV 物流地图、实时事件滚动表、设备一览；按钮：启动/暂停/急停/复位/开关安全门/手动出库/调倍率 |

## 三、安装与启动

```bash
# 环境：Windows 10 + Python 3.12
pip install -r requirements.txt      # numpy/flask/pymodbus

# 全厂自检（逐模块 + Web API 冒烟 + AGV 闭环 + 600s 加速冒烟，报告到 reports/）
python selftest.py

# ★ 班次2 一键演示：实时模式 + 监控大屏(http://127.0.0.1:5080)
#   + WebSocket(5081) + Modbus TCP 从站(1502)，Ctrl+C 优雅停机
python main.py --web --speed 10

# 浏览器打开大屏后可用按钮控制全厂；第三方组态软件连 Modbus 1502 即可读写点表

# 加速跑批（无界面）
python main.py --speed 60 --duration 900

# 常用参数
#   --speed 1|10|60    加速倍率（接受任意正数）
#   --web              启动大屏+WS+Modbus（默认 realtime 长驻，Ctrl+C 退出）
#   --duration 秒      仿真时长（普通模式默认600，--web 默认不限时）
#   --seed N           随机种子（结果可复现）
#   --no-random-faults 关闭随机故障（脚本故障保留）
#   --no-agv           关闭车队退回班次1占位搬运（回归对照用）
```

每个模块均可独立运行内置自检：`python core/sim_clock.py`、`python agv/agv_fleet.py`、
`python scada/ws_hub.py`、`python scada/web_server.py`、`python scada/modbus_server.py` 等。

## 四、文件树

```
Virtual-Smart-Factory/
├─ main.py                    编排入口（Plant 编排器 + AGV接入 + execute_command + --web）
├─ selftest.py                全厂自检（A1~A8 + B2 Web冒烟 + B3 AGV闭环 + B1 600s联跑 = 11 用例）
├─ requirements.txt
├─ config/settings.py         全局参数中心（节拍/垛型/库型/故障率/AGV站点/端口/趋势桶）
├─ core/
│  ├─ sim_clock.py            仿真时钟引擎
│  ├─ event_bus.py            事件总线 + JSONL 持久化（班次2追加 agv.task_* / ui.command 事件）
│  ├─ device_base.py          设备基类（状态机/IO点表/统计/故障接口）
│  └─ fault_injector.py       故障注入器
├─ lines/
│  ├─ product.py              产品/托盘数据结构
│  ├─ unit_assembly.py        装配单元（8步顺控+双联锁）
│  ├─ unit_vision.py          视觉质检单元
│  ├─ unit_palletizing.py     码垛机器人单元（班次2加 current_grid() 访问器）
│  └─ warehouse.py            立体库简化模型
├─ agv/                       【班次2新增】
│  └─ agv_fleet.py            AGV 车队（六阶段任务状态机 + 调度器 + 平面位置模型）
├─ scada/                     【班次2新增】
│  ├─ ws_hub.py               标准库 RFC6455 WebSocket 推送网关
│  ├─ web_server.py           Flask REST + WS 订阅推送 + KPI 趋势聚合
│  └─ modbus_server.py        pymodbus TCP 从站（io_table→保持寄存器映射）
├─ web/static/                【班次2新增】监控大屏（index.html + app.js + style.css，ECharts CDN）
├─ docs/
│  ├─ HANDOVER_SHIFT2.md      班次2交接Prompt模板（存档）
│  ├─ HANDOVER_SHIFT3.md      班次3交接Prompt模板【班次2产出】
│  ├─ CHECKLIST_SHIFT1.md     班次1自检清单
│  └─ CHECKLIST_SHIFT2.md     班次2自检清单【班次2产出】
├─ logs/                      运行期生成：events_*.jsonl 事件流
└─ reports/                   运行期生成：selftest_report_*.txt 自检报告
```

## 五、关键指标（仿真验证值）

- 装配节拍 **32.0 s/件**（8 步顺控之和，可在 settings 改配）
- 视觉检测节拍 **2.5 s/件**，理论 NG 率 **≈4.6%**（σ=0.04mm，公差 ±0.08mm）
- 码垛能力 **48 箱/托**（3×4×4），单箱码放 1.2s
- 立体库容量 **200 托**（4×10×5），堆垛机单任务 25s
- AGV 车速 **1.5 m/s**，装/卸各 4s，2 台车；满托端到端入库（码垛出口→上架）约 **65s**
- 600s 加速联跑产量 **15~18 件**（含注入故障影响；班次2 因并入 AGV 随机故障，随机流与班次1略有差异）
- 自检 **11/11 通过**（A1~A8 模块级 + B2 Web 冒烟 + B3 AGV 闭环 + B1 联跑）

## 六、后续班次挂接点速查

| 班次 | 挂接点 | 位置 |
|---|---|---|
| 2✔ | SCADA Web 服务（REST/WebSocket） | `scada/web_server.py`，端口 `settings.SCADA_HTTP_PORT/SCADA_WS_PORT` |
| 2✔ | Modbus TCP 从站 | `scada/modbus_server.py`，端口 `MODBUS_TCP_PORT`，映射表 `/api/modbus/map` |
| 2✔ | 真实 AGV 调度 | `agv/agv_fleet.py`（已替换占位搬运；多车调度扩展点在 `AGVFleet.update()` 派单段） |
| 3 | 真实视觉算法 | 覆写 `UnitVision.judge()`（qc_records 已含原始测量字段） |
| 3 | MES 报工 | 直接消费 JSONL 事件流或订阅 `flow.product_out / vision.* / pallet.full / wh.*` |
| 3 | EMS/健康模块 | 订阅 `fault.raised / device.state` 事件做特征提取；`DeviceBase.enter_maintenance()` 已预留 |

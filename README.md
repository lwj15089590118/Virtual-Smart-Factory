# Virtual-Smart-Factory 虚拟智能工厂一体化仿真平台

> 旗舰作品集项目 · 全软件仿真、零硬件依赖 · 所有运行指标均为 **仿真验证值**
> 分三个班次开发：**班次1 仿真内核与产线层（已完成）** → **班次2 SCADA监控层+AGV物流+Web可视化（已完成）** → **班次3 视觉算法/MES/EMS（已完成）**

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

## 三、班次3 新增能力（视觉算法 + MES 制造执行 + EMS 能源/健康管理）

| 子系统 | 文件 | 能力 |
|---|---|---|
| 多特征测量模型 | `vision/measure_model.py` | 单尺寸高斯抽样升级为 4 维观测向量（尺寸偏差/圆度/表面得分/边缘锐度，含测量噪声）；真值口径 NG = 尺寸超差(≈4.6%) ∪ 公差带内隐性缺陷(≈2.5%) ≈ **7%**，隐性缺陷（表面划痕/边缘崩缺/装配错位）只有多特征算法能识别 |
| 轻量分类器 | `vision/classifiers.py` | 全部 numpy 手写：健康基线异常得分变换器（SPC 控制限思想）/ 逻辑回归（批量GD+L2，在线主模型）/ 单类马氏距离（Hotelling T² 思想，A/B 对照第二算法）；参数支持导出回载 |
| 样本与评估 | `vision/defect_generator.py` | 受控随机生成带真值缺陷样本集（种子固定可复现）；混淆矩阵/准确率/查准/查全/F1 计算；规则法 vs 逻辑回归 vs 马氏三方 A/B 对照流水线（独立测试集口径） |
| 判定算法注入 | `vision/vision_upgrade.py` | 实例级覆写 `UnitVision.judge()`（原类零改动，班次1/2 回归路径保留）；判定明细（P(NG)/特征向量/与规则法对照结论）随质检记录落盘；在线混淆矩阵滚动累计；`--rule-vision` 一键退回规则法对照 |
| MES 引擎 | `mes/mes_engine.py` | 订阅总线通配符 "*" 自动报工；OEE ≈ 可用率×性能率×良品率（装配单元近似口径）；工单满单自动关单翻单；产品↔托盘↔批次↔工单四级追溯反查；Web 命令按指定数量开单（插单优先投产） |
| 订单/追溯模型 | `mes/order_model.py` | WorkOrder/Batch 数据模型 + TraceabilityIndex 追溯索引（正查/反查、托盘库位流转历史、QC 档案、容量上限防长跑爆内存） |
| JSONL 回放 | `mes/jsonl_replay.py` | 离线回放 `logs/events_*.jsonl` 重建完整 MES 台账（与在线台账一致性已入自检 C2）；CLI 直接输出报工报表 |
| 能耗模型 | `ems/energy_model.py` | 订阅 device.state 按【状态→功率kW】曲线分段积分 kWh（未闭合段快照时虚拟结算）；电费按尖峰平谷分时电价（谷0.35/平0.65/峰1.05 元/kWh，可配/可关）跨档自动切分子段计价并输出分档台账，另折算 CO₂，全部为仿真验证值 |
| 健康监视 | `ems/health_monitor.py` | 滚动窗口提取 故障次数/停机占比/平均恢复时长/启停切换 → 扣分制 0~100 健康分 + 四级维护建议；跌破阈值发 `ems.health_alert`（滞回防抖）；`ems_maintain / ems_maintain_done` 维护命令闭环（触发 `DeviceBase.enter_maintenance()` 预留接口） |
| 大屏扩展 | `web/static/*`、`scada/web_server.py` | REST 新增 `/api/mes/orders /api/mes/batches /api/mes/trace /api/ems/energy /api/ems/health`；面板⑨ MES 工单与追溯（支持产品号/托盘号查询）、面板⑩ 能耗·设备健康度 |

## 四、安装与启动

```bash
# 环境：Windows 10 + Python 3.12
pip install -r requirements.txt      # numpy/flask/pymodbus

# 全厂自检（A模块级 + B Web/AGV冒烟 + C 算法/MES/EMS/订单生命周期 共15用例 + 600s 加速联跑，报告到 reports/）
python selftest.py

# 离线回放最新事件流，输出 MES 报工报表（作品集"离线数据分析"演示素材）
python mes/jsonl_replay.py

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
#   --rule-vision      关闭班次3视觉算法注入（退回班次1规则法，A/B 回归对照用）
```

每个模块均可独立运行内置自检：`python core/sim_clock.py`、`python agv/agv_fleet.py`、
`python scada/ws_hub.py`、`python scada/web_server.py`、`python scada/modbus_server.py`、
`python vision/vision_upgrade.py`、`python mes/mes_engine.py`、`python ems/health_monitor.py` 等。

## 五、文件树

```
Virtual-Smart-Factory/
├─ main.py                    编排入口（Plant 编排器 + AGV接入 + execute_command + --web）
├─ selftest.py                全厂自检（A1~A8 + B2 Web冒烟 + B3 AGV闭环 + C1~C4 + B1 600s联跑 = 15 用例）
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
│  ├─ web_server.py           Flask REST + WS 订阅推送 + KPI 趋势聚合（班次3追加 /api/mes/* /api/ems/*）
│  └─ modbus_server.py        pymodbus TCP 从站（io_table→保持寄存器映射）
├─ vision/                    【班次3新增】视觉算法升级包
│  ├─ measure_model.py        多特征测量仿真模型（4维观测+测量噪声+隐性缺陷真值口径）
│  ├─ classifiers.py          numpy 手写轻量分类器（异常变换器/逻辑回归/单类马氏）
│  ├─ defect_generator.py     缺陷样本生成器 + 混淆矩阵指标计算 + 三方A/B评估
│  └─ vision_upgrade.py       UnitVision.judge() 实例级注入入口（保留规则法A/B对照）
├─ mes/                       【班次3新增】制造执行系统
│  ├─ order_model.py          工单/批次数据模型 + 产品→托盘→批次→工单追溯索引
│  ├─ mes_engine.py           MES 引擎（事件驱动自动报工/OEE近似/四级追溯反查）
│  └─ jsonl_replay.py         JSONL 回放器（离线重建 MES 台账 + CLI 报告）
├─ ems/                       【班次3新增】能源与健康管理
│  ├─ energy_model.py         设备能耗模型（状态功率曲线分段积分 → kWh/电费/CO₂）
│  └─ health_monitor.py       设备健康评分（滚动窗口特征 → 0~100分 + 维护建议 + 告警）
├─ web/static/                【班次2新增·班次3扩展】监控大屏（index.html + app.js + style.css，
│                             ECharts CDN；班次3追加面板⑨ MES工单追溯、面板⑩ 能耗·健康度）
├─ docs/
│  ├─ HANDOVER_SHIFT2.md      班次2交接Prompt模板（存档）
│  ├─ HANDOVER_SHIFT3.md      班次3交接Prompt模板（存档）
│  ├─ CHECKLIST_SHIFT1.md     班次1自检清单
│  ├─ CHECKLIST_SHIFT2.md     班次2自检清单【班次2产出】
│  └─ CHECKLIST_SHIFT3.md     班次3自检清单【班次3产出】
├─ logs/                      运行期生成：events_*.jsonl 事件流
└─ reports/                   运行期生成：selftest_report_*.txt 自检报告
```

## 六、关键指标（仿真验证值）

- 装配节拍 **32.0 s/件**（8 步顺控之和，可在 settings 改配）
- 视觉检测节拍 **2.5 s/件**；真值 NG 率 **≈7%**（尺寸超差 ≈4.6%，σ=0.04mm 公差 ±0.08mm ∪ 公差带内隐性缺陷 ≈2.5%）
- 码垛能力 **48 箱/托**（3×4×4），单箱码放 1.2s
- 立体库容量 **200 托**（4×10×5），堆垛机单任务 25s
- AGV 车速 **1.5 m/s**，装/卸各 4s，2 台车；满托端到端入库（码垛出口→上架）约 **65s**
- 视觉算法 A/B 对照（训练1500件/独立测试2000件）：逻辑回归 **准确率98.95% / 查全84.8% / F1 90.99%**，对比班次1规则法 97.45%/64.0%/75.83%（查全率 64%→84.8% 来自"健康基线异常特征"工程）；600s 联跑在线混淆矩阵账目自洽
- MES：工单→批次→托盘→产品 四级追溯全链路闭环（可反查库位与流转历史）；48件直灌+装配并行产出用例报工 OK54/NG2，良率 **96.4%**，OEE≈**90.0%**（A×P×Q 近似口径）；JSONL 回放重建台账与在线完全一致
- MES 指定数量订单：Web 命令按任意计划量开单并**插单优先投产**——C4 用例 50 件工单从 REST 开单 → 满单 50 件自动关单（审计落盘）→ 自动翻单开新单全生命周期闭环，期间旧工单零污染
- EMS 能耗：功率曲线分段积分精确（60s×12kW=0.200kWh，误差<0.01）；电费按尖峰平谷分时计价（谷0.35/平0.65/峰1.05 元/kWh），状态段跨档自动切分，分档电费合计=总电费；CO₂ 按 0.5568 kg/kWh 折算
- EMS 健康：无故障期评分 ≥98 → 全线急停40s 后 89.5 → 连续故障 46.5（跌破告警线60自动发 `ems.health_alert` 并给出维护建议）；`ems_maintain` 维护命令进出闭环
- 600s 加速联跑产量 **15~18 件**（含注入故障影响；班次3实测 18件 OK17/NG1、NG率5.6%——升级算法把隐性缺陷纳入 NG 口径所致，加 `--rule-vision` 可复现班次1/2 口径）
- 自检 **15/15 通过**（A1~A8 模块级 + B2 Web 冒烟 + B3 AGV 闭环 + C1~C4 算法/MES/EMS/订单全生命周期 + B1 联跑）

## 七、后续班次挂接点速查

| 班次 | 挂接点 | 位置 |
|---|---|---|
| 2✔ | SCADA Web 服务（REST/WebSocket） | `scada/web_server.py`，端口 `settings.SCADA_HTTP_PORT/SCADA_WS_PORT` |
| 2✔ | Modbus TCP 从站 | `scada/modbus_server.py`，端口 `MODBUS_TCP_PORT`，映射表 `/api/modbus/map` |
| 2✔ | 真实 AGV 调度 | `agv/agv_fleet.py`（已替换占位搬运；多车调度扩展点在 `AGVFleet.update()` 派单段） |
| 3✔ | 真实视觉算法 | `vision/vision_upgrade.py`（实例级注入覆写 `judge()`；`--rule-vision` 退回规则法对照） |
| 3✔ | MES 报工 | `mes/mes_engine.py`（订阅 "*" 自动报工/追溯）；离线分析 `python mes/jsonl_replay.py` |
| 3✔ | EMS/健康模块 | `ems/energy_model.py`（能耗积分）+ `ems/health_monitor.py`（健康评分/告警/维护接口已启用） |

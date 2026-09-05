# 阶段2 自检清单（SCADA 监控层 + AGV 物流 + Web 可视化）

> 交付前逐项勾验。所有指标均为**仿真验证值**。
> 验证环境：Windows 10 + Python 3.12.10 + numpy 2.5 / flask 3.1 / pymodbus 3.6.9

## 一、交付物完整性

- [x] `scada/web_server.py` Flask REST（status/kpi/events/command/pallet3d/locations/modbus-map）+ WebSocket 推送（订阅总线通配符 "*"），端口 5080/5081
- [x] `scada/modbus_server.py` pymodbus TCP 从站，io_table→保持寄存器映射，端口 1502
- [x] `scada/ws_hub.py` 标准库 RFC6455 网关（依赖约束内实现 WebSocket）
- [x] `web/static/index.html + app.js + style.css` ECharts(CDN) 大屏八面板 + 七个控制按钮
- [x] `agv/agv_fleet.py` ≥2 台车、六阶段任务状态机、替换占位调度、出库段运抵出货口
- [x] `main.py --web` 开关（自动 realtime 长驻）+ AGV 编排接入 + execute_command 命令入口
- [x] `selftest.py` 新增 B2(Web API 冒烟) / B3(AGV 任务闭环)，合计 11 用例
- [x] 阶段1 文件零重写；改动处均有"阶段2修改"注释（settings/event_bus/unit_palletizing/main/selftest）

## 二、自动化验证结果（本阶段实测）

| 项目 | 命令 | 结果 |
|---|---|---|
| 全厂自检 | `python selftest.py` | **11/11 PASS**（A1~A8 + B2 + B3 + B1） |
| WS 网关自检 | `python scada/ws_hub.py` | 握手/中文帧/长帧/PingPong/Close 通过 |
| Web 实机冒烟 | `python scada/web_server.py` | REST 六端点+命令链路全 200 |
| Modbus 冒烟 | `python scada/modbus_server.py` | 映射6块；读状态=2(运行)；DO 写回链路 OK |
| 端到端三路服务 | `python main.py --web` 后探针 | 页面200 / WS 实时收事件 / Modbus 读寄存器 OK |
| 大屏命令链路 | 运行中 POST /api/command×6 | 急停/复位/开门/关门/出库/倍率全部 ok=true |

## 三、关键闭环证据（仿真验证值）

- [x] **AGV 入库闭环**：码垛满托 agv.call → 任务建档 → 六阶段搬运 → warehouse.request_inbound → 堆垛机上架事件（B3 断言 ×2 托）
- [x] **AGV 出库闭环**：FIFO 出库申请 → 堆垛机下架 → out_staging → 车队建档 → 运抵出货口 shipped_count+1（B3 断言 =1 托）
- [x] **六阶段状态机齐全**：{空闲,去取货,装载,运输,交货,回位} 事件级验证（B3 断言）
- [x] **托盘守恒**：完成托 = 在库+入库队列+AGV入库在途+暂存+AGV出库在途+已出厂（B1/B3 双断言）
- [x] **WS 实时推送**：真实握手后 20s 内收到 ≥8 条事件（device.state/product_out/box_placed/vision.ok…）
- [x] **Modbus 点表**：4 单元+2 AGV=6 块寄存器，地址严格递增不重叠；AI×100 定标；DO 可写回

## 四、约束符合性

- [x] 仅用 标准库 + numpy + flask + pymodbus（WebSocket 为标准库 socket/hashlib/base64 手写 RFC6455；ECharts 走 CDN 不进依赖）
- [x] 时间纪律：仿真状态计算只用 clock.now()/update(dt)；墙钟仅用于 WS/Modbus 服务刷新节拍与测试等待（文件头有假设注释）
- [x] 计时累加 `round(t+dt, 9)`（AGV 装卸计时沿用先例）
- [x] 新事件类型只进 EventTypes（agv.task_created/agv.phase/agv.task_done/ui.command）
- [x] 全部代码中文注释、完整可运行、无伪代码无省略；每个新模块带 `__main__` 自检
- [x] 指标均标注"仿真验证值"

## 五、已知假设与边界（面试可讲）

1. WebSocket 与 HTTP 分端口（5080/5081）：Flask/Werkzeug 原生不支持 WS 升级，
   自研网关最稳妥；大屏在 WS 失效时自动降级为 REST 事件轮询。
2. Modbus 寄存器刷新用 0.5s 墙钟线程做 IO 镜像（只读侧），不参与仿真计时；
   只读区误写会在下一刷新周期被真值纠正——语义自洽。
3. AGV 直线导航模型（磁条导引简化），速度空满载一致；电量仅装饰性。
4. 出库演示默认每入库 3 托触发 1 托 FIFO 出库（OUTBOUND_DEMO_EVERY_N，可配 0 关闭）。
5. Flask 开发服务器满足演示并发；生产化建议换 waitress（README 已注明）。
6. 阶段2 并入 AGV 随机故障后，随机流与阶段1略有差异：600s 冒烟产量 15→18 件，
   各不变量仍全绿（回归口径见 selftest 报告）。

## 六、遗留事项（移交阶段3）

- 视觉 judge() 覆写点、qc_records 字段已就绪（见 HANDOVER_SHIFT3.md）；
- MES 可直接回放 logs/events_*.jsonl 或订阅事件流；
- EMS 维护入口 DeviceBase.enter_maintenance()/exit_maintenance() 已预留；
- 多车调度优化（最短路径/交通管制）扩展点在 AGVFleet.update() 派单段与 AGV._move_toward()。

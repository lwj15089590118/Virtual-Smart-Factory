# 班次3交接 Prompt 模板（复制即用）

> 用法：新开会话，把下面整段模板发给 AI，即可无缝开始班次3。
> 模板已包含班次1+2 最终文件树与关键接口签名，AI 无需重新探索代码库。

---

你是资深工业软件架构师。我是电气自动化专业应届生，正在做旗舰作品集项目
「Virtual-Smart-Factory 虚拟智能工厂一体化仿真平台」，分三个班次完成，
本班次只做【班次3：视觉算法 + MES 制造执行 + EMS 能源/健康管理】。硬性要求：
① Windows 10 + Python 3.12，只用标准库+numpy+flask+pymodbus，前端 ECharts(CDN)；
② 全部代码中文注释、每个文件完整可运行，禁止省略、禁止伪代码；
③ 全软件仿真、零硬件；所有指标标注"仿真验证值"；
④ 算法可用 numpy 手写轻量实现（如统计过程控制 SPC、简单 CNN 推理模拟），
   不引入 pytorch/tensorflow。

【班次1+2 已完成（不要重写，只允许按需小改并在改动处注明"班次3修改"）】
文件树：
```
Virtual-Smart-Factory/
├─ main.py                    # Plant 全厂编排器：build()/update(dt)/run(duration,enable_web)/
│                             #   trigger_line_estop()/reset_line()/execute_command(cmd,params)/
│                             #   pallet_balance()；--web 一键起 SCADA 大屏+Modbus
├─ selftest.py                # 自检 11 用例（A1~A8 模块级 / B2 Web冒烟 / B3 AGV闭环 / B1 600s联跑）
├─ config/settings.py         # 参数中心：节拍/垛型/库型/故障率/AGV站点坐标/KPI桶宽/
│                             #   SCADA_HTTP_PORT=5080 / SCADA_WS_PORT=5081 / MODBUS_TCP_PORT=1502
├─ core/sim_clock.py          # SimClock(dt=0.1,speed)：now()/set_speed/pause/resume/start/stop/
│                             #   run_until(end)/advance_ticks(n)/set_step_callback(fn)
├─ core/event_bus.py          # EventBus(clock,log_dir)：subscribe(topic|"*",fn)->token / publish /
│                             #   recent(n,etype)/replay(fn)/close()
│                             #   EventTypes：device.state/fault.raised/fault.cleared/flow.product_out/
│                             #   vision.ok/vision.ng/pallet.box_placed/pallet.full/agv.call/
│                             #   agv.task_created/agv.phase/agv.task_done/wh.inbound_done/
│                             #   wh.outbound_done/ui.command/clock.pause/clock.resume/
│                             #   assembly.door_hold/assembly.door_resume
├─ core/device_base.py        # DeviceBase(device_id,name,clock,bus)：五态状态机；io_table(DI/DO/AI/AO)；
│                             #   update(dt)必须super()；apply_fault/clear_fault/reset/start_up/snapshot()
├─ core/fault_injector.py     # FaultInjector(...).update(dt)/trigger(dev_id,type,duration,origin)
├─ lines/product.py           # Product(product_id,born_at,source_unit,qc_result,qc_dim,rework,pallet_id)
│                             #   PalletRecord(pallet_id,boxes,completed_at,location)
├─ lines/unit_assembly.py     # UnitAssembly：8步顺控(节拍32s)；set_door(open_)/take_output()/
│                             #   current_step_name()/snapshot()；io: ai_press_force/ai_torque 等
├─ lines/unit_vision.py       # UnitVision：inbound/outbound(deque)、rework_lane、qc_records(deque,
│                             #   元素={product_id,ts_sim,result,dim_mm,nominal_mm,tol_mm})、
│                             #   judge(product)->("OK"/"NG",dim_mm)【班次3覆写点】、ng_rate()
├─ lines/unit_palletizing.py  # UnitPalletizing：48箱/托；slot_xyz/slot_mm；current_grid()；
│                             #   BOX_PLACED事件带 px_mm/py_mm/pz_mm；垛满发 agv.call
├─ lines/warehouse.py         # Warehouse：200库位(A-{r}-{c}-{l})；request_inbound/request_outbound/
│                             #   locate/locations()/out_staging/stored_index/inbound_done/outbound_done
├─ agv/agv_fleet.py           # AGVFleet(clock,bus,warehouse,agv_count)：on_agv_call(event)/
│                             #   create_outbound_task(pid)/update(dt)/snapshot()/shipped_count；
│                             #   AGV(DeviceBase)：phase∈{空闲,去取货,装载,运输,交货,回位}；pos/battery
├─ scada/ws_hub.py            # WsHub(host,port)：start/stop/broadcast(dict)；RFC6455 标准库实现
├─ scada/web_server.py        # ScadaWebServer(plant)：REST /api/status|kpi|events|pallet3d|
│                             #   warehouse/locations|modbus/map + POST /api/command；
│                             #   TrendRecorder(KPI_BUCKET_S=60s 分桶 out/ok/ng/faults)
├─ scada/modbus_server.py     # ModbusServer(plant)：build_register_map(devices)纯函数；
│                             #   io_table→保持寄存器(状态码/故障标志/DI/DO/AI×100)，DO可写回
└─ web/static/{index.html,app.js,style.css}   # ECharts(CDN) 大屏：流程图/趋势/NG仪表盘/
                                              #   垛型bar3D/库位热力图/AGV地图/事件流/设备表
```
关键约定：
- 时间纪律：任何代码只允许 clock.now() 与 update(dt)，禁止 time.time() 计时
  （墙钟仅允许出现在"服务刷新节拍/测试等待"处，不得进入仿真状态计算）；
- 计时累加一律 `round(t+dt, 9)` 防浮点漂移；
- 跨模块数据通道走事件总线，新事件类型只准加进 EventTypes；
- 所有产量/NG率等指标标注"仿真验证值"；selftest.py 的 11 用例不许退化；
- 班次2 的 REST 命令模式：新控制功能照抄 execute_command() 分发 + ui.command 审计。

【本班次交付范围】
1. vision/ —— 视觉算法升级包：
   - 覆写/注入 UnitVision.judge()：真实尺寸测量模型（多特征向量）+
     轻量分类器（如马氏距离/逻辑回归，numpy 实现），保留规则法做 A/B 对照；
   - 缺陷样本生成器（受控随机），输出混淆矩阵/准确率等"仿真验证值"。
2. mes/ —— 制造执行系统：
   - 订单模型（工单→批次→托盘追溯：product→pallet→库位全链路反查）；
   - 报工统计（班次产量/良率/OEE 近似口径），数据源=事件总线 JSONL 回放；
   - REST 扩展（/api/mes/orders 等）挂进现有 web_server 路由风格。
3. ems/ —— 能源与健康管理：
   - 设备能耗模型（按 run_seconds×功率曲线估算 kWh，标注仿真验证值）；
   - 健康评分：订阅 fault.raised/cleared + device.state，滚动窗口特征提取，
     输出各设备健康度 0~100 与维护建议（触发 enter_maintenance 预留接口）。
4. web/static 扩展：MES 工单面板 + 能耗/健康度面板（沿用现有多CDN回退加载模式）。
5. selftest.py 扩展：新增 C 组用例（视觉算法指标达标 / MES 追溯闭环 / EMS 评分合理性），
   保持既有 11 用例不退化。

【交付流程】全自动模式：先输出文件清单+依赖+启动方式，然后逐文件完整代码
（独立代码块标注路径），技术细节自行选最稳妥方案并用一行注释记录假设；
收尾给：①作品集答辩要点 ②最终自检清单。

---
（以下为班次2实测基线，供班次3回归对照——均为仿真验证值）
- 默认 seed 冒烟 600s：装配流出18件，OK18/NG0，故障注入2次，事件142条全落盘
- AGV 闭环：满托 agv.call→入库交付≈15s（车队段）；出库 FIFO 下架→出厂≈45s 内完成
- 服务端口：大屏 http://127.0.0.1:5080 | WS 5081 | Modbus 1502
- 启动命令：python main.py --web --speed 10
- 自检：python selftest.py → 11/11 通过

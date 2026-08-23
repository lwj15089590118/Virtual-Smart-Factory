# 班次2交接 Prompt 模板（复制即用）

> 用法：新开会话，把下面整段模板发给 AI，即可无缝开始班次2。
> 模板中已包含班次1最终文件树与关键接口签名，AI 无需重新探索代码库。

---

你是资深工业软件架构师。我是电气自动化专业应届生，正在做旗舰作品集项目
「Virtual-Smart-Factory 虚拟智能工厂一体化仿真平台」，分三个班次完成，
本班次只做【班次2：SCADA 监控层 + AGV 物流 + Web 可视化】。硬性要求：
① Windows 10 + Python 3.12，只用标准库+numpy+flask+pymodbus，前端 ECharts(CDN)；
② 全部代码中文注释、每个文件完整可运行，禁止省略、禁止伪代码；
③ 全软件仿真、零硬件；所有指标标注"仿真验证值"；
④ 为班次3（视觉算法/MES/EMS）预留扩展点。

【班次1 已完成（不要重写，只允许按需小改并在改动处注明"班次2修改"）】
文件树：
```
Virtual-Smart-Factory/
├─ main.py                    # Plant 全厂编排器：build()/update(dt)/run(duration)/trigger_line_estop()/reset_line()/print_status()
├─ selftest.py                # 全厂自检（9用例+600s加速冒烟），python selftest.py 必须保持 9/9 通过
├─ config/settings.py         # 全局参数中心（含 SCADA_HTTP_PORT=5080 / MODBUS_TCP_PORT=1502 预留）
├─ core/sim_clock.py          # SimClock(dt=0.1, speed)：now()/set_speed/pause/resume/start/stop/run_until(end)/advance_ticks(n)/set_step_callback(fn)
├─ core/event_bus.py          # EventBus(clock, log_dir)：subscribe(topic|"*", fn)->token / unsubscribe / publish(source,type,data,severity)->event / recent(n,etype) / replay(fn) / close()
│                             #   EventTypes 常量：device.state/fault.raised/fault.cleared/flow.product_out/vision.ok/vision.ng/
│                             #   pallet.box_placed/pallet.full/agv.call/wh.inbound_done/wh.outbound_done/clock.pause/clock.resume
├─ core/device_base.py        # DeviceBase(device_id,name,clock,bus)：state∈{停止,待机,运行,故障,维护}；io_table(DI/DO/AI/AO)；
│                             #   update(dt)必须super()；apply_fault(type,detail,origin)/clear_fault/reset()/start_up()/snapshot()
├─ core/fault_injector.py     # FaultInjector(clock,bus,devices,rng,random_rates,random_types,scripted,enabled,random_enabled)
│                             #   update(dt)/trigger(dev_id,type,duration,origin)；duration=None→需人工复位(急停)
├─ lines/product.py           # Product(product_id,born_at,source_unit,qc_result,qc_dim,rework,pallet_id) / PalletRecord(pallet_id,boxes,completed_at,location)
├─ lines/unit_assembly.py     # UnitAssembly：8步顺控 STEP_ORDER=["等待上料","上料","输送入站","定位夹紧","压装","拧紧","输送流出","流出完成"]，
│                             #   节拍=settings.ASSEMBLY_STEP_DURATIONS(默认和=32s)；set_door(open_)/take_output()->Product|None/current_step_name()/snapshot()
├─ lines/unit_vision.py       # UnitVision：inbound/outbound(deque)、rework_lane(list)、qc_records(deque)、judge(product)->("OK"/"NG",dim_mm)【班次3覆写点】、ng_rate()
├─ lines/unit_palletizing.py  # UnitPalletizing：3×4×4=48箱/托；slot_xyz(slot)/(slot_mm)(ECharts3D坐标)；BOX_PLACED事件带 px_mm/py_mm/pz_mm；垛满发 agv.call
└─ lines/warehouse.py         # Warehouse：200库位(A-{r}-{c}-{l})；request_inbound(pallet_id)/request_outbound(pallet_id|None=FIFO)/locate(id)/locations()/out_staging
```
关键约定：
- 时间纪律：任何代码只允许 clock.now() 与 update(dt)，禁止 time.time() 计时（保证加速跑批一致）；
- 计时累加一律 `round(t+dt, 9)` 防浮点漂移（已有先例）；
- 事件是唯一跨模块数据通道之一，新事件类型只准加进 EventTypes；
- 所有产量/NG率等指标标注"仿真验证值"；selftest.py 的 9 个用例不许退化。

【本班次交付范围】
1. scada/web_server.py —— Flask 服务：REST(/api/status /api/events /api/kpi /api/command) + WebSocket 实时推送
   （订阅事件总线通配符）；端口 settings.SCADA_HTTP_PORT
2. scada/modbus_server.py —— pymodbus TCP 从站：把 Plant.devices[*].io_table 映射为保持寄存器，
   供组态软件/第三方 SCADA 演示读写；端口 settings.MODBUS_TCP_PORT
3. web/static/index.html + app.js + style.css —— ECharts(CDN) 监控大屏：
   工厂流程图(单元状态色块)、产量趋势折线、NG率仪表盘、垛型3D(bar3d 用 BOX_PLACED 的 mm 坐标)、
   库位热力图(warehouse.locations())、实时事件滚动表；按钮：启动/暂停/急停/复位/开关安全门
4. agv/agv_fleet.py —— AGV 车队仿真（≥2 台）：任务状态机 空闲→去取货→装载→运输→交货→回位，
   替换 Plant._on_agv_call 占位调度；出库段接 warehouse.out_staging 运至出货口
5. main.py 扩展（最小侵入）：--web 开关启动 Flask 线程 + AGV 编排接入；命令按钮走 REST→Plant 公开方法
6. selftest.py 扩展：新增 B2(Web API 冒烟) / B3(AGV 任务闭环: agv.call→入库完成) 用例

【交付流程】全自动模式：先输出文件清单+依赖+启动方式，然后逐文件完整代码（独立代码块标注路径），
技术细节自行选最稳妥方案并用一行注释记录假设；收尾给：①班次3交接Prompt模板 ②本班次自检清单。

---
（以下为班次1实测基线数据，供班次2回归对照——均为仿真验证值）
- 900s fast@60x seed=7：装配流出27件，视觉26 OK/1 NG(3.7%)，注入3次故障，事件204条全落盘
- 600s 冒烟（默认 seed）：15件产出，9/9 自检通过

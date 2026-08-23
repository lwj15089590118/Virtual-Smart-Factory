# Virtual-Smart-Factory 虚拟智能工厂一体化仿真平台

> 旗舰作品集项目 · 全软件仿真、零硬件依赖 · 所有运行指标均为 **仿真验证值**
> 分三个班次开发：**班次1 仿真内核与产线层（当前已完成）** → 班次2 SCADA/AGV/立体库联运 → 班次3 视觉算法/MES/EMS

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

## 二、安装与启动

```bash
# 环境：Windows 10 + Python 3.12
pip install -r requirements.txt      # numpy/flask/pymodbus

# 全厂自检（逐模块 + 600 仿真秒加速冒烟，生成报告到 reports/）
python selftest.py

# 一键启动全厂（推荐先跑加速模式看全流程）
python main.py --speed 60 --mode fast --duration 900

# 实时模式（墙钟按倍率节拍），Ctrl+C 优雅停机
python main.py --mode realtime --speed 1

# 常用参数
#   --speed 1|10|60   加速倍率（接受任意正数）
#   --duration 秒     仿真时长（默认 600）
#   --seed N          随机种子（结果可复现）
#   --no-random-faults 关闭随机故障（脚本故障保留）
```

每个模块均可独立运行内置自检：`python core/sim_clock.py`、`python lines/unit_assembly.py` 等。

## 三、文件树

```
Virtual-Smart-Factory/
├─ main.py                    编排入口（Plant 全厂编排器 + 占位AGV调度 + 控制台仪表盘）
├─ selftest.py                全厂自检（9 用例 + 600s 冒烟 + 报告）
├─ requirements.txt
├─ config/settings.py         全局参数中心（节拍/垛型/库型/故障率/种子/预留端口）
├─ core/
│  ├─ sim_clock.py            仿真时钟引擎
│  ├─ event_bus.py            事件总线 + JSONL 持久化
│  ├─ device_base.py          设备基类（状态机/IO点表/统计/故障接口）
│  └─ fault_injector.py       故障注入器
├─ lines/
│  ├─ product.py              产品/托盘数据结构
│  ├─ unit_assembly.py        装配单元（8步顺控+双联锁）
│  ├─ unit_vision.py          视觉质检单元
│  ├─ unit_palletizing.py     码垛机器人单元
│  └─ warehouse.py            立体库简化模型
├─ docs/
│  ├─ HANDOVER_SHIFT2.md      班次2交接Prompt模板（含接口签名）
│  └─ CHECKLIST_SHIFT1.md     班次1自检清单
├─ logs/                      运行期生成：events_*.jsonl 事件流
└─ reports/                   运行期生成：selftest_report_*.txt 自检报告
```

## 四、关键指标（仿真验证值）

- 装配节拍 **32.0 s/件**（8 步顺控之和，可在 settings 改配）
- 视觉检测节拍 **2.5 s/件**，理论 NG 率 **≈4.6%**（σ=0.04mm，公差 ±0.08mm）
- 码垛能力 **48 箱/托**（3×4×4），单箱码放 1.2s
- 立体库容量 **200 托**（4×10×5），堆垛机单任务 25s
- 600s 加速联跑产量 **15~18 件**（含注入故障影响，仿真验证值）

## 五、后续班次挂接点速查

| 班次 | 挂接点 | 位置 |
|---|---|---|
| 2 | SCADA Web 服务（Flask REST/WebSocket） | 订阅 `EventBus.recent()/replay()`，端口 `settings.SCADA_HTTP_PORT` |
| 2 | Modbus TCP 从站 | 遍历 `Plant.devices[*].io_table` 映射寄存器，端口 `MODBUS_TCP_PORT` |
| 2 | 真实 AGV 调度 | 替换 `Plant._on_agv_call()` 占位实现 |
| 2 | AGV 搬运接入立体库 | `Warehouse.request_inbound()/request_outbound()` 即任务接口 |
| 3 | 真实视觉算法 | 覆写 `UnitVision.judge()` |
| 3 | EMS/健康模块 | 订阅 `fault.raised / device.state` 事件做特征提取 |

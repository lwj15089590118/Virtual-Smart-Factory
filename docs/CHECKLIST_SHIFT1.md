# 班次1 自检清单（Self-Check Checklist）

> 使用方法：交付验收时逐项打勾。全部满足 = 班次1 验收通过。
> 自动验证命令：`python selftest.py`（应输出 9/9 通过并生成 reports/ 报告）

## A. 硬性要求符合性

- [x] ① Windows 10 + Python 3.12 运行通过（实测 3.12.10）
- [x] ① 仅用标准库 + numpy + flask + pymodbus（班次1 实际只用标准库+numpy；flask/pymodbus 仅在 requirements 声明预留）
- [x] ② 全部代码中文注释（每个模块 docstring + 关键行注释 + 假设记录）
- [x] ② 每个文件完整可运行：8 个核心/单元文件均有 `__main__` 内置自检，可单独执行
- [x] ③ 全软件仿真、零硬件依赖
- [x] ③ 关键指标标注"仿真验证值"（README 指标表 / 自检报告 / 控制台打印）
- [x] ④ 后续班次扩展点已留（见 README 第五节速查表 + `Plant._install_extension_hooks` 注释）

## B. 十项交付范围逐项核对

| # | 交付项 | 要求点 | 验证方式 | 状态 |
|---|---|---|---|---|
| 1 | sim_clock | 统一时间源；1x/10x/60x；暂停/恢复；全厂只从时钟取时 | selftest A1 + 各模块无 time.time() 计时 | ✅ |
| 2 | event_bus | 发布/订阅 + JSONL 追加持久化，供后续模块订阅 | selftest A2（100条发布=100行落盘）| ✅ |
| 3 | device_base | 五态机、IO 点表、运行秒数/循环数/停机原因、故障接口 | selftest A3（含停机原因单次记账）| ✅ |
| 4 | fault_injector | 随机故障概率可配 + 脚本故障(设备/时间/类型)，全部产生事件 | selftest A4 + 冒烟不变量5/6 | ✅ |
| 5 | unit_assembly | 8 步顺控；安全门开→暂停；急停→全线停；节拍可配 | selftest A5（32s×10 精确产出+双联锁）| ✅ |
| 6 | unit_vision | 每件判定 OK/NG；NG 分流返修道；输出质检记录 | selftest A6（500件 NG率5.0% vs 理论4.6%）| ✅ |
| 7 | unit_palletizing | 3×4×4 垛型；垛满→托盘输出→AGV 呼叫事件 | selftest A7（48格坐标唯一+AGV呼叫）| ✅ |
| 8 | warehouse | 库位表 + 入库/出库队列（数据结构优先，班次2接AGV） | selftest A8（200库位+库位复用）| ✅ |
| 9 | main.py | 一键启动；控制台实时状态与产量统计；--speed 倍率 | 实测 fast@60x 900s 与 realtime@60x 60s | ✅ |
| 10 | selftest.py | 逐模块自检 + 10 分钟加速联跑冒烟 + 自检报告 | 9/9 PASS，报告落盘 reports/ | ✅ |

## C. 冒烟测试不变量（600s 加速联跑）

- [x] 时钟 tick 数精确 = duration/dt（6000 ticks）
- [x] 装配流出 ≈ 视觉判定 + 在制品（容差≤2）
- [x] OK 品 ≈ 码垛码箱 + 在制（容差≤2）；返修道数 = NG 数
- [x] 托盘守恒：完成托 = 在库 + 入库排队 + AGV 在途
- [x] 故障账目：raised = cleared + 生效中
- [x] 脚本故障准点触发（120s 压装压力超限 / 300s 抓取失败）
- [x] 事件 JSONL 落盘行数 = 发布条数

## D. 工程质量

- [x] 时间纪律：所有计时基于 clock.now()/dt，累加做 round(...,9) 防浮点漂移
- [x] 可复现：--seed 固定随机源（注入器与视觉各自独立种子）
- [x] 优雅停机：Ctrl+C → pause → 终报 → 关闭 JSONL 句柄
- [x] 订阅者隔离：单个坏订阅者不影响仿真主流程
- [x] 中文编码：JSONL/report UTF-8，控制台 stdout reconfigure

## E. 已知假设记录（决策留痕）

1. 原料无限供应（班次2 接立体库出库后改有限料仓）
2. 空托盘供应无限
3. 急停复位后断点续走（保留步计时器剩余量）
4. 安全门保持期间不计运行秒数（保持≠有效加工）
5. 占位 AGV 固定 15s 搬运（班次2 替换为真实车队状态机）
6. 视觉规则判定 σ=0.04mm / 公差±0.08mm → 理论 NG≈4.6%
7. 本班次堆垛机内置建模使端到端物流闭环可演示

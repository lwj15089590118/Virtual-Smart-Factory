# 阶段3交付清单 —— 视觉算法 + MES 制造执行 + EMS 能源/健康管理

> 本清单对应阶段3全部交付物。所有指标均为**仿真验证值**（Python 3.12 @ Windows 10，
> 依赖仅 标准库 + numpy + flask + pymodbus，前端 ECharts CDN）。

## 一、新增文件（12 个，全部完整可运行、中文注释）

| 文件 | 职责 | 独立自检命令 |
|---|---|---|
| `vision/__init__.py` | 包初始化 + sys.path 引导 | — |
| `vision/measure_model.py` | 多特征尺寸测量仿真模型（4特征观测+测量噪声+隐性缺陷真值口径） | `python vision/measure_model.py` |
| `vision/classifiers.py` | numpy 手写：异常得分变换器(SPC思想) / 逻辑回归 / 单类马氏T² | `python vision/classifiers.py` |
| `vision/defect_generator.py` | 缺陷样本生成器（受控随机）+ 混淆矩阵/准确率等指标 + 三方A/B评估 | `python vision/defect_generator.py` |
| `vision/vision_upgrade.py` | UnitVision.judge() 实例级注入入口；保留规则法 A/B 对照与在线混淆矩阵 | `python vision/vision_upgrade.py` |
| `mes/__init__.py` | 包初始化 | — |
| `mes/order_model.py` | 工单/批次数据模型 + 产品→托盘→批次→工单 追溯索引 | `python mes/order_model.py` |
| `mes/mes_engine.py` | MES 引擎：事件总线自动报工、追溯反查、OEE 近似口径 | `python mes/mes_engine.py` |
| `mes/jsonl_replay.py` | JSONL 回放器：离线重建 MES 台账（含 CLI 报告） | `python mes/jsonl_replay.py [路径]` |
| `ems/__init__.py` | 包初始化 | — |
| `ems/energy_model.py` | 能耗模型：状态功率曲线分段积分 → kWh/电费/CO₂（仿真验证值） | `python ems/energy_model.py` |
| `ems/health_monitor.py` | 健康评分：滚动窗口特征提取 → 0~100 分 + 维护建议 + 告警 + 维护接口 | `python ems/health_monitor.py` |

## 二、修改文件（9 个，改动处均注明"阶段3修改"）

| 文件 | 改动摘要 |
|---|---|
| `config/settings.py` | 追加第 8~11 节：视觉算法/MES/EMS功率曲线/健康评分 参数段 |
| `core/event_bus.py` | EventTypes 追加 `mes.order_created / mes.order_closed / ems.health_alert / ems.maintenance` |
| `lines/unit_vision.py` | update() 判定明细回填钩子 + snapshot() 导出算法档案（约10行，行为向后兼容） |
| `main.py` | `_install_extension_hooks()` 装配视觉算法/MES/EMS 三件套；execute_command 新增 `mes_new_order`、`ems_maintain`、`ems_maintain_done`；shutdown 终报追加 MES/EMS 摘要；CLI 新增 `--rule-vision` 回归对照开关 |
| `scada/web_server.py` | 新增 REST：`/api/mes/orders` `/api/mes/batches` `/api/mes/trace?query=` `/api/ems/energy` `/api/ems/health`；独立冒烟扩为十端点 |
| `web/static/index.html` | 新增面板⑨ MES 工单与追溯（含追溯查询框）、面板⑩ 能耗·健康度 |
| `web/static/app.js` | 新增 pollMes/pollEms 轮询、能耗条形图、追溯查询渲染、新事件中文名映射 |
| `web/static/style.css` | 新增面板样式（chips/迷你进度条/追溯结果框/健康分色阶） |
| `selftest.py` | 新增 C 组用例 C1/C2/C3；报告头更新为阶段3 |

## 三、核心指标（仿真验证值）

### 视觉算法 A/B 对照（训练1500件 / 独立测试2000件）
| 方法 | 准确率 | 查准率 | 查全率 | F1 | 漏检率 |
|---|---|---|---|---|---|
| 阶段1规则法(单尺寸) | 97.45% | 93.02% | 64.00% | 75.83% | 36.00% |
| **逻辑回归(本阶段主模型)** | **98.95%** | **98.15%** | **84.80%** | **90.99%** | **15.20%** |
| 单类马氏距离(Hotelling T²) | 96.00% | 81.69% | 46.40% | 59.18% | 53.60% |

- 真值口径：NG = 尺寸超差(≈4.6%，继承阶段1理论值) ∪ 公差带内隐性缺陷(≈2.5%) → 综合 ≈7%
- 在线判定（600s联跑）：NG率 5.6%，在线混淆矩阵账目自洽
- 关键技术点：原始特征空间线性不可分（OR结构），经"健康基线异常得分+越限深度"
  特征工程后线性可分——这是查全率 64%→84.8% 的来源

### MES 报工（C2 用例，48件直灌+装配并行产出）
- 报工 OK54/NG2，良率 96.4%，OEE≈90.0%（A×P×Q 近似口径）
- 四级反查：产品 Pxxxxxxxx → 托盘 PLTxxxxxx → 批次 WO-0001-B01 → 工单 WO-0001 → 库位
- JSONL 回放重建台账与在线台账一致（stat_ok/stat_ng/追溯链路全等）

### EMS 能耗与健康
- 功率曲线积分精确：60s×12kW=0.200kWh（误差<0.01），电费/CO₂ 换算正确
- 健康评分方向性验证：无故障≥98 → 急停40s后89.5 → 连续故障46.5（跌破60告警）
- 维护闭环：REST 命令 `ems_maintain`→维护态→`ems_maintain_done`→待机，全程审计落盘

## 四、启动方式（不变）

```
python main.py --web --speed 10     # 大屏 http://127.0.0.1:5080 | WS 5081 | Modbus 1502
python selftest.py                  # 全厂自检（14用例：A1~A8 + B1~B3 + C1~C3）
python mes/jsonl_replay.py          # 离线回放最新事件文件出 MES 报告
```

## 五、回归对照基线（阶段3实测 vs 阶段2基线，均为仿真验证值）

| 项目 | 阶段2基线 | 阶段3实测 |
|---|---|---|
| 默认种子 600s 冒烟 | 流出18件 OK18/NG0 故障2次 事件142条 | 流出18件 OK17/NG1(NG率5.6%) 故障2次 事件140条 |
| 自检 | 11/11 | **14/14**（原11用例无退化） |
| 服务端口 | 5080/5081/1502 | 不变 |

> NG 从 0→1 属预期：升级版判定把"公差带内隐性缺陷"也纳入了 NG 口径（真值先验≈7%），
> 长跑样本少时逐件波动属正常。需要复现阶段1规则法口径时加 `--rule-vision`。

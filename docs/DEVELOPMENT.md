# 开发指南（DEVELOPMENT.md）

> 面向继续开发/评审本项目的开发者。运行时依赖原则不变：
> **标准库 + numpy + flask + pymodbus**；pytest/ruff 属于开发期工具，
> 装在 `requirements-dev.txt`，不污染运行环境。

## 一、环境与常用命令

```bash
# 运行环境（Windows 10 / Python 3.12）
pip install -r requirements.txt

# 开发工具链（一次性）
pip install -r requirements-dev.txt        # pytest / pytest-cov / ruff

# —— 质量三件套 ——
python -m ruff check .                     # 静态检查（E4/E7/E9+F 基线）
python -m pytest -m "not smoke"            # 快速测试层（跳过 600s 联跑冒烟）
python -m pytest                           # 全量 17 用例（含冒烟）

# 覆盖率（核心模块口径）
python -m pytest -q --cov=core --cov=lines --cov=agv --cov=scada --cov=mes --cov=ems \
       --cov-report=term

# 长程稳定性压测（多"仿真日"加速挂机；产出 reports/soak_report_*.txt 与 logs/soak_metrics_*.csv）
python soak_run.py --days 30 --sample-min 60
python soak_run.py --sim-hours 0.5 --sample-min 10 --tag calib   # 快速标定吞吐/验证链路

# —— 原有入口不变 ——
python selftest.py                         # 全厂自检 17 用例 → reports/ 报告
python main.py --web --speed 10            # 监控大屏 http://127.0.0.1:5080
python mes/jsonl_replay.py                 # 离线回放 MES 报表

# 各模块独立自检（含真实 socket/客户端链路的那几个尤其值得单跑）
python scada/ws_hub.py                     # 真实 TCP 全链路 WebSocket 收发
python scada/modbus_server.py              # 真实 pymodbus 客户端读写闭环
```

Windows 商店版 Python 的 Scripts 目录常不在 PATH，统一用 `python -m` 调用最稳。

## 二、测试体系：三层结构

| 层 | 入口 | 特点 |
|---|---|---|
| 模块自检 | `python core/sim_clock.py` 等 | 每个文件自带行为断言；**真实 socket/客户端链路只在这层验证** |
| pytest 薄壳 | `tests/test_plant_selftest.py` | 参数化转接 selftest 的 16 个用例函数 + smoke 标记的 B1 冒烟；**零重复实现**——薄壳只调用不复制断言，两套入口永远测同一份逻辑 |
| 全厂自检 | `selftest.py` | 原有入口：顺序执行 17 用例并产出 `reports/selftest_report_*.txt`（CI 保留） |

约定：**新增用例一律写在 selftest.py 并登记进 tests 薄壳的 `_CASES` 表**，
不要绕开 selftest 另起炉灶——保证 `pytest 全绿 ⇔ selftest 全绿`。

## 三、ruff 规则说明

- 基线 = ruff 默认组 `select = ["E4", "E7", "E9", "F"]`：语法错误、未定义名、
  未用导入/变量、歧义命名等高价值规则；不开全量风格规则，存量代码不做机器重排。
- 行宽 140 只对未选中的 E501 生效，当前等于无行宽限制；中文注释较长的现状不受影响。
- 确属故意的用法就地加 `# noqa: F401`（包 `__init__` 的显式再导出已有先例）。
- 配置位置：`pyproject.toml` 的 `[tool.ruff]` / `[tool.ruff.lint]`。

## 四、覆盖率快照（2026-08，pytest 全量口径）

- **总计 65%**；产线/调度核心：装配 84%、码垛 83%、AGV 车队 82%、事件总线 81%。
- ws_hub(21%) 与 modbus_server(27%) 偏低属预期：它们的**真实网络收发**
  （socket 握手/帧编解码、pymodbus 客户端读写）在各自 `__main__` 自检里验证，
  pytest 薄壳不覆盖——需要时 `python scada/ws_hub.py`、`python scada/modbus_server.py`
  单独跑通即等价覆盖。
- sqlite_ledger(53%)、jsonl_replay(52%)：主链路已被 C2/C4 用例覆盖，
  未覆盖部分为 CLI 打印排版函数。

## 五、CI 流水线（GitHub Actions, ubuntu-latest, ≤15min）

1. 安装 运行时 + 开发依赖；
2. `compileall` 语法检查；
3. `ruff check .`（新增，2026-08）；
4. `pytest -q`（新增，含 B1 冒烟）；
5. `python selftest.py` 生成自检报告（历史入口保留）。

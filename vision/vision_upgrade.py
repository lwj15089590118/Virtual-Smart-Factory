# -*- coding: utf-8 -*-
"""
vision/vision_upgrade.py —— UnitVision.judge() 覆写/注入入口（班次3新增）
==========================================================================
职责（交付范围1 的落地点）：
    1. VisionAlgorithmV3：训练一次轻量逻辑回归（受控随机样本，种子固定），
       对每件在检品采样多特征观测向量 → 模型推理 → 输出 ("OK"/"NG", dim_mm)；
    2. install_vision_upgrade()：把 UnitVision 实例的 judge() 以【实例级注入】
       方式替换为升级算法（原类定义零改动，班次1/2 回归路径原样保留）；
       班次1规则法在注入算法内部并行执行，形成 A/B 对照：
       - 判定明细写入 unit_vision.last_judge_detail（update() 回填进 qc_records）；
       - 在线混淆矩阵（算法 vs 仿真真值）滚动累计在 unit_vision.ab_stats，
         作为运行期"仿真验证值"指标（Web 端可读）。

时间纪律：
    判定是纯函数式计算（采样+矩阵运算），不涉及任何时钟。

假设记录：
    - 训练数据与在线数据同分布（同一 MeasureSimulator 观测模型）——
      现实中对应"离线标注样本训练、在线同工况推理"的标准视觉工程链路。
"""

import os
import sys
# 路径引导：直接运行本文件(python vision/vision_upgrade.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional

import numpy as np

from config import settings as S
from vision.measure_model import MeasureSimulator
from vision.defect_generator import DefectSampleGenerator, build_models, metrics


class VisionAlgorithmV3:
    """升级判定算法：异常特征工程 + 逻辑回归主模型 + 规则法并行对照。"""

    ALGO_ID = "LR-v3(健康基线异常特征+逻辑回归)"

    def __init__(self, seed: int):
        # ---- 离线训练：固定种子生成受控样本集并训练（进程内一次）----
        self._train_seed = int(seed) + 300
        train = DefectSampleGenerator(self._train_seed).generate(S.VISION_TRAIN_N)
        # build_models 与评估流水线完全同源：健康参考批 → 异常变换器 → LR / 马氏
        models = build_models(train)
        self.transformer = models["transformer"]
        self.model = models["lr"]
        self.model_mahalanobis = models["mahalanobis"]   # 保留第二算法（展示/对照）
        # 训练集自评（答辩展示拟合水平；仿真验证值）
        self.train_metrics = metrics(
            train["y"], self.model.predict(self.transformer.transform(train["X"])))
        # ---- 在线采样器：独立随机流（与训练流隔离，保证长跑可复现）----
        self.sampler = MeasureSimulator(np.random.default_rng(int(seed) + 301))
        # ---- 在线混淆矩阵（算法判定 vs 仿真真值；运行期滚动累计）----
        self.online = {"n": 0, "TP": 0, "FN": 0, "TN": 0, "FP": 0,
                       "rule_TP": 0, "rule_FN": 0, "rule_TN": 0, "rule_FP": 0,
                       "agree_rule": 0}
        # 最近一次判定明细（由 install 的闭包回填到 UnitVision.last_judge_detail）
        self.last_detail: Optional[dict] = None

    # ------------------------------------------------------------------
    def judge(self) -> tuple:
        """
        执行一次"测量+推理"：返回 (结果, dim_mm)。
        注意：本方法只做计算，不触碰 UnitVision；副作用统一写在 self.last_detail，
        由 install_vision_upgrade 的包装闭包负责转交。
        """
        s = self.sampler.sample_item()
        x_feat = self.transformer.transform(s["features"].reshape(1, -1))
        p_ng = float(self.model.predict_proba(x_feat)[0])
        result = "NG" if p_ng >= self.model.threshold else "OK"
        rule = self.sampler.rule_judge_features(s["features"])

        # ---- 在线混淆矩阵累计（真值仅仿真可观测）----
        y = s["y_true"]
        yhat = 1 if result == "NG" else 0
        rhat = 1 if rule == "NG" else 0
        self.online["n"] += 1
        self.online["TP" if (y == 1 and yhat == 1) else
                    "FN" if (y == 1 and yhat == 0) else
                    "FP" if (y == 0 and yhat == 1) else "TN"] += 1
        self.online["rule_" + ("TP" if (y == 1 and rhat == 1) else
                               "FN" if (y == 1 and rhat == 0) else
                               "FP" if (y == 0 and rhat == 1) else "TN")] += 1
        self.online["agree_rule"] += int(yhat == rhat)

        # ---- 判定明细（update() 会把它并入 qc_records 与 vision.ok/ng 事件）----
        scores = self.transformer._scores(s["features"].reshape(1, -1))[0]
        self.last_detail = {
            "algo": self.ALGO_ID,                       # 判定算法名
            "clf_p_ng": round(p_ng, 4),                 # 模型输出 P(NG)
            "rule_result": rule,                        # 规则法 A/B 对照结论
            "agree_rule": bool(yhat == rhat),           # 与规则法是否一致
            "features": {name: round(float(v), 3)          # 原始多特征观测向量
                         for name, v in zip(S.VISION_FEATURE_NAMES, s["features"])},
            "anomaly_scores": [round(float(v), 2) for v in scores],  # 异常得分(4维)
            "hidden_defect": s["defect_type"],          # 仿真真值（仅全软件仿真可观测）
        }
        return result, s["dim_mm"]

    # ------------------------------------------------------------------
    def online_metrics(self) -> dict:
        """把在线混淆矩阵换算成准确率/查全率等（运行期仿真验证值）。"""
        o = self.online
        n = max(o["n"], 1)
        ng = o["TP"] + o["FN"]
        ok = o["TN"] + o["FP"]
        return {
            "n": o["n"],
            "clf_acc": round((o["TP"] + o["TN"]) / n, 4),
            "clf_recall": round(o["TP"] / ng, 4) if ng else None,
            "rule_acc": round((o["rule_TP"] + o["rule_TN"]) / n, 4),
            "rule_recall": round(o["rule_TP"] / ng, 4) if ng else None,
            "agree_rate": round(o["agree_rule"] / n, 4),
            "confusion": {k: o[k] for k in ("TP", "FN", "TN", "FP")},
        }

    def snapshot(self) -> dict:
        """算法档案快照（Web 面板/自检报告引用）。"""
        return {
            "algo": self.ALGO_ID,
            "train_n": S.VISION_TRAIN_N,
            "train_acc": self.train_metrics["accuracy"],
            "train_recall": self.train_metrics["recall"],
            "online": self.online_metrics(),
        }


def install_vision_upgrade(unit_vision, seed: int = S.DEFAULT_SEED) -> VisionAlgorithmV3:
    """
    把升级算法注入 UnitVision 实例（实例属性覆盖类方法，原类零侵入）：
        - unit_vision.judge        ← VisionAlgorithmV3.judge 包装闭包
        - unit_vision.last_judge_detail ← 每次判定明细（update 回填 qc_records）
        - unit_vision.ab_stats     ← 在线 A/B 对照统计（snapshot 可导出）
    返回算法实例（调用方可进一步读取训练指标/在线混淆矩阵）。
    """
    algo = VisionAlgorithmV3(seed)

    def judge_v3(_product):
        # 假设记录：产品对象本身不携带工艺差异，测量过程由算法内采样器统一仿真
        ret = algo.judge()
        # 把本轮判定明细转交到单元实例上（update() 会取走并入 qc_records）
        unit_vision.last_judge_detail = algo.last_detail
        return ret

    unit_vision.judge = judge_v3                       # 实例级覆写（注入点）
    unit_vision.last_judge_detail = None               # 判定明细暂存位
    unit_vision.ab_stats = algo.online                 # 在线混淆矩阵（引用共享）
    unit_vision.algo_info = algo.snapshot()            # 算法档案（snapshot 导出）
    return algo


# ----------------------------------------------------------------------
# 自模块快速自检：python vision/vision_upgrade.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from core.sim_clock import SimClock
    from core.event_bus import EventBus, EventTypes
    from lines.unit_vision import UnitVision
    from lines.product import Product

    clock = SimClock(dt=0.1)
    bus = EventBus(clock, persist=False)
    vis = UnitVision(clock, bus, unit_id="VIS-UP", rng=np.random.default_rng(1))
    algo = install_vision_upgrade(vis, seed=S.DEFAULT_SEED)
    assert vis.algo_info["algo"] == VisionAlgorithmV3.ALGO_ID

    vis.start_up()
    ok_ev, ng_ev = [], []
    bus.subscribe(EventTypes.VISION_OK, lambda e: ok_ev.append(e))
    bus.subscribe(EventTypes.VISION_NG, lambda e: ng_ev.append(e))
    n = 300
    for i in range(n):
        vis.inbound.append(Product(f"UP{i:08d}", born_at=0.0, source_unit="T"))
    clock.advance_ticks(int(n * S.VISION_INSPECT_TIME * 1.3 / clock.dt),
                        step_fn=vis.update)

    judged = vis.ok_total + vis.ng_total
    assert judged == n, f"注入后应仍全量判定: {judged}"
    # 记录明细回填验证：qc_records 应带算法字段
    rec = vis.qc_records[-1]
    assert "algo" in rec and "clf_p_ng" in rec and "rule_result" in rec, \
        f"判定明细未回填: {rec.keys()}"
    # 在线混淆矩阵账目自洽
    om = algo.online_metrics()
    assert om["n"] == n, f"在线样本数不符: {om['n']} vs {n}"
    c = om["confusion"]
    assert c["TP"] + c["FN"] + c["TN"] + c["FP"] == n, "混淆矩阵账目不平"
    assert om["clf_acc"] >= 0.85, f"在线准确率异常: {om['clf_acc']}"
    print(f"[vision_upgrade 自检通过] 判定{n}件, 在线准确率={om['clf_acc']*100:.1f}%, "
          f"与规则法一致率={om['agree_rate']*100:.1f}% (仿真验证值)")
    print("  算法档案:", {k: v for k, v in vis.algo_info.items() if k != "online"})

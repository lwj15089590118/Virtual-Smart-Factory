# -*- coding: utf-8 -*-
"""
vision/defect_generator.py —— 缺陷样本生成器 + 算法评估（阶段3新增）
====================================================================
职责：
    1. 受控随机批量生成"带真值标签"的缺陷样本数据集
       （复用 MeasureSimulator 的同一观测模型，保证训练/在线/评估同分布）；
    2. 提供混淆矩阵与准确率/查准率/查全率/F1 等指标计算（纯 numpy）；
    3. evaluate_classifiers()：一次跑完 规则法 / 逻辑回归 / 马氏距离 三方
       A/B 对照实验，输出可直接写进答辩材料的"仿真验证值"。

时间纪律：
    纯离线统计模块，不涉及任何时钟。

假设记录：
    - 训练集/测试集按 70%/30% 切分（有放回洗牌由固定种子随机流完成）。
"""

import os
import sys
# 路径引导：直接运行本文件(python vision/defect_generator.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, List

import numpy as np

from config import settings as S
from vision.measure_model import MeasureSimulator
from vision.classifiers import (AnomalyTransformer, LogisticRegressionLite,
                                MahalanobisClassifier)


# ======================================================================
# 数据集生成
# ======================================================================
class DefectSampleGenerator:
    """缺陷样本生成器：受控随机产出带真值的多特征样本。"""

    def __init__(self, seed: int):
        # 假设记录：种子固定 → 同一版本代码任何人重跑得到同一份数据集（可复现性）
        self.sim = MeasureSimulator(np.random.default_rng(int(seed)))

    def generate(self, n: int) -> dict:
        """
        生成 n 件样本。
        返回 dict：
            X            (n,4) 特征矩阵
            y            (n,)  真值标签 1=NG
            rule_pred    (n,)  规则法判定 1=NG（A/B 对照基准）
            defect_types List[str] 缺陷子类型（健康件="无"）
            dim_list     List[float] 关键尺寸观测值 mm
        """
        feats = np.empty((n, len(S.VISION_FEATURE_NAMES)), dtype=float)
        y = np.empty(n, dtype=int)
        rule = np.empty(n, dtype=int)
        types: List[str] = []
        dims: List[float] = []
        for i in range(n):
            s = self.sim.sample_item()
            feats[i] = s["features"]
            y[i] = s["y_true"]
            rule[i] = 1 if self.sim.rule_judge_features(s["features"]) == "NG" else 0
            types.append(s["defect_type"])
            dims.append(s["dim_mm"])
        return {"X": feats, "y": y, "rule_pred": rule,
                "defect_types": types, "dim_list": dims}


def train_test_split(X, y, rule=None, test_ratio: float = 0.3, seed: int = 0) -> dict:
    """固定种子的随机切分（numpy 实现，不引入 sklearn）。"""
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    n_test = max(1, int(len(y) * test_ratio))
    te_idx, tr_idx = idx[:n_test], idx[n_test:]
    out = {"X_train": X[tr_idx], "y_train": y[tr_idx],
           "X_test": X[te_idx], "y_test": y[te_idx],
           "rule_pred_test": None if rule is None else rule[te_idx]}
    return out


# ======================================================================
# 指标计算（正类=NG=1）
# ======================================================================
def confusion_matrix(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, int]:
    """混淆矩阵四要素：TP(真NG判NG)/FN(真NG漏检)/TN(真OK判OK)/FP(真OK误杀)。"""
    y_true = np.asarray(y_true).astype(int)
    y_pred = np.asarray(y_pred).astype(int)
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    return {"TP": tp, "FN": fn, "TN": tn, "FP": fp}


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """由混淆矩阵导出常用指标（全部保留4位小数，仿真验证值）。"""
    cm = confusion_matrix(y_true, y_pred)
    tp, fn, tn, fp = cm["TP"], cm["FN"], cm["TN"], cm["FP"]
    acc = (tp + tn) / max(tp + tn + fp + fn, 1)
    precision = tp / max(tp + fp, 1)          # 查准率：报的 NG 里真是 NG 的比例
    recall = tp / max(tp + fn, 1)             # 查全率：真 NG 被抓住的比例（漏检率的补）
    specificity = tn / max(tn + fp, 1)        # 特异度：真 OK 被放行的比例
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    return {"accuracy": round(acc, 4), "precision": round(precision, 4),
            "recall": round(recall, 4), "specificity": round(specificity, 4),
            "f1": round(f1, 4), "miss_rate": round(fn / max(tp + fn, 1), 4),
            "confusion": {"TP": tp, "FN": fn, "TN": tn, "FP": fp}}


# ======================================================================
# 三方 A/B 对照评估（规则法 / 逻辑回归 / 单类马氏）
# ======================================================================
def build_models(train: dict) -> dict:
    """
    用训练集训练全部模型（评估与在线判定共用同一流水线）：
        1) 健康参考批 = 训练集中真值 OK 的子集（现实中对应"已知良品批"）；
        2) AnomalyTransformer 在健康参考批上估计基线/控制限；
        3) 逻辑回归在异常特征空间训练；
        4) 单类马氏在原始空间拟合+校准。
    返回 {"transformer", "lr", "mahalanobis"}。
    """
    X, y = train["X"], train["y"]
    ref = X[y == 0]                                   # 已知良品参考批
    transformer = AnomalyTransformer().fit(ref)
    lr = LogisticRegressionLite().fit(transformer.transform(X), y)
    mh = MahalanobisClassifier(alpha=0.99).fit(ref).calibrate(ref)
    return {"transformer": transformer, "lr": lr, "mahalanobis": mh}


def evaluate_classifiers(seed: int = S.DEFAULT_SEED,
                         train_n: int = None, test_n: int = 2000) -> dict:
    """
    完整评估流水线：
        1) 用训练种子生成训练集并训练 LR 与马氏分类器；
        2) 用另一个种子生成独立测试集（防"考试题就是练习题"）；
        3) 分别计算三种方法的指标，返回汇总 dict（全部为仿真验证值）。
    """
    train_n = int(train_n if train_n is not None else S.VISION_TRAIN_N)
    train = DefectSampleGenerator(seed).generate(train_n)
    test = DefectSampleGenerator(seed + 10000).generate(test_n)
    models = build_models(train)

    return {
        "train_n": train_n, "test_n": test_n,
        "feature_names": list(S.VISION_FEATURE_NAMES),
        "rule": metrics(test["y"], test["rule_pred"]),
        "logistic_regression": metrics(
            test["y"], models["lr"].predict(models["transformer"].transform(test["X"]))),
        "mahalanobis": metrics(test["y"], models["mahalanobis"].predict(test["X"])),
        "lr_params": models["lr"].to_dict(),
        "loss_tail": models["lr"].loss_history[-3:],
    }


def format_report(ev: dict) -> str:
    """把评估结果排版成可读文本（自检报告/答辩材料直接引用）。"""
    lines = [
        f"视觉算法 A/B 对照（训练{ev['train_n']}件 / 独立测试{ev['test_n']}件，"
        f"特征={ev['feature_names']}，均为仿真验证值）",
        f"{'方法':<10}{'准确率':>8}{'查准':>8}{'查全':>8}{'F1':>8}{'漏检率':>8}",
    ]
    for key, name in (("rule", "规则法"), ("logistic_regression", "逻辑回归"),
                      ("mahalanobis", "马氏距离")):
        m = ev[key]
        lines.append(f"{name:<10}{m['accuracy']*100:>7.2f}%{m['precision']*100:>7.2f}%"
                     f"{m['recall']*100:>7.2f}%{m['f1']*100:>7.2f}%"
                     f"{m['miss_rate']*100:>7.2f}%")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 自模块快速自检：python vision/defect_generator.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ev = evaluate_classifiers(seed=S.DEFAULT_SEED)
    print(format_report(ev))
    # 核心达标断言（C 组用例同款口径，此处先行验证）
    assert ev["logistic_regression"]["accuracy"] >= S.VISION_CLF_ACC_MIN, "LR 准确率不达标"
    assert ev["logistic_regression"]["recall"] > ev["rule"]["recall"] + 0.10, \
        "多特征算法对'公差带内缺陷'的查全率优势未体现"
    assert ev["logistic_regression"]["f1"] > ev["rule"]["f1"], \
        "分类器综合 F1 不应低于规则法"
    print("[defect_generator 自检通过] 三方对照达标 (仿真验证值)")

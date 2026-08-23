# -*- coding: utf-8 -*-
"""
vision/classifiers.py —— numpy 手写轻量分类器（班次3新增）
============================================================
提供三种不依赖任何深度学习框架的算法（硬性要求④）：
    1. AnomalyTransformer 异常得分变换器：
       把原始观测向量变换为"相对健康基线"的统计量——
       前4维 = 各特征相对健康均值/方差的标准化异常得分（|z| 或单侧 z），
       后4维 = 越限深度（超出健康批 99.5% 分位控制限的部分，SPC 控制限思想）。
       这一步把"任一特征显著异常即 NG"的 OR 逻辑变成线性可分结构，
       是有限缺陷样本条件下工业视觉的常用特征工程手段；
    2. LogisticRegressionLite 逻辑回归：批量梯度下降 + L2 正则，
       在异常特征空间输出 P(NG)，作为在线判定主模型；
    3. MahalanobisClassifier 单类马氏距离（Hotelling T² 思想）：
       只用健康参考批估计均值/协方差，样本离健康中心过远即判 NG，
       阈值按健康批距离的经验分位数校准——作为 A/B 对照的第二算法。
三者都支持参数导出（to_dict），作品集可做持久化/回载演示。

假设记录：
    - 特征维度仅 4 维、样本千级，批量 GD 数千轮即可收敛，无需更高阶优化器。
"""

import os
import sys
# 路径引导：直接运行本文件(python vision/classifiers.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


# ======================================================================
# 特征工程：健康基线异常得分（SPC 思想）
# ======================================================================
class AnomalyTransformer:
    """
    把原始观测向量 (n,4) 变换为异常特征 (n,8)：
        前4维 anomaly score：
            f0 尺寸偏差  双侧 |z|（偏大/偏小都是缺陷）
            f1 圆度um    单侧 max(z,0)（只有偏大才是缺陷）
            f2 表面得分  单侧 max(-z,0)（只有偏低才是缺陷）
            f3 边缘锐度  单侧 max(-z,0)（只有偏低才是缺陷）
        后4维 exceedance depth：
            max(score - q_ref, 0)，q_ref 为健康参考批各维得分的分位数控制限
    fit() 只允许喂"健康参考批"样本（训练集中规则法+真值均判 OK 的子集）。
    """

    # 每特征的单侧方向："both"=双侧 / "high"=偏大异常 / "low"=偏低异常
    SIDES = ("both", "high", "low", "low")

    def __init__(self, quantile: float = None):
        from config import settings as _S   # 延迟导入，保持本模块可独立复用
        self.quantile = float(quantile if quantile is not None
                              else _S.VISION_REF_QUANTILE)
        self.ref_mean = None            # 健康基线均值（逐特征）
        self.ref_std = None             # 健康基线标准差（逐特征）
        self.control_lim = None         # 各维得分控制限（分位数）

    def fit(self, X_ref: np.ndarray) -> "AnomalyTransformer":
        X = np.asarray(X_ref, dtype=float)
        assert len(X) >= 30, "健康参考批样本过少，无法估计基线"
        self.ref_mean = X.mean(axis=0)
        std = X.std(axis=0)
        self.ref_std = np.where(std < 1e-12, 1.0, std)
        scores = self._scores(X)
        self.control_lim = np.quantile(scores, self.quantile, axis=0)
        return self

    def _scores(self, X: np.ndarray) -> np.ndarray:
        """标准化异常得分（前4维）。"""
        Z = (np.asarray(X, dtype=float) - self.ref_mean) / self.ref_std
        cols = []
        for j, side in enumerate(self.SIDES):
            if side == "both":
                cols.append(np.abs(Z[:, j]))
            elif side == "high":
                cols.append(np.maximum(Z[:, j], 0.0))
            else:                                   # "low"
                cols.append(np.maximum(-Z[:, j], 0.0))
        return np.stack(cols, axis=1)

    def transform(self, X: np.ndarray) -> np.ndarray:
        """输出 (n,8) 异常特征 = [得分4维 | 越限深度4维]。未训练则报错。"""
        if self.control_lim is None:
            raise RuntimeError("AnomalyTransformer 尚未 fit，禁止 transform")
        scores = self._scores(X)
        exceed = np.maximum(scores - self.control_lim, 0.0)
        return np.hstack([scores, exceed])

    def score_names(self) -> list:
        """变换后 8 维特征的名称（记录/答辩展示用）。"""
        from config import settings as _S
        raw = list(_S.VISION_FEATURE_NAMES)
        return [f"异常得分·{n}" for n in raw] + [f"越限深度·{n}" for n in raw]


# ======================================================================
# 逻辑回归（在线判定主模型）
# ======================================================================
class LogisticRegressionLite:
    """二分类逻辑回归（NG=1 / OK=0），numpy 批量梯度下降实现。"""

    def __init__(self, lr: float = None, epochs: int = None,
                 l2: float = 1e-4, threshold: float = 0.5):
        # 假设记录：默认超参取自参数中心（班次3配置段），统一调参入口
        from config import settings as _S
        self.lr = float(lr if lr is not None else _S.VISION_LR_RATE)
        self.epochs = int(epochs if epochs is not None else _S.VISION_LR_EPOCHS)
        self.l2 = float(l2)
        self.threshold = float(threshold)
        self.w = None            # 权重向量 shape=(d+1,)，末位为偏置
        self.loss_history = []   # 训练损失曲线（答辩展示收敛性）

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticRegressionLite":
        """训练：加权交叉熵损失 + L2 正则的批量梯度下降（异常特征无需标准化）。"""
        Xb = self._design(X)
        yv = np.asarray(y, dtype=float).reshape(-1)
        w = np.zeros(Xb.shape[1])
        self.loss_history = []
        for epoch in range(self.epochs):
            p = self._sigmoid(Xb @ w)
            grad = Xb.T @ (p - yv) / len(yv) + self.l2 * w
            w -= self.lr * grad
            if epoch % 50 == 0 or epoch == self.epochs - 1:   # 稀疏记录损失曲线
                eps = 1e-12
                loss = -np.mean(yv * np.log(p + eps) + (1 - yv) * np.log(1 - p + eps))
                self.loss_history.append(round(float(loss), 6))
        self.w = w
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """返回 P(NG) 概率（未训练则报错，防止静默给出垃圾结果）。"""
        if self.w is None:
            raise RuntimeError("逻辑回归尚未训练，禁止推理")
        return self._sigmoid(self._design(X) @ self.w)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """按阈值（默认0.5）输出 0/1 标签。"""
        return (self.predict_proba(X) >= self.threshold).astype(int)

    # ------------------------------------------------------------------
    @staticmethod
    def _design(X: np.ndarray) -> np.ndarray:
        """设计矩阵：原始特征后拼一列全 1 作为偏置维度。"""
        X = np.asarray(X, dtype=float)
        return np.hstack([X, np.ones((X.shape[0], 1))])

    @staticmethod
    def _sigmoid(z: np.ndarray) -> np.ndarray:
        """数值稳定的 sigmoid（防指数溢出）。"""
        out = np.empty_like(z, dtype=float)
        pos = z >= 0
        out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
        ez = np.exp(z[~pos])
        out[~pos] = ez / (1.0 + ez)
        return out

    def to_dict(self) -> dict:
        """参数导出（JSON 可序列化）。"""
        return {"model": "LogisticRegressionLite", "lr": self.lr,
                "epochs": self.epochs, "l2": self.l2, "threshold": self.threshold,
                "w": None if self.w is None else self.w.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticRegressionLite":
        m = cls(lr=d["lr"], epochs=d["epochs"], l2=d["l2"], threshold=d["threshold"])
        m.w = np.array(d["w"])
        return m


# ======================================================================
# 单类马氏距离（Hotelling T² 思想，A/B 对照第二算法）
# ======================================================================
class MahalanobisClassifier:
    """
    单类分类器：只用健康参考批估计中心 μ 与协方差 Σ，
    样本到健康中心的马氏距离 √((x-μ)ᵀΣ⁻¹(x-μ)) 超过控制限即判 NG；
    控制限 = 健康批距离的经验分位数（默认 99%，对应 SPC 的受控误报率）。
    """

    def __init__(self, alpha: float = 0.99, ridge: float = 1e-9):
        self.alpha = float(alpha)   # 控制限分位数（1-α 即理论误报率）
        self.ridge = float(ridge)   # 协方差对角加载，防奇异
        self.mean_ = None
        self.cov_inv_ = None
        self.threshold_ = None      # 距离控制限

    def fit(self, X_ref: np.ndarray) -> "MahalanobisClassifier":
        X = np.asarray(X_ref, dtype=float)
        assert len(X) >= 30, "健康参考批样本过少，无法估计协方差"
        self.mean_ = X.mean(axis=0)
        d = X - self.mean_
        cov = d.T @ d / (len(X) - 1)
        self.cov_inv_ = np.linalg.pinv(cov + np.eye(cov.shape[0]) * self.ridge)
        return self

    def calibrate(self, X_ref: np.ndarray) -> "MahalanobisClassifier":
        """用健康批自身距离的经验分位数定控制限（须与 fit 同一批）。"""
        dists = self.distances(X_ref)
        self.threshold_ = float(np.quantile(dists, self.alpha))
        return self

    def distances(self, X: np.ndarray) -> np.ndarray:
        """逐样本马氏距离（未训练则报错）。"""
        if self.cov_inv_ is None:
            raise RuntimeError("马氏分类器尚未 fit，禁止推理")
        d = np.asarray(X, dtype=float) - self.mean_
        return np.sqrt(np.einsum("ij,jk,ik->i", d, self.cov_inv_, d))

    def predict(self, X: np.ndarray) -> np.ndarray:
        """距离超限判 NG（1），否则 OK（0）。"""
        if self.threshold_ is None:
            raise RuntimeError("马氏分类器尚未 calibrate，禁止推理")
        return (self.distances(X) > self.threshold_).astype(int)

    def to_dict(self) -> dict:
        return {"model": "MahalanobisClassifier", "alpha": self.alpha,
                "mean": None if self.mean_ is None else self.mean_.tolist(),
                "threshold": self.threshold_}


# ----------------------------------------------------------------------
# 自模块快速自检：python vision/classifiers.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    rng = np.random.default_rng(7)
    # 玩具集（符合 SIDES 单侧语义）：
    #   OK 类围绕原点；NG-A 类沿 维0/维1 正向偏移（双侧/偏高方向检出）；
    #   NG-B 类沿 维2/维3 负向偏移（偏低方向检出）
    x_ok = rng.normal(0, 1.0, size=(400, 4))
    x_ng_a = rng.normal(0, 1.0, size=(200, 4)) + np.array([3.0, 3.0, 0.0, 0.0])
    x_ng_b = rng.normal(0, 1.0, size=(200, 4)) + np.array([0.0, 0.0, -3.0, -3.0])
    X = np.vstack([x_ok, x_ng_a, x_ng_b])
    y = np.array([0] * 400 + [1] * 400)

    # 1) 异常变换器：用 OK 批做基线，NG 批的异常得分应显著更大
    tr = AnomalyTransformer(quantile=0.99).fit(x_ok)
    s_ok = tr.transform(x_ok).mean(axis=0)
    s_ng = tr.transform(np.vstack([x_ng_a, x_ng_b])).mean(axis=0)
    assert (s_ng > s_ok).all(), "NG 批各维异常特征均值应全面高于健康批"

    # 2) 逻辑回归（在异常特征空间）
    lr = LogisticRegressionLite(lr=1.0, epochs=2000).fit(tr.transform(X), y)
    acc_lr = float((lr.predict(tr.transform(X)) == y).mean())
    assert acc_lr > 0.95, f"逻辑回归玩具集准确率过低: {acc_lr}"

    # 3) 单类马氏：只用 OK 批拟合+校准，应能抓住大部分 NG
    mh = MahalanobisClassifier(alpha=0.99).fit(x_ok).calibrate(x_ok)
    acc_mh = float((mh.predict(np.vstack([x_ng_a, x_ng_b])) == 1).mean())
    fp_mh = float((mh.predict(x_ok) == 1).mean())
    assert acc_mh > 0.8, f"马氏单类对 NG 检出率过低: {acc_mh}"
    assert fp_mh <= 0.02, f"马氏单类误报率过高: {fp_mh}"

    # 4) 参数导出/回载一致性
    lr2 = LogisticRegressionLite.from_dict(lr.to_dict())
    assert (lr2.predict(tr.transform(X)) == lr.predict(tr.transform(X))).all()
    print(f"[classifiers 自检通过] LR acc={acc_lr:.3f}, 马氏检出={acc_mh:.3f}, "
          f"马氏误报={fp_mh:.3f}, 最终损失={lr.loss_history[-1]} (仿真验证值)")

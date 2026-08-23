# -*- coding: utf-8 -*-
"""
vision/measure_model.py —— 多特征尺寸测量仿真模型（班次3新增）
================================================================
职责：
    把班次1的"单尺寸高斯抽样"升级为贴近真实视觉测量的【多特征向量】观测过程：
        f0 尺寸偏差mm  ：关键尺寸相对名义值的偏差（带测量噪声）
        f1 圆度um      ：形位公差特征（装配错位类缺陷会显著恶化）
        f2 表面得分    ：表面反射成像质量评分0~100（划痕类缺陷显著降低）
        f3 边缘锐度    :边缘提取清晰度评分0~100（崩缺类缺陷显著降低）

    【真值口径】（班次3定义，与班次1理论NG率≈4.6%无缝衔接）：
        真NG = 尺寸超差（过程散布 |d0|>公差，理论≈4.6%，继承班次1物理模型）
             ∪ 公差带内隐性缺陷（表面划痕/边缘崩缺/装配错位，先验≈2.5%）
        → 综合真值 NG 率 ≈ 7%（仿真验证值）。
    规则法只能命中"尺寸超差"，隐性缺陷只有多特征分类器能识别——
    这就是 A/B 对照的价值点。

时间纪律：
    本模块不使用任何墙钟/计时，只做纯随机采样（随机流由外部注入保证可复现）。

假设记录：
    - 特征分布参数取"典型机器视觉检测站"量级并做了可分性设计（教学演示用）。
"""

import os
import sys
# 路径引导：直接运行本文件(python vision/measure_model.py)时把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from config import settings as S


class MeasureSimulator:
    """多特征测量仿真器：sample_item() 每次模拟一件产品的完整测量过程。"""

    def __init__(self, rng: np.random.Generator,
                 nominal: float = S.VISION_NOMINAL_DIM,
                 sigma: float = S.VISION_SIGMA,
                 tol: float = S.VISION_TOLERANCE):
        """
        :param rng: numpy 随机源（由调用方持有 → 种子可控、结果可复现）
        :param nominal: 关键尺寸名义值 mm
        :param sigma:   加工过程散布 σ mm（与班次1同款口径）
        :param tol:     公差带 ±mm
        """
        self.rng = rng
        self.nominal = float(nominal)
        self.sigma = float(sigma)
        self.tol = float(tol)
        # 健康件辅助特征的分布参数（圆度um / 表面得分 / 边缘锐度）
        self._healthy = {"round_um": (8.0, 2.0), "surface": (88.0, 6.0), "edge": (80.0, 7.0)}
        # 各隐性缺陷子类型的权重表（来自参数中心；归一化防御浮点和≠1）
        types = S.VISION_DEFECT_TYPES
        self._names = list(types.keys())
        self._probs = np.array([types[n] for n in self._names], dtype=float)
        self._probs = self._probs / self._probs.sum()

    # ------------------------------------------------------------------
    # 单件采样
    # ------------------------------------------------------------------
    def sample_item(self) -> dict:
        """
        采样一件产品的"隐含真值 + 观测特征向量"。
        返回 dict：
            features    np.ndarray shape=(4,) —— [尺寸偏差mm, 圆度um, 表面得分, 边缘锐度]
            y_true      int 1=真NG 0=真OK（仿真中可观测的真值，用于在线混淆矩阵）
            defect_type str  缺陷子类型名（健康件为"无"）
            dim_mm      float 观测到的关键尺寸（=名义+观测偏差），回填 product.qc_dim 用
        """
        rng = self.rng
        # 1) 过程散布：真实尺寸偏差（健康工艺，与班次1同款高斯模型）
        d0_true = float(rng.normal(0.0, self.sigma))
        round_mu, round_sd = self._healthy["round_um"]
        surf_mu, surf_sd = self._healthy["surface"]
        edge_mu, edge_sd = self._healthy["edge"]

        # 2) 隐含真值判定：尺寸超差 ∪ 公差带内隐性缺陷
        dim_ng = abs(d0_true) > self.tol
        aux_type = None
        if not dim_ng and rng.random() < S.VISION_AUX_DEFECT_RATE:
            aux_type = str(rng.choice(self._names, p=self._probs))
        y_true = 1 if (dim_ng or aux_type is not None) else 0
        defect_type = ("尺寸超差" if dim_ng else (aux_type or "无"))

        # 3) 隐性缺陷只污染对应的辅助特征维度（尺寸仍合格——规则法盲区）
        if aux_type == "表面划痕":
            surf_mu, surf_sd = 52.0, 8.0                  # 表面得分骤降
        elif aux_type == "边缘崩缺":
            edge_mu, edge_sd = 45.0, 9.0                  # 边缘锐度骤降
        elif aux_type == "装配错位":
            round_mu, round_sd = 26.0, 6.0                # 圆度恶化

        # 4) 观测环节：真实测量必带观测噪声（测量系统误差，量级远小于公差）
        d0_obs = d0_true + float(rng.normal(0.0, S.VISION_MEAS_NOISE_STD))
        f_round = max(float(rng.normal(round_mu, round_sd)), 0.0)
        f_surf = min(max(float(rng.normal(surf_mu, surf_sd)), 0.0), 100.0)
        f_edge = min(max(float(rng.normal(edge_mu, edge_sd)), 0.0), 100.0)

        return {
            "features": np.array([d0_obs, f_round, f_surf, f_edge], dtype=float),
            "y_true": y_true,
            "defect_type": defect_type,
            "dim_mm": round(self.nominal + d0_obs, 4),
        }

    # ------------------------------------------------------------------
    # 班次1规则法（A/B 对照基准）：只用单一尺寸维度
    # ------------------------------------------------------------------
    def rule_judge_features(self, features: np.ndarray) -> str:
        """规则法判定：|尺寸偏差| > 公差 即 NG。与 UnitVision.judge() 原口径一致。"""
        return "NG" if abs(float(features[0])) > self.tol else "OK"


# ----------------------------------------------------------------------
# 自模块快速自检：python vision/measure_model.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sim = MeasureSimulator(np.random.default_rng(S.DEFAULT_SEED))
    n = 6000
    ng = 0
    rule_catch = 0
    type_stat = {}
    for _ in range(n):
        s = sim.sample_item()
        assert s["features"].shape == (4,)
        assert abs(s["dim_mm"] - (s["features"][0] + S.VISION_NOMINAL_DIM)) < 0.006, \
            "dim_mm 应等于 名义值+观测偏差(±舍入)"
        assert s["y_true"] in (0, 1)
        if s["y_true"] == 1:
            ng += 1
            type_stat[s["defect_type"]] = type_stat.get(s["defect_type"], 0) + 1
            if sim.rule_judge_features(s["features"]) == "NG":
                rule_catch += 1
        else:
            # 健康件真值口径自洽：尺寸必须公差带内且无隐性缺陷
            assert abs(s["dim_mm"] - S.VISION_NOMINAL_DIM) <= S.VISION_TOLERANCE * 2.4, \
                f"健康件偏差异常(4σ外): {s['dim_mm']}"
            assert s["defect_type"] == "无"
    rate = ng / n
    catch = rule_catch / max(ng, 1)
    # 综合 NG 率 ≈ 尺寸超差4.6% + 隐性缺陷2.5%×0.954 ≈ 7%（±抽样波动带宽）
    assert 0.04 < rate < 0.11, f"综合 NG 率偏离理论: {rate:.3f} (期望≈7%)"
    # 规则法命中率应显著低于 1（漏掉全部隐性缺陷），且高于纯超差占比的一半
    assert 0.45 < catch < 0.85, f"规则法对真NG命中率异常: {catch:.2f}"
    print(f"[measure_model 自检通过] 样本{n}件: 综合NG率={rate*100:.2f}% (期望≈7%), "
          f"规则法命中={catch*100:.1f}%, 缺陷构成={type_stat} (仿真验证值)")

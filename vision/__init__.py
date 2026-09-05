# -*- coding: utf-8 -*-
"""
vision/ —— 阶段3新增：视觉算法升级包
======================================
组成：
    measure_model.py   多特征尺寸测量仿真模型（真实测量过程建模）
    classifiers.py     numpy 手写轻量分类器（逻辑回归 + 马氏距离）
    defect_generator.py 缺陷样本生成器（受控随机）+ 混淆矩阵/指标评估
    vision_upgrade.py  UnitVision.judge() 覆写/注入入口（保留规则法 A/B 对照）

假设记录：
    - 本包只用 标准库+numpy，不引入 pytorch/tensorflow（硬性要求④）。
"""


def _bind():
    """把项目根加入 sys.path，保证 python -m 或直接运行子模块时导入可用。"""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bind()

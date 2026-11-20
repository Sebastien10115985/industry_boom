"""市场牛熊状态：后台自动计算入口。

取值范围 -100（极熊）～ +100（极牛），0 为震荡中枢。
页面只调用 ``compute_regime`` 获取结果，不提供人工设置入口。

后续拿到算法方案文档时，只需替换 ``compute_regime`` 的实现：
自动算出的值可通过 ``macro_risk.save_regime(value, is_admin=True)`` 持久化，
供同一服务器上的所有会话共享。
"""

from __future__ import annotations

from macro_risk import load_regime

DEFAULT_REGIME = -80.0


def compute_regime() -> float:
    """返回当前市场状态值。

    占位实现：读取已有缓存值；算法方案接入后在此改为自动计算。
    """
    try:
        value = load_regime()
    except Exception:
        return DEFAULT_REGIME
    return float(value) if value is not None else DEFAULT_REGIME

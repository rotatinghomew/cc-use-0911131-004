"""weights 职责（二）：权重基期与固定权重篮子。

权重基期选择与取舍：

* 取系统时点可见的**最早月期**为权重基期（本样例为 2026-01），权重为基期在样
  商户的标准化数量份额 ``w_i = q_i(base) / Σq(base)``。固定基期权重让相邻月的
  纯价格比较不受数量结构变化影响，是“按旧口径可比变化”的计量基础；
* 基期之后新入样的（商户, 商品）没有基期支出份额，采用其入样期标准数量相对
  基期总量的规模 ``q_i / Σq(base)`` 作为引入权重，并统一在 sample_entry
  贡献中解释，不回写到基期、不改变历史发布；
* 退样商户在退样期之后自然离开篮子，其影响在 sample_exit 贡献中解释；
* 权重不在每期重新归一后直接比较——每期篮子内部归一用于展示水平，桥接分解
  使用同一基期刻度的权重，保证贡献可加总、无“其他”残差。
"""
from __future__ import annotations

from market_index_bureau.restatement.domain import (
    MembershipInterval,
    Observation,
    freeze_membership,
    freeze_observations,
    panel_on,
    period_day,
)
from market_index_bureau.restatement.normalize import (
    NormalizedQuote,
    index_by_identity_and_revision,
)

STAGE = "restatement_weights"


def base_period_of(observations: list[Observation], as_of: str) -> str:
    frozen = freeze_observations(observations, as_of)
    if not frozen:
        raise ValueError(f"as_of={as_of} 时点没有任何已知报价，无法确定权重基期")
    return min(obs.period for obs in frozen.values())


def build_base_weights(
    observations: list[Observation],
    normalized: list[NormalizedQuote],
    intervals: list[MembershipInterval],
    as_of: str,
) -> tuple[str, dict[tuple[str, str], float], float]:
    """返回 (基期, 基期固定权重表, 基期标准数量总量)。"""
    base_period = base_period_of(observations, as_of)
    frozen = freeze_observations(observations, as_of)
    panel = panel_on(freeze_membership(intervals, as_of), period_day(base_period))
    norm_by_identity = index_by_identity_and_revision(normalized)
    base_quantities: dict[tuple[str, str], float] = {}
    for identity, obs in frozen.items():
        merchant_id, product_id, period = identity
        if period != base_period or merchant_id not in panel:
            continue
        norm = norm_by_identity[(*identity, obs.revision)]
        if not norm.valid:
            continue
        base_quantities[(merchant_id, product_id)] = (
            base_quantities.get((merchant_id, product_id), 0.0) + norm.standard_quantity
        )
    total = sum(base_quantities.values())
    if total <= 0:
        raise ValueError(f"权重基期 {base_period} 没有有效在样数量，无法建立权重")
    weights = {key: quantity / total for key, quantity in base_quantities.items()}
    return base_period, weights, total

"""normalization 职责：单位归一与逐笔报价标准化。

输入是 feeds 阶段解析出的 :class:`Observation`，输出标准单位下的价格与数量：

* 标准价格 ``price_std = price / factor``
* 标准数量 ``quantity_std = quantity * factor``

两者乘积（支出额）在单位换算下保持不变，这是后续基期权重可加总的前提。
缺报价不会在这里被补成 0：无效价格的报价只会被标记并从篮子中剔除。
"""
from __future__ import annotations

from dataclasses import dataclass

from market_index_bureau.restatement.domain import Observation


@dataclass(frozen=True, slots=True)
class NormalizedQuote:
    observation: Observation
    standard_price: float
    standard_quantity: float
    valid: bool
    flags: tuple[str, ...]


def normalize_observation(
    observation: Observation, factors: dict[str, float]
) -> NormalizedQuote:
    factor = factors[observation.unit]
    flags: list[str] = []
    valid = True
    if observation.price is None or observation.price <= 0:
        # 缺报价/非正价格不是 0 价格：标记后排除，绝不参与均值或计权。
        valid = False
        flags.append("invalid_price")
    standard_price = observation.price / factor if valid else 0.0
    standard_quantity = observation.quantity * factor
    return NormalizedQuote(
        observation=observation,
        standard_price=standard_price,
        standard_quantity=standard_quantity,
        valid=valid,
        flags=tuple(flags),
    )


def normalize_all(
    observations: list[Observation], factors: dict[str, float]
) -> list[NormalizedQuote]:
    return [normalize_observation(item, factors) for item in observations]


def index_by_identity_and_revision(
    quotes: list[NormalizedQuote],
) -> dict[tuple[str, str, str, int], NormalizedQuote]:
    """按 (商户, 商品, 期次, revision) 建立标准化报价索引。

    同一 observation_id 的不同 revision 是独立版本行，不能互相覆盖；
    面板切片在冻结出最高 revision 后再按此键取回对应版本的标准化值。
    """
    return {
        (
            item.observation.merchant_id,
            item.observation.product_id,
            item.observation.period,
            item.observation.revision,
        ): item
        for item in quotes
    }

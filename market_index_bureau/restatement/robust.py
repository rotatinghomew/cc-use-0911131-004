"""anomalies 职责：稳健异常报价识别。

沿用既有 robust_filter 的“稳健统计”定位，但这里处理的是真实逐笔价格。

取舍说明（见输出 method_notes）：

* 横截面规则：同期有效报价达到 MIN_CROSS_SECTION 户时，用中位数 + MAD
  （median absolute deviation）稳健 z 分数（阈值 3.5）判断离散异常；
* 小样本兜底：同期报价不足 4 户时横截面 MAD 会被极端值自身主导，改用该
  (商户, 商品) 自身历史价格的中位数做参考，偏离超过 SMALL_SAMPLE_RATIO
  倍即标记；没有历史的首期不判异常，避免无基准的主观告警；
* 异常报价**不删除、不当零、不静默插补**，只打 ``anomaly_price`` 标记并保留在
  篮子里，同时在 issues / evidence 中留痕。指数是否信任该报价由证据链回答，
  而不是由黑盒剔除回答；
* 真正无效的报价（非正价格）已在 normalization 阶段标记 invalid_price 并排除。
"""
from __future__ import annotations

from statistics import median
from typing import Sequence

from market_index_bureau.restatement.domain import Issue
from market_index_bureau.restatement.normalize import NormalizedQuote

STAGE = "restatement_robust_filter"
Z_THRESHOLD = 3.5
MAD_Z_CONSTANT = 1.4826
SMALL_SAMPLE_RATIO = 2.0
MIN_CROSS_SECTION = 4


def _cross_section_anomalous(price: float, prices: list[float]) -> bool:
    center = median(prices)
    mad = median([abs(value - center) for value in prices])
    if center <= 0:
        return False
    if mad > 0:
        return abs(price - center) / (MAD_Z_CONSTANT * mad) > Z_THRESHOLD
    # MAD=0（价格趋同）：用相对中位数倍数兜底。
    return abs(price - center) / center > SMALL_SAMPLE_RATIO


def _history_anomalous(
    price: float, history: dict[tuple[str, str], list[float]], key: tuple[str, str]
) -> bool:
    past = history.get(key, [])
    if not past:
        return False
    center = median(past)
    if center <= 0:
        return False
    return abs(price - center) / center > SMALL_SAMPLE_RATIO


def flag_anomalies(quotes: Sequence[NormalizedQuote]) -> tuple[list[str], list[Issue]]:
    """按期对有效报价做稳健异常识别，返回被标记的 observation_id 与问题清单。"""
    flagged: set[str] = set()
    issues: list[Issue] = []
    by_period: dict[str, list[NormalizedQuote]] = {}
    history: dict[tuple[str, str], list[float]] = {}
    for quote in quotes:
        if not quote.valid:
            continue
        by_period.setdefault(quote.observation.period, []).append(quote)

    for period in sorted(by_period):
        members = by_period[period]
        prices = [item.standard_price for item in members]
        use_cross_section = len(members) >= MIN_CROSS_SECTION
        for item in members:
            key = (item.observation.merchant_id, item.observation.product_id)
            if use_cross_section:
                anomalous = _cross_section_anomalous(item.standard_price, prices)
            else:
                anomalous = _history_anomalous(item.standard_price, history, key)
            if not anomalous:
                continue
            flagged.add(item.observation.observation_id)
            issues.append(
                Issue(
                    code="anomaly_price",
                    severity="warning",
                    message=(
                        f"期次 {period} 报价 {item.observation.observation_id} "
                        f"价格 {item.standard_price} 超出稳健阈值，保留计权并标记 anomaly_price"
                    ),
                    period=period,
                    observation_ids=(item.observation.observation_id,),
                )
            )
        for item in members:
            history.setdefault(
                (item.observation.merchant_id, item.observation.product_id), []
            ).append(item.standard_price)
    return sorted(flagged), issues

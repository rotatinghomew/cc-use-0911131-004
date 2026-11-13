"""vintages 职责：双时态版本编排、旧版回看与重述桥接。

对一次分析运行构造两个（或多个）系统时点切片：

* **原发布 vintage**：以 publications 中每条记录的 as_of 冻结当时已知版本，
  原样重算当时能看到的指数，并与发布 level 对账；原发布行只插入、不覆盖；
* **重述 vintage**：以输入 as_of 冻结全部已知版本（含迟到修订、新入样商户、
  生效后的类别映射），产出新口径水平与完整桥接；
* 两个 vintage 在同一期上的差异用 :func:`vintage_bridge` 分解，迟到价格归入
  late_revision，链条历史保存在独立版本行中，原发布与链式历史不会被覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from market_index_bureau.restatement.domain import (
    Contribution,
    Issue,
    MappingRule,
    MembershipInterval,
    Observation,
    PublicationRecord,
    Quote,
    RECONCILE_TOLERANCE,
    round6,
)
from market_index_bureau.restatement.engine import (
    VintageResult,
    basket,
    build_period_results,
    category_levels,
    vintage_bridge,
)
from market_index_bureau.restatement.normalize import NormalizedQuote
from market_index_bureau.restatement.panel import VintageSlice, build_slice
from market_index_bureau.restatement.robust import flag_anomalies
from market_index_bureau.restatement.weights import build_base_weights


@dataclass(slots=True)
class VintageBundle:
    kind: str                      # original / restatement
    as_of: str
    slice: VintageSlice
    result: VintageResult
    base_period: str
    anchor_period: str | None
    anchor_level: float | None
    issues: list[Issue] = field(default_factory=list)


def _mapping_change_periods(rules: list[MappingRule], periods: list[str]) -> set[str]:
    changes: set[str] = set()
    for rule in rules:
        for period in periods:
            if rule.effective_at <= period + "-15":
                changes.add(period)
    return changes


def build_vintage(
    kind: str,
    as_of: str,
    observations: list[Observation],
    normalized: list[NormalizedQuote],
    intervals: list[MembershipInterval],
    rules: list[MappingRule],
    anomaly_ids: set[str],
    anchor: tuple[str, float] | None,
) -> VintageBundle:
    base_period, base_weights, base_total = build_base_weights(
        observations, normalized, intervals, as_of
    )
    slice_obj = build_slice(
        as_of=as_of,
        observations=observations,
        normalized=normalized,
        intervals=intervals,
        rules=rules,
        base_weights=base_weights,
        base_total_quantity=base_total,
        anomaly_ids=anomaly_ids,
    )
    anchor_period, anchor_level = anchor if anchor else (None, None)
    periods = build_period_results(
        slice_obj.quotes_by_period,
        anchor_period or base_period,
        anchor_level if anchor_level is not None else 100.0,
        _mapping_change_periods(slice_obj.mapping_rules, slice_obj.periods),
    )
    result = VintageResult(
        as_of=as_of,
        periods=periods,
        issues=slice_obj.issues,
        category_levels=category_levels(slice_obj.quotes_by_period),
    )
    return VintageBundle(
        kind=kind,
        as_of=as_of,
        slice=slice_obj,
        result=result,
        base_period=base_period,
        anchor_period=anchor_period,
        anchor_level=anchor_level,
        issues=slice_obj.issues,
    )


def cross_vintage_contributions(
    original: VintageBundle, restated: VintageBundle
) -> dict[str, list[Contribution]]:
    """对两个 vintage 共有的每一期做迟到/重述分解。"""
    output: dict[str, list[Contribution]] = {}
    original_baskets = {
        period: basket(original.slice.quotes_by_period[period])
        for period in original.slice.periods
    }
    restated_baskets = {
        period: basket(restated.slice.quotes_by_period[period])
        for period in restated.slice.periods
        if period in original_baskets
    }
    for period in sorted(restated_baskets):
        contributions = vintage_bridge(
            period, original_baskets[period], restated_baskets[period]
        )
        if contributions:
            output[period] = contributions
    return output


def reconcile_vintage(
    period: str,
    original: VintageBundle,
    restated: VintageBundle,
    contributions: list[Contribution],
) -> dict[str, float | bool]:
    original_level = next(
        item.headline_level for item in original.result.periods if item.period == period
    )
    restated_level = next(
        item.headline_level for item in restated.result.periods if item.period == period
    )
    explained = sum(item.amount for item in contributions)
    total_change = restated_level - original_level
    residual = total_change - explained
    return {
        "period": period,
        "original_level": round6(original_level),
        "restated_level": round6(restated_level),
        "total_change": round6(total_change),
        "explained_change": round6(explained),
        "residual": round6(residual),
        "balanced": abs(residual) <= RECONCILE_TOLERANCE,
    }


def run_vintages(
    as_of: str,
    observations: list[Observation],
    normalized: list[NormalizedQuote],
    intervals: list[MembershipInterval],
    rules: list[MappingRule],
    publications: list[PublicationRecord],
) -> dict[str, Any]:
    """完整双时态编排，返回可直接序列化的中间结构。"""
    anomaly_ids, anomaly_issues = flag_anomalies(normalized)

    # 原发布 vintage：每条发布记录独立冻结，锚定其自身发布水平。
    originals: list[VintageBundle] = []
    for publication in publications:
        originals.append(
            build_vintage(
                kind="original",
                as_of=publication.as_of,
                observations=observations,
                normalized=normalized,
                intervals=intervals,
                rules=rules,
                anomaly_ids=set(anomaly_ids),
                anchor=(publication.period, publication.level),
            )
        )

    # 重述 vintage 锚定到第一条原发布，保证链式历史接续而不是重置。
    first_publication = publications[0] if publications else None
    restated_anchor = (
        (first_publication.period, first_publication.level)
        if first_publication
        else None
    )
    restated = build_vintage(
        kind="restatement",
        as_of=as_of,
        observations=observations,
        normalized=normalized,
        intervals=intervals,
        rules=rules,
        anomaly_ids=set(anomaly_ids),
        anchor=restated_anchor,
    )
    # 异常识别是对当前已知全部报价的全局诊断，挂在重述 vintage 的问题清单上。
    restated.issues.extend(anomaly_issues)

    vintage_deltas = {
        original.slice.as_of: cross_vintage_contributions(original, restated)
        for original in originals
    }
    vintage_reconciliation = []
    for original in originals:
        for period, contributions in vintage_deltas[original.slice.as_of].items():
            vintage_reconciliation.append(
                reconcile_vintage(period, original, restated, contributions)
            )

    return {
        "anomaly_ids": anomaly_ids,
        "anomaly_issues": anomaly_issues,
        "originals": originals,
        "restated": restated,
        "vintage_deltas": vintage_deltas,
        "vintage_reconciliation": vintage_reconciliation,
    }

"""weights 职责（一）：商户面板双时态切片与类别映射。

对应既有 merchant_panel 的职责边界。对每个发布/重算时点 ``as_of``：

1. 用 membership 的生效区间切出该期在样商户（同时要求 known_at <= as_of）；
2. 用 mappings 的生效区间把报价的源类别映射到期次口径类别；同一源类别在同一
   生效时点出现多个不同目标即“映射冲突”，受影响报价标记 mapping_conflict，
   该期受影响类别标记不可比，不做任何猜测性平滑；
3. 入样前不在样的商户即使提前报送了报价，也只能作为 out_of_panel 证据，
   不能计入任何历史期次；
4. 在样商户某期缺报价（或只有无效报价）记 coverage_gap，不补零、不插补增长率。
"""
from __future__ import annotations

from dataclasses import dataclass

from market_index_bureau.restatement.domain import (
    Issue,
    MappingRule,
    MembershipInterval,
    Observation,
    Quote,
    freeze_mappings,
    freeze_membership,
    freeze_observations,
    mapping_on,
    panel_on,
    period_day,
    with_flag,
)
from market_index_bureau.restatement.normalize import NormalizedQuote, index_by_identity_and_revision

STAGE = "restatement_merchant_panel"


@dataclass(slots=True)
class VintageSlice:
    as_of: str
    periods: list[str]
    quotes_by_period: dict[str, list[Quote]]
    issues: list[Issue]
    out_of_panel_ids: list[str]
    excluded_ids: list[str]
    mapping_rules: list[MappingRule]
    membership_intervals: list[MembershipInterval]
    observation_versions: dict[str, int]  # observation_id -> 实际使用 revision


def _category_for(
    source_category: str,
    day: str,
    rules: list[MappingRule],
    issues: list[Issue],
    period: str,
    observation_id: str,
) -> tuple[str, str | None, bool]:
    chosen, candidates = mapping_on(rules, source_category, day)
    if chosen is None and candidates:
        issues.append(
            Issue(
                code="mapping_conflict",
                severity="critical",
                message=(
                    f"期次 {period} 源类别 {source_category} 存在多个互斥映射目标，"
                    "该类别桥接标记为不可比"
                ),
                period=period,
                observation_ids=(observation_id,),
                mapping_ids=tuple(rule.mapping_id for rule in candidates),
            )
        )
        return source_category, None, True
    if chosen is None:
        return source_category, None, False
    return chosen.target, chosen.mapping_id, False


def build_slice(
    as_of: str,
    observations: list[Observation],
    normalized: list[NormalizedQuote],
    intervals: list[MembershipInterval],
    rules: list[MappingRule],
    base_weights: dict[tuple[str, str], float],
    base_total_quantity: float,
    anomaly_ids: set[str],
) -> VintageSlice:
    """构造某个系统时点 as_of 下的期次篮子（权重已在 weights 模块确定）。"""
    frozen_obs = freeze_observations(observations, as_of)
    active_intervals = freeze_membership(intervals, as_of)
    active_rules = freeze_mappings(rules, as_of)
    norm_by_identity = index_by_identity_and_revision(normalized)

    periods = sorted({obs.period for obs in frozen_obs.values()})
    quotes_by_period: dict[str, list[Quote]] = {period: [] for period in periods}
    issues: list[Issue] = []
    out_of_panel: list[str] = []
    excluded: list[str] = []
    versions: dict[str, int] = {}

    for period in periods:
        day = period_day(period)
        panel = panel_on(active_intervals, day)
        for identity, obs in sorted(frozen_obs.items()):
            if identity[2] != period:
                continue
            if obs.merchant_id not in panel:
                out_of_panel.append(obs.observation_id)
                issues.append(
                    Issue(
                        code="out_of_panel_quote",
                        severity="info",
                        message=(
                            f"报价 {obs.observation_id} 的商户 {obs.merchant_id} 在 "
                            f"{period} 尚未入样，报价仅留存证据不计权"
                        ),
                        period=period,
                        observation_ids=(obs.observation_id,),
                        merchant_ids=(obs.merchant_id,),
                    )
                )
                continue
            norm = norm_by_identity[(*identity, obs.revision)]
            if not norm.valid:
                excluded.append(obs.observation_id)
                issues.append(
                    Issue(
                        code="invalid_price_excluded",
                        severity="critical",
                        message=(
                            f"报价 {obs.observation_id} 价格无效，已从篮子排除；"
                            "缺报价不按零价格处理"
                        ),
                        period=period,
                        observation_ids=(obs.observation_id,),
                        merchant_ids=(obs.merchant_id,),
                    )
                )
                continue

            target_category, mapping_id, conflict = _category_for(
                obs.category, day, active_rules, issues, period, obs.observation_id
            )
            entrant = identity[:2] not in base_weights
            if entrant:
                weight = norm.standard_quantity / base_total_quantity
            else:
                weight = base_weights[identity[:2]]
            flags = list(norm.flags)
            if conflict:
                flags.append("mapping_conflict")
            if obs.observation_id in anomaly_ids:
                flags.append("anomaly_price")
            if entrant:
                flags.append("entrant")
            quote = Quote(
                observation_id=obs.observation_id,
                revision=obs.revision,
                merchant_id=obs.merchant_id,
                product_id=obs.product_id,
                source_category=obs.category,
                category=target_category,
                period=period,
                price=norm.standard_price,
                quantity=norm.standard_quantity,
                weight=weight,
                entrant=entrant,
                flags=tuple(flags),
                mapping_id=mapping_id,
            )
            if conflict:
                quote = with_flag(quote, "mapping_conflict")
            quotes_by_period[period].append(quote)
            # 只把真正进入篮子的报价版本记入来源版本，出样/被排除的报价
            # 通过 out_of_panel_ids / excluded_ids 与 issues 单独留痕。
            versions[obs.observation_id] = obs.revision

    # 在样商户覆盖缺口：某期面板商户没有任何有效篮子报价。
    for period in periods:
        covered = {quote.merchant_id for quote in quotes_by_period[period]}
        day = period_day(period)
        for merchant_id in sorted(panel_on(active_intervals, day) - covered):
            issues.append(
                Issue(
                    code="coverage_gap",
                    severity="warning",
                    message=(
                        f"在样商户 {merchant_id} 在 {period} 缺少有效报价，"
                        "不插补、不按零价格，链接按匹配覆盖判断可比性"
                    ),
                    period=period,
                    merchant_ids=(merchant_id,),
                )
            )

    return VintageSlice(
        as_of=as_of,
        periods=periods,
        quotes_by_period=quotes_by_period,
        issues=issues,
        out_of_panel_ids=sorted(set(out_of_panel)),
        excluded_ids=sorted(set(excluded)),
        mapping_rules=active_rules,
        membership_intervals=active_intervals,
        observation_versions=versions,
    )

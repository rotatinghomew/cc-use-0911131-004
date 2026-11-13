"""feeds 职责：分析输入合同校验与领域对象解析。

对应既有 market_index_bureau.feeds.contract 的边界角色，但这里的合同针对
fixtures/analysis 的双时态分析输入，而不是旧的合成业务记录。

校验原则：

* 缺报价与零价格语义不同——缺字段直接拒绝合同；零/负价格不按 0 计入指数，
  而是标记 ``invalid_price`` 并在后续阶段排除（绝不拿 0 价格拉低指数）；
* 同一 observation_id 的多条记录必须构成单调修订链（revision 不得倒退）；
* 所有日期必须是 ISO 字符串，effective_at / known_at 各自承担业务/系统时间语义。
"""
from __future__ import annotations

from typing import Any, Mapping

from market_index_bureau.restatement.domain import (
    MembershipInterval,
    MappingRule,
    Observation,
    PublicationRecord,
    RestatementError,
    UnitConversion,
    require_list,
    require_number,
    require_object,
    require_text,
)

STAGE = "restatement_feed_contract"


def _parse_observation(raw: Any, seen_revisions: dict[str, set[int]]) -> Observation:
    record = require_object(raw, "observations[]")
    observation_id = require_text(record, "observation_id")
    revision = int(require_number(record, "revision"))
    if revision < 1:
        raise RestatementError(f"observation {observation_id} 的 revision 必须 >= 1")
    prior = seen_revisions.setdefault(observation_id, set())
    if revision in prior:
        raise RestatementError(
            f"observation {observation_id} 出现重复 revision={revision}，"
            "同一报价的每个修订只能出现一次"
        )
    prior.add(revision)
    price = require_number(record, "price")
    quantity = require_number(record, "quantity")
    if quantity <= 0:
        raise RestatementError(
            f"observation {observation_id} 的 quantity 必须为正数，缺数量不能当作 0"
        )
    return Observation(
        observation_id=observation_id,
        revision=revision,
        merchant_id=require_text(record, "merchant_id"),
        product_id=require_text(record, "product_id"),
        category=require_text(record, "category"),
        period=require_text(record, "period"),
        price=price,
        quantity=quantity,
        unit=require_text(record, "unit"),
        effective_at=require_text(record, "effective_at"),
        known_at=require_text(record, "known_at"),
    )


def _parse_membership(raw: Any) -> MembershipInterval:
    record = require_object(raw, "membership[]")
    effective_to = record.get("effective_to")
    if effective_to is not None and not isinstance(effective_to, str):
        raise RestatementError("membership.effective_to 必须是 ISO 日期字符串或 null")
    return MembershipInterval(
        merchant_id=require_text(record, "merchant_id"),
        effective_from=require_text(record, "effective_from"),
        effective_to=effective_to,
        known_at=require_text(record, "known_at"),
    )


def _parse_mapping(raw: Any) -> MappingRule:
    record = require_object(raw, "mappings[]")
    return MappingRule(
        mapping_id=require_text(record, "mapping_id"),
        revision=int(require_number(record, "revision")),
        source=require_text(record, "source"),
        target=require_text(record, "target"),
        effective_at=require_text(record, "effective_at"),
        known_at=require_text(record, "known_at"),
    )


def _parse_conversion(raw: Any) -> UnitConversion:
    record = require_object(raw, "unit_conversions[]")
    factor = require_number(record, "factor")
    if factor <= 0:
        raise RestatementError(f"unit {record.get('unit')} 的转换因子必须为正数")
    return UnitConversion(unit=require_text(record, "unit"), factor=factor)


def _parse_publication(raw: Any) -> PublicationRecord:
    record = require_object(raw, "publications[]")
    return PublicationRecord(
        publication_id=require_text(record, "publication_id"),
        as_of=require_text(record, "as_of"),
        period=require_text(record, "period"),
        level=require_number(record, "level"),
    )


def parse_analysis_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把原始 JSON 合同解析成强类型领域对象，并完成结构性校验。"""
    require_object(payload, "input")
    as_of = require_text(payload, "as_of")
    seen_revisions: dict[str, set[int]] = {}
    observations = [
        _parse_observation(item, seen_revisions)
        for item in require_list(payload.get("observations"), "observations")
    ]
    if not observations:
        raise RestatementError("observations 不能为空，无法建立指数")

    conversions = [
        _parse_conversion(item)
        for item in require_list(payload.get("unit_conversions"), "unit_conversions")
    ]
    conversion_by_unit = {item.unit: item.factor for item in conversions}
    missing_units = sorted({obs.unit for obs in observations} - set(conversion_by_unit))
    if missing_units:
        raise RestatementError(f"缺少单位转换定义: {', '.join(missing_units)}")

    membership = [
        _parse_membership(item)
        for item in require_list(payload.get("membership"), "membership")
    ]
    mappings = [
        _parse_mapping(item) for item in require_list(payload.get("mappings"), "mappings")
    ]
    publications = [
        _parse_publication(item)
        for item in require_list(payload.get("publications"), "publications")
    ]

    # 同一 observation_id 的修订链不得倒退；不同物理键（商户/商品/期次）也不允许混用。
    physical: dict[str, tuple[str, str, str]] = {}
    for obs in observations:
        identity = (obs.merchant_id, obs.product_id, obs.period)
        if obs.observation_id in physical and physical[obs.observation_id] != identity:
            raise RestatementError(
                f"observation_id {obs.observation_id} 对应了不同的商户/商品/期次"
            )
        physical[obs.observation_id] = identity

    return {
        "as_of": as_of,
        "observations": observations,
        "unit_conversions": conversion_by_unit,
        "membership": membership,
        "mappings": mappings,
        "publications": publications,
    }

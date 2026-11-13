"""重述分析应用服务：组装 feeds -> normalization -> robust -> weights/panel ->
indices -> vintages 全链路，并产出含 publication_id、vintages、contributions、
bridge_status、reconciliation 的对外 JSON。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from market_index_bureau.restatement.domain import (
    RECONCILE_TOLERANCE,
    Contribution,
    Issue,
    PublicationRecord,
    Quote,
    round6,
)
from market_index_bureau.restatement.engine import reconcile_period
from market_index_bureau.restatement.feed import STAGE as FEED_STAGE, parse_analysis_input
from market_index_bureau.restatement.normalize import normalize_all
from market_index_bureau.restatement.store import RestatementStore
from market_index_bureau.restatement.vintages import VintageBundle, run_vintages
from market_index_bureau.vintages.index_store import fingerprint

METHOD_NOTES = {
    "weight_base_period": "取系统时点可见的最早月期为权重基期，基期在样商户标准数量份额固定，"
    "新入样商户以入样期数量相对基期总量的规模引入，不回写历史",
    "headline_level": "当期篮子内固定基期刻度权重的加权平均价格（篮子内归一），反映新口径现行水平",
    "matched_chain": "仅用相邻两期共同在样且无映射冲突的匹配键计算加权价格相对数，链式接续并锚定"
    "原发布水平；匹配权重覆盖低于50%或匹配键少于2个时标记 not_comparable，沿用上一可比水平，"
    "不生成平滑增长率",
    "bridge": "Bennet 恒等分解：matched_price + weight_mix + sample_entry + sample_exit "
    "（类别迁移逐键留痕，纯改名贡献为0），贡献以未舍入值求和，残差超过1e-9即判定不平衡，"
    "不存在可吞掉差额的“其他”项",
    "anomalies": "中位数+MAD 稳健 z 分数（阈值3.5）识别异常报价；MAD 退化为 0 的小样本"
    "改用相对中位数偏离倍数（2.0）兜底；异常只标记不删除、不当零、不插补",
    "missing_quotes": "缺报价/非正价格标记 invalid_price 并排除，绝不按零价格计权；在样商户缺报"
    "记 coverage_gap，按匹配覆盖判断可比性",
    "bitemporal": "每个 vintage 只使用 known_at <= as_of 的报价、membership 与 mapping 版本；"
    "迟到更正产生新的重述版，原发布与链式历史只插入不覆盖",
    "revision_handling": "同一 (商户,商品,期次) 取最高 revision，旧 revision 不再计权；"
    "observation_id 修订链不得倒退或重复",
}


def _issue_dict(issue: Issue, vintage_id: str) -> dict[str, Any]:
    return {
        "vintage_id": vintage_id,
        "code": issue.code,
        "severity": issue.severity,
        "message": issue.message,
        "period": issue.period,
        "observation_ids": list(issue.observation_ids),
        "mapping_ids": list(issue.mapping_ids),
        "merchant_ids": list(issue.merchant_ids),
    }


def _source_versions(bundle: VintageBundle) -> dict[str, Any]:
    slice_obj = bundle.slice
    return {
        "as_of": bundle.as_of,
        "observation_versions": dict(sorted(slice_obj.observation_versions.items())),
        "mapping_ids": [rule.mapping_id for rule in slice_obj.mapping_rules],
        "mapping_revisions": {
            rule.mapping_id: rule.revision for rule in slice_obj.mapping_rules
        },
        "membership_known_at": sorted({item.known_at for item in slice_obj.membership_intervals}),
        "excluded_observation_ids": slice_obj.excluded_ids,
        "out_of_panel_observation_ids": slice_obj.out_of_panel_ids,
    }


def _vintage_block(
    bundle: VintageBundle, publications: list[PublicationRecord]
) -> dict[str, Any]:
    periods_out = []
    for item in bundle.result.periods:
        periods_out.append(
            {
                "period": item.period,
                "basis": item.basis,
                "comparable": item.comparable,
                "headline_level": round6(item.headline_level) if item.headline_level is not None else None,
                "matched_index": round6(item.matched_index) if item.matched_index is not None else None,
                "match_weight_coverage": item.match_weight_coverage,
                "match_key_count": item.match_key_count,
                "basket_size": item.basket_size,
                "entrant_count": item.entrant_count,
                "flags": list(item.flags),
                "category_levels": bundle.result.category_levels.get(item.period, {}),
            }
        )
    return {
        "vintage_id": f"{bundle.kind}:{bundle.as_of}",
        "kind": bundle.kind,
        "as_of": bundle.as_of,
        "base_period": bundle.base_period,
        "anchor_period": bundle.anchor_period,
        "anchor_level": bundle.anchor_level,
        "source_versions": _source_versions(bundle),
        "periods": periods_out,
        "issues": [_issue_dict(issue, f"{bundle.kind}:{bundle.as_of}") for issue in bundle.issues],
        "publication_ids": [
            pub.publication_id for pub in publications if pub.as_of == bundle.as_of
        ],
    }


def _period_contributions(bundle: VintageBundle) -> list[dict[str, Any]]:
    output = []
    for item in bundle.result.periods:
        for contribution in item.contributions:
            output.append(_contribution_dict(bundle.as_of, contribution))
    return output


def _contribution_dict(as_of: str, contribution: Contribution) -> dict[str, Any]:
    return {
        "vintage_as_of": as_of,
        "period": contribution.period,
        "factor": contribution.factor,
        "amount": round6(contribution.amount),
        "observation_ids": list(contribution.observation_ids),
        "evidence": contribution.evidence,
    }


def _bridge_status(bundle: VintageBundle) -> list[dict[str, Any]]:
    rows = []
    periods = bundle.result.periods
    for index, item in enumerate(periods):
        if index == 0:
            rows.append(
                {
                    "period": item.period,
                    "status": "anchor_period" if item.comparable else "base_period",
                    "comparable": item.comparable,
                    "basis": item.basis,
                    "reasons": list(item.flags),
                }
            )
            continue
        status = "comparable" if item.comparable else "not_comparable"
        reasons = list(item.flags)
        if item.contributions:
            reasons.append("bridge_balanced")
        rows.append(
            {
                "period": item.period,
                "status": status,
                "comparable": item.comparable,
                "basis": item.basis,
                "match_weight_coverage": item.match_weight_coverage,
                "match_key_count": item.match_key_count,
                "reasons": reasons,
            }
        )
    return rows


def _period_reconciliation(bundle: VintageBundle) -> list[dict[str, Any]]:
    rows = []
    periods = bundle.result.periods
    levels = {item.period: item.headline_level for item in periods}
    for index, item in enumerate(periods):
        if index == 0:
            rows.append(
                {
                    "period": item.period,
                    "total_change": 0.0 if item.headline_level is not None else None,
                    "explained_change": 0.0 if item.headline_level is not None else None,
                    "residual": 0.0 if item.headline_level is not None else None,
                    "balanced": item.headline_level is not None,
                    "tolerance": RECONCILE_TOLERANCE,
                }
            )
            continue
        check = reconcile_period(
            levels[periods[index - 1].period],
            item.headline_level,
            item.contributions,
        )
        check["period"] = item.period
        check["tolerance"] = RECONCILE_TOLERANCE
        rows.append(check)
    return rows


def _evidence_rows(bundle: VintageBundle) -> list[dict[str, Any]]:
    rows = []
    for quotes in bundle.slice.quotes_by_period.values():
        quote: Quote
        for quote in quotes:
            rows.append(
                {
                    "observation_id": quote.observation_id,
                    "revision": quote.revision,
                    "mapping_id": quote.mapping_id,
                    "quote_snapshot": {
                        "merchant_id": quote.merchant_id,
                        "product_id": quote.product_id,
                        "period": quote.period,
                        "source_category": quote.source_category,
                        "category": quote.category,
                        "standard_price": quote.price,
                        "standard_quantity": quote.quantity,
                        "weight": round6(quote.weight),
                        "entrant": quote.entrant,
                        "flags": list(quote.flags),
                    },
                }
            )
    return rows


def analyze(input_path: str | Path, database: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    input_fingerprint = fingerprint(payload)
    store = RestatementStore(database)
    recalled = store.recall_run(input_fingerprint)
    if recalled is not None:
        recalled["replayed"] = True
        recalled["storage"] = store.stats()
        return recalled

    parsed = parse_analysis_input(payload)
    normalized = normalize_all(parsed["observations"], parsed["unit_conversions"])
    assembled = run_vintages(
        as_of=parsed["as_of"],
        observations=parsed["observations"],
        normalized=normalized,
        intervals=parsed["membership"],
        rules=parsed["mappings"],
        publications=parsed["publications"],
    )

    run_id = uuid.uuid4().hex
    restated: VintageBundle = assembled["restated"]
    originals: list[VintageBundle] = assembled["originals"]

    vintage_blocks = [_vintage_block(bundle, parsed["publications"]) for bundle in originals]
    vintage_blocks.append(_vintage_block(restated, parsed["publications"]))

    contributions: list[dict[str, Any]] = []
    for bundle in originals:
        contributions.extend(_period_contributions(bundle))
    contributions.extend(_period_contributions(restated))
    # 重述 vs 原发布的跨 vintage 贡献（迟到价格等）。零金额的纯结构性留痕
    # （篮子完全相同的键）不进贡献清单，避免噪声；有差异的因子必须完整列示。
    for as_of, by_period in assembled["vintage_deltas"].items():
        for period, period_contributions in sorted(by_period.items()):
            for contribution in period_contributions:
                if abs(contribution.amount) <= 0.0:
                    continue
                record = _contribution_dict(f"restatement-vs:{as_of}", contribution)
                record["period"] = period
                contributions.append(record)

    reconciliation = {
        "period_bridges": {
            restated.as_of: _period_reconciliation(restated),
        },
        "vintage_restatements": assembled["vintage_reconciliation"],
        "publication_checks": _publication_checks(originals, parsed["publications"]),
        "all_balanced": True,
    }
    all_checks = (
        [row for rows in reconciliation["period_bridges"].values() for row in rows]
        + reconciliation["vintage_restatements"]
        + reconciliation["publication_checks"]
    )
    reconciliation["all_balanced"] = all(row["balanced"] for row in all_checks)

    bridge_status = {
        restated.as_of: _bridge_status(restated),
    }
    for bundle in originals:
        bridge_status[bundle.as_of] = _bridge_status(bundle)

    output = {
        "publication_id": "|".join(pub.publication_id for pub in parsed["publications"])
        or f"restatement:{parsed['as_of']}",
        "run_id": run_id,
        "as_of": parsed["as_of"],
        "weight_base_period": restated.base_period,
        "vintages": vintage_blocks,
        "contributions": contributions,
        "bridge_status": bridge_status,
        "reconciliation": reconciliation,
        "issues": [
            _issue_dict(issue, f"{bundle.kind}:{bundle.as_of}")
            for bundle in [*originals, restated]
            for issue in bundle.issues
        ],
        "method_notes": METHOD_NOTES,
        "storage": store.stats(),
        "replayed": False,
        "stages": [
            FEED_STAGE,
            "restatement_unit_normalization",
            "restatement_robust_filter",
            "restatement_merchant_panel",
            "restatement_weights",
            "restatement_chain_index",
            "restatement_vintage_revision",
        ],
    }

    _persist(store, run_id, input_fingerprint, parsed["as_of"], output, originals, restated, assembled)
    output["storage"] = store.stats()
    return output


def _publication_checks(
    originals: list[VintageBundle], publications: list[PublicationRecord]
) -> list[dict[str, Any]]:
    checks = []
    for publication in publications:
        bundle = next(item for item in originals if item.as_of == publication.as_of)
        actual = next(
            item.headline_level
            for item in bundle.result.periods
            if item.period == publication.period
        )
        # 发布水平是锚定的指数点，内部水平是加权平均价格，检查锚定期匹配指数是否
        # 严格等于发布水平，并记录两者供人工核对。
        matched = next(
            item.matched_index
            for item in bundle.result.periods
            if item.period == publication.period
        )
        residual = (matched if matched is not None else actual) - publication.level
        checks.append(
            {
                "publication_id": publication.publication_id,
                "period": publication.period,
                "published_level": publication.level,
                "recomputed_matched_index": matched,
                "recomputed_headline_level": actual,
                "residual": round6(residual),
                "balanced": abs(residual) <= RECONCILE_TOLERANCE,
                "tolerance": RECONCILE_TOLERANCE,
            }
        )
    return checks


def _persist(
    store: RestatementStore,
    run_id: str,
    input_fingerprint: str,
    as_of: str,
    output: Mapping[str, Any],
    originals: list[VintageBundle],
    restated: VintageBundle,
    assembled: Mapping[str, Any],
) -> None:
    store.save_run(run_id, input_fingerprint, as_of, output)
    for bundle in [*originals, restated]:
        vintage_key = f"{run_id}:{bundle.kind}:{bundle.as_of}"
        period_rows = [
            {
                "period": item.period,
                "basis": item.basis,
                "comparable": item.comparable,
                "headline_level": item.headline_level,
                "matched_index": item.matched_index,
                "match_weight_coverage": item.match_weight_coverage,
                "match_key_count": item.match_key_count,
                "basket_size": item.basket_size,
                "entrant_count": item.entrant_count,
                "flags": list(item.flags),
            }
            for item in bundle.result.periods
        ]
        flat_contributions: list[Contribution] = [
            contribution
            for item in bundle.result.periods
            for contribution in item.contributions
        ]
        store.save_vintage(
            run_id=run_id,
            vintage_key=vintage_key,
            kind=bundle.kind,
            as_of=bundle.as_of,
            base_period=bundle.base_period,
            anchor_period=bundle.anchor_period,
            anchor_level=bundle.anchor_level,
            source_versions=_source_versions(bundle),
            period_rows=period_rows,
            contributions=flat_contributions,
            evidence_rows=_evidence_rows(bundle),
        )

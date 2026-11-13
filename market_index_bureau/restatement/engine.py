"""indices 职责：可比指数、新口径水平与桥接分解。

指数方法（选型与取舍见 method_notes / README）：

1. **新口径水平（headline level）**：当期篮子内以固定基期刻度权重做加权平均
   价格，篮子内部对在样权重归一。它回答“按现行样本与分类，指数现在是什么
   水平”，因此天然包含样本更替与分类改版的影响。
2. **旧口径可比变化（matched chain）**：只使用相邻两期都在样、都有有效报价
   且无映射冲突的匹配键，计算加权价格相对数并逐月链式接续，锚定到已发布期
   次的发布水平。匹配覆盖不足时不生成平滑增长率，直接标记 not_comparable，
   链接沿用上一可比水平。
3. **桥接（Bennet 分解）**：对相邻两期并集上的每个键，用平均权重×价格差
   （matched_price）与平均价格×权重差（weight_mix），加上进入（sample_entry）、
   退出（sample_exit），在加权平均价格的指数点口径上恒等分解：

       L_t - L_{t-1} = Σ matched_price + Σ weight_mix
                        + Σ sample_entry + Σ sample_exit

   分解是恒等式，残差只可能是浮点误差，不允许出现“其他”吞掉的差额。
4. **类别迁移（category_migration）**：纯改名映射对总水平贡献为 0，但仍逐键
   留痕；只有当映射改变了类别内部组成（拆分/合并）时才产生非零迁移贡献，
   冲突映射直接判不可比。
5. **迟到价格（late_revision）**：在同一期两个系统时点（原发布 vs 重述）之间
   做同样的键级分解，专门归集 revision 更正带来的差异。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from market_index_bureau.restatement.domain import (
    Contribution,
    Issue,
    MIN_MATCH_KEYS,
    MIN_MATCH_WEIGHT_COVERAGE,
    Quote,
    RECONCILE_TOLERANCE,
    round6,
)

STAGE = "restatement_chain_index"

# 桥接贡献的固定因子顺序，输出与对账都按此顺序，杜绝“其他”兜底项。
PERIOD_FACTORS = (
    "matched_price",
    "sample_entry",
    "sample_exit",
    "weight_mix",
    "category_migration",
)
VINTAGE_FACTORS = (
    "late_revision",
    "sample_entry",
    "sample_exit",
    "weight_mix",
    "category_migration",
)


@dataclass(slots=True)
class PeriodResult:
    period: str
    headline_level: float | None
    matched_index: float | None
    comparable: bool
    basis: str                      # old / new，由类别映射生效情况决定
    match_weight_coverage: float
    match_key_count: int
    basket_size: int
    entrant_count: int
    contributions: list[Contribution] = field(default_factory=list)
    flags: tuple[str, ...] = ()


@dataclass(slots=True)
class VintageResult:
    as_of: str
    periods: list[PeriodResult]
    issues: list[Issue]
    category_levels: dict[str, dict[str, float]]


def basket(quotes: list[Quote]) -> dict[tuple[str, str], Quote]:
    return {item.key: item for item in quotes}


def normalized_weights(items: dict[tuple[str, str], Quote]) -> dict[tuple[str, str], float]:
    total = sum(quote.weight for quote in items.values())
    if total <= 0:
        return {}
    return {key: quote.weight / total for key, quote in items.items()}


def headline_level(items: dict[tuple[str, str], Quote]) -> float | None:
    if not items:
        # 整期无有效报价：没有水平可言，返回 None 由调用方标记不可比，
        # 绝不返回 0.0 让缺报价伪装成价格归零。
        return None
    weights = normalized_weights(items)
    return sum(weights[key] * quote.price for key, quote in items.items())


def matched_keys(
    before: dict[tuple[str, str], Quote], after: dict[tuple[str, str], Quote]
) -> list[tuple[str, str]]:
    keys = []
    for key in after:
        if key not in before:
            continue
        if "mapping_conflict" in after[key].flags or "mapping_conflict" in before[key].flags:
            continue
        keys.append(key)
    return sorted(keys)


def matched_relative(
    before: dict[tuple[str, str], Quote],
    after: dict[tuple[str, str], Quote],
    keys: list[tuple[str, str]],
) -> float:
    """匹配键上的加权价格相对数（权重采用当期固定基期刻度，匹配集内归一）。"""
    after_weights = normalized_weights({key: after[key] for key in keys})
    numerator = sum(after_weights[key] * after[key].price for key in keys)
    denominator = sum(after_weights[key] * before[key].price for key in keys)
    return numerator / denominator


def _aggregate(
    period: str, factor: str, pieces: list[tuple[float, Quote | None, Quote | None]]
) -> Contribution:
    amount = sum(piece[0] for piece in pieces)
    ids = tuple(
        sorted(
            {
                quote.observation_id
                for _, left, right in pieces
                for quote in (left, right)
                if quote is not None
            }
        )
    )
    evidence = {
        "item_count": len(pieces),
        "items": [
            {
                "amount": round6(amount_item),
                "merchant_id": _pick(left, right, "merchant_id"),
                "product_id": _pick(left, right, "product_id"),
                "observation_ids": [
                    quote.observation_id
                    for quote in (left, right)
                    if quote is not None
                ],
                "revisions": [
                    quote.revision for quote in (left, right) if quote is not None
                ],
                "price_before": left.price if left else None,
                "price_after": right.price if right else None,
            }
            for amount_item, left, right in pieces
        ],
    }
    return Contribution(
        period=period, factor=factor, amount=amount, observation_ids=ids, evidence=evidence
    )


def _pick(left: Quote | None, right: Quote | None, attr: str) -> str:
    quote = right or left
    return getattr(quote, attr)


def bennet_bridge(
    period: str,
    before: dict[tuple[str, str], Quote],
    after: dict[tuple[str, str], Quote],
) -> list[Contribution]:
    """相邻两期在指数点口径上的恒等 Bennet 分解。"""
    w_before = normalized_weights(before)
    w_after = normalized_weights(after)
    price_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    mix_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    entry_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    exit_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    migration_pieces: list[tuple[float, Quote | None, Quote | None]] = []

    for key in sorted(set(before) | set(after)):
        left, right = before.get(key), after.get(key)
        w0 = w_before.get(key, 0.0)
        w1 = w_after.get(key, 0.0)
        p0 = left.price if left else 0.0
        p1 = right.price if right else 0.0
        if left is not None and right is not None:
            w_bar = (w0 + w1) / 2.0
            p_bar = (p0 + p1) / 2.0
            price_pieces.append((w_bar * (p1 - p0), left, right))
            mix_pieces.append((p_bar * (w1 - w0), left, right))
            if left.category != right.category or left.source_category != right.source_category:
                # 纯改名在同一加权平均口径下贡献为 0；留痕保证可追溯到映射版本。
                migration_pieces.append((0.0, left, right))
        elif right is not None:
            entry_pieces.append((w1 * p1, None, right))
        else:
            assert left is not None
            exit_pieces.append((-w0 * p0, left, None))

    contributions = [
        _aggregate(period, "matched_price", price_pieces),
        _aggregate(period, "sample_entry", entry_pieces),
        _aggregate(period, "sample_exit", exit_pieces),
        _aggregate(period, "weight_mix", mix_pieces),
        _aggregate(period, "category_migration", migration_pieces),
    ]
    return [item for item in contributions if item.evidence["item_count"] > 0]


def vintage_bridge(
    period: str,
    original: dict[tuple[str, str], Quote],
    restated: dict[tuple[str, str], Quote],
) -> list[Contribution]:
    """同一期两个系统时点之间的键级分解，迟到修订归入 late_revision。"""
    w_original = normalized_weights(original)
    w_restated = normalized_weights(restated)
    late_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    mix_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    entry_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    exit_pieces: list[tuple[float, Quote | None, Quote | None]] = []
    migration_pieces: list[tuple[float, Quote | None, Quote | None]] = []

    for key in sorted(set(original) | set(restated)):
        left, right = original.get(key), restated.get(key)
        w0 = w_original.get(key, 0.0)
        w1 = w_restated.get(key, 0.0)
        p0 = left.price if left else 0.0
        p1 = right.price if right else 0.0
        if left is not None and right is not None:
            w_bar, p_bar = (w0 + w1) / 2.0, (p0 + p1) / 2.0
            if left.revision != right.revision or left.price != right.price:
                late_pieces.append((w_bar * (p1 - p0), left, right))
            mix_pieces.append((p_bar * (w1 - w0), left, right))
            if left.category != right.category:
                migration_pieces.append((0.0, left, right))
        elif right is not None:
            entry_pieces.append((w1 * p1, None, right))
        else:
            assert left is not None
            exit_pieces.append((-w0 * p0, left, None))

    grouped = [
        _aggregate(period, "late_revision", late_pieces),
        _aggregate(period, "sample_entry", entry_pieces),
        _aggregate(period, "sample_exit", exit_pieces),
        _aggregate(period, "weight_mix", mix_pieces),
        _aggregate(period, "category_migration", migration_pieces),
    ]
    return [item for item in grouped if item.evidence["item_count"] > 0]


def category_levels(
    quotes_by_period: dict[str, list[Quote]],
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for period, quotes in quotes_by_period.items():
        groups: dict[str, list[Quote]] = {}
        for quote in quotes:
            groups.setdefault(quote.category, []).append(quote)
        result[period] = {
            category: round6(level) if level is not None else None
            for category, items in sorted(groups.items())
            for level in [headline_level(basket(items))]
        }
    return result


def build_period_results(
    quotes_by_period: dict[str, list[Quote]],
    anchor_period: str,
    anchor_level: float,
    mapping_change_periods: set[str],
) -> list[PeriodResult]:
    """生成逐期水平、匹配链与桥接；anchor 期匹配指数重置为发布水平。"""
    periods = sorted(quotes_by_period)
    baskets = {period: basket(quotes_by_period[period]) for period in periods}
    results: list[PeriodResult] = []
    # 锚定期之前的月期：没有已发布锚点，匹配链保持空值并标记不可比，
    # 绝不倒推一个看似平滑的增长率来“补齐”链史。
    anchor_seen = False
    carried_index: float | None = None

    for index, period in enumerate(periods):
        items = baskets[period]
        if not items:
            # 整期篮子为空：标记不可比，水平与匹配链均给空值，不制造任何数字。
            results.append(
                PeriodResult(
                    period=period,
                    headline_level=None,
                    matched_index=None,
                    comparable=False,
                    basis="new" if period in mapping_change_periods else "old",
                    match_weight_coverage=0.0,
                    match_key_count=0,
                    basket_size=0,
                    entrant_count=0,
                    contributions=[],
                    flags=("empty_basket",),
                )
            )
            continue
        level = headline_level(items)
        basis = "new" if period in mapping_change_periods else "old"
        flags: list[str] = []
        if any("mapping_conflict" in quote.flags for quote in items.values()):
            flags.append("mapping_conflict")

        if period == anchor_period:
            matched_index = anchor_level
            comparable = True
            coverage = 0.0
            keys: list[tuple[str, str]] = []
            carried_index = anchor_level
            anchor_seen = True
        elif not anchor_seen:
            matched_index = None
            comparable = False
            coverage = 0.0
            keys = []
            flags.append("pre_anchor_no_published_chain")
        else:
            previous = baskets[periods[index - 1]]
            keys = matched_keys(previous, items)
            base_weight_sum = sum(previous[key].weight for key in keys)
            previous_total = sum(quote.weight for quote in previous.values())
            coverage = base_weight_sum / previous_total if previous_total else 0.0
            conflict = any(
                "mapping_conflict" in quote.flags
                for key in keys
                for quote in (previous[key], items[key])
            ) or any("mapping_conflict" in quote.flags for quote in items.values())
            if conflict:
                comparable = False
                matched_index = carried_index
                flags.append("not_comparable_mapping_conflict")
            elif len(keys) >= MIN_MATCH_KEYS and coverage >= MIN_MATCH_WEIGHT_COVERAGE:
                comparable = True
                assert carried_index is not None
                carried_index = carried_index * matched_relative(previous, items, keys)
                matched_index = carried_index
            else:
                # 不补平滑增长率：沿用上一可比水平并显式标记不可比。
                comparable = False
                matched_index = carried_index
                flags.append("not_comparable_gap")

        contributions = (
            bennet_bridge(period, baskets[periods[index - 1]], items) if index > 0 else []
        )
        results.append(
            PeriodResult(
                period=period,
                headline_level=level,
                matched_index=matched_index,
                comparable=comparable,
                basis=basis,
                match_weight_coverage=round6(coverage),
                match_key_count=len(keys),
                basket_size=len(items),
                entrant_count=sum(1 for quote in items.values() if quote.entrant),
                contributions=contributions,
                flags=tuple(flags),
            )
        )
    return results


def reconcile_period(
    before_level: float | None, after_level: float | None, contributions: list[Contribution]
) -> dict[str, float | bool | None]:
    if before_level is None or after_level is None:
        return {
            "total_change": None,
            "explained_change": None,
            "residual": None,
            "balanced": False,
        }
    explained = sum(item.amount for item in contributions)
    total_change = after_level - before_level
    residual = total_change - explained
    return {
        "total_change": round6(total_change),
        "explained_change": round6(explained),
        "residual": round6(residual),
        "balanced": abs(residual) <= RECONCILE_TOLERANCE,
    }

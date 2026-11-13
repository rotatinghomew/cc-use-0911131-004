"""重述分析的共享领域原语与双时态（bitemporal）工具。

双时态约定与 fixtures/analysis/说明.md 保持一致：

* ``effective_at`` / ``effective_from`` 描述业务事实的生效时点（业务时间轴）；
* ``known_at`` 描述该版本被局方获知的时点（系统时间轴）。

任何历史重算只能使用发布时点 ``as_of`` 已经 ``known_at <= as_of`` 的版本，
这是“历史发布只能使用当时已知版本”的统一入口，其他模块不得绕过 :func:`freeze`
直接读取原始记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

# 浮点对账容差：所有贡献均以未舍入值求和，1e-9 只用于最后的平衡判断。
RECONCILE_TOLERANCE = 1e-9
# 可比链接的最低匹配覆盖（基期权重口径）与最少匹配商户数。
MIN_MATCH_WEIGHT_COVERAGE = 0.5
MIN_MATCH_KEYS = 2


class RestatementError(ValueError):
    """输入合同不满足重述分析前置条件。"""


@dataclass(frozen=True, slots=True)
class Observation:
    """逐笔报价在某个系统时点下可见的一个版本（同 observation_id 取最高 revision）。"""

    observation_id: str
    revision: int
    merchant_id: str
    product_id: str
    category: str
    period: str
    price: float
    quantity: float
    unit: str
    effective_at: str
    known_at: str

    @property
    def key(self) -> tuple[str, str]:
        return self.merchant_id, self.product_id


@dataclass(frozen=True, slots=True)
class MembershipInterval:
    merchant_id: str
    effective_from: str
    effective_to: str | None
    known_at: str

    def covers(self, day: str) -> bool:
        if self.effective_from > day:
            return False
        return self.effective_to is None or day < self.effective_to


@dataclass(frozen=True, slots=True)
class MappingRule:
    mapping_id: str
    revision: int
    source: str
    target: str
    effective_at: str
    known_at: str


@dataclass(frozen=True, slots=True)
class UnitConversion:
    unit: str
    factor: float


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    as_of: str
    period: str
    level: float


@dataclass(slots=True)
class Quote:
    """进入指数篮子的一笔标准化报价。"""

    observation_id: str
    revision: int
    merchant_id: str
    product_id: str
    source_category: str
    category: str
    period: str
    price: float                 # 归一化后的标准单位价格
    quantity: float              # 归一化后的标准单位数量
    weight: float                # 基期支出份额口径权重（进入篮子前未归一）
    entrant: bool                # 基期篮子中不存在的（商户, 商品）
    flags: tuple[str, ...] = ()
    mapping_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.merchant_id, self.product_id


@dataclass(slots=True)
class Contribution:
    """一笔可追溯的桥接贡献（指数点口径）。

    factor 取值固定为：
    matched_price / sample_entry / sample_exit / weight_mix /
    category_migration / late_revision。
    """

    period: str
    factor: str
    amount: float
    observation_ids: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Issue:
    code: str
    severity: str
    message: str
    period: str | None = None
    observation_ids: tuple[str, ...] = ()
    mapping_ids: tuple[str, ...] = ()
    merchant_ids: tuple[str, ...] = ()


def period_day(period: str) -> str:
    """月期（YYYY-MM）的代表性业务日，用于与生效区间比较。"""
    return period + "-15"


def require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RestatementError(f"{name} 必须是对象")
    return dict(value)


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise RestatementError(f"{name} 必须是数组")
    return value


def require_text(record: Mapping[str, Any], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RestatementError(f"字段 {field_name} 必须是非空字符串: {record!r}")
    return value.strip()


def require_number(record: Mapping[str, Any], field_name: str) -> float:
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RestatementError(f"字段 {field_name} 必须是数值: {record!r}")
    return float(value)


def freeze_observations(
    observations: Sequence[Observation], as_of: str
) -> dict[tuple[str, str, str], Observation]:
    """按系统时点 as_of 冻结报价事实。

    只保留 ``known_at <= as_of`` 的版本；同一 (merchant, product, period)
    若存在多个 observation_id/版本，取最高 revision —— revision 是对同一笔
    报价的更正，绝不允许把旧 revision 再次计权。
    """
    frozen: dict[tuple[str, str, str], Observation] = {}
    for obs in observations:
        if obs.known_at > as_of:
            continue
        key = (obs.merchant_id, obs.product_id, obs.period)
        current = frozen.get(key)
        if current is None or (obs.revision, obs.known_at) > (current.revision, current.known_at):
            frozen[key] = obs
    return frozen


def freeze_membership(
    intervals: Sequence[MembershipInterval], as_of: str
) -> list[MembershipInterval]:
    return [item for item in intervals if item.known_at <= as_of]


def freeze_mappings(rules: Sequence[MappingRule], as_of: str) -> list[MappingRule]:
    return [item for item in rules if item.known_at <= as_of]


def panel_on(intervals: Sequence[MembershipInterval], day: str) -> set[str]:
    return {item.merchant_id for item in intervals if item.covers(day)}


def mapping_on(
    rules: Sequence[MappingRule], source_category: str, day: str
) -> tuple[MappingRule | None, list[MappingRule]]:
    """返回某类别在业务日 day 生效的映射规则与全部候选（用于冲突识别）。"""
    candidates = [
        rule
        for rule in rules
        if rule.source == source_category and rule.effective_at <= day
    ]
    if not candidates:
        return None, []
    # 同一生效时点出现多条不同目标即冲突；取最新生效时点的规则判断。
    latest = max(rule.effective_at for rule in candidates)
    latest_rules = [rule for rule in candidates if rule.effective_at == latest]
    targets = {rule.target for rule in latest_rules}
    if len(targets) > 1:
        return None, latest_rules
    chosen = max(latest_rules, key=lambda r: r.revision)
    return chosen, [chosen]


def with_flag(quote: Quote, flag: str) -> Quote:
    if flag in quote.flags:
        return quote
    return replace(quote, flags=(*quote.flags, flag))


def round6(value: float) -> float:
    return round(value + 0.0, 6)

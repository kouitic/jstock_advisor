"""買付価格3段階の信頼性ゲート(2026-07 BUYパイプライン第2次修正。要求仕様6節)。

安全余裕率が上限に張り付く・適正価格手法間のバラつきが大きい・有効な算出
方式が少ない等、機械的に算出した買付価格をそのまま購入判断に使ってよいか
怪しい場合、無理に低い買付価格を提示するより「適正価格の信頼性不足」として
扱う。信頼性LOWの場合、呼び出し側(domain/signals/buy_decision.py)はBUY系
判定そのものを禁止し、WATCH_FOR_PRICEまたはMANUAL_REVIEWへ格下げする。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jstock_advisor.domain.entities.enums import BuyPriceReliability, EarningsDateStatus
from jstock_advisor.domain.valuation.margin_of_safety import MarginOfSafetyResult

# 業種別モデル未適用(常にTrue)を除く5項目のうち、この件数以上該当したらLOWとする。
_MIN_CONCERNS_FOR_LOW = 2


@dataclass(frozen=True)
class BuyPriceReliabilityResult:
    reliability: BuyPriceReliability
    concerns: list[str] = field(default_factory=list)


def determine_buy_price_reliability(
    *,
    margin_result: MarginOfSafetyResult,
    maximum_entry_margin: float,
    valuation_dispersion_ratio: float | None,
    dispersion_medium_max: float,
    methods_used_count: int | None,
    data_quality_warning: bool,
    earnings_date_status: EarningsDateStatus | None,
    excluded_outlier_count: int,
    outlier_filter_blocking_reason: str | None = None,
) -> BuyPriceReliabilityResult:
    """要求仕様6節の判定基準。

    (a) 段階別上限適用前のentry_marginがmaximum_margin.entryを超える場合は
        単独でLOW(安全余裕率が最初から不足している = 打診買い価格ですら
        本来必要な余裕を確保できていない)。
    (b)(d)(e)(f)(g)は「業種別モデル未適用」を除く残り5項目で、2件以上
        該当した場合にLOWとする(業種別モデル未適用は現状すべての業種で
        常にTrueのため、単独では発火条件に数えない — さもないと毎回LOWに
        なってしまう)。

    --- BUYパイプライン第3次修正(2026-07)で追加 ---
    outlier_filter_blocking_reason(valuation_methods.py::apply_outlier_filters()
    が外れ値除外の結果を採用できず除外前へフォールバックした場合に設定される)
    がNoneでない場合、(a)と同様に単独でLOWとする。除外前の全方式をそのまま
    使っているため、methods_used_countだけでは「除外が破綻した」事実が
    見えなくなる(例: 3方式が互いを外れ値とみなし合い全滅した場合、
    フォールバック後のmethods_used_countは3のままでTOO_FEW_VALUATION_METHODS
    が発火しない)ため、この明示的なシグナルで確実にLOWへ倒す。
    """
    concerns: list[str] = []

    entry_before_cap = margin_result.entry_margin_before_cap
    exceeds_entry_cap = (
        entry_before_cap is not None and float(entry_before_cap) > maximum_entry_margin
    )
    if exceeds_entry_cap:
        concerns.append("ENTRY_MARGIN_EXCEEDS_CAP")

    if (
        valuation_dispersion_ratio is not None
        and valuation_dispersion_ratio > dispersion_medium_max
    ):
        concerns.append("HIGH_VALUATION_DISPERSION")
    if methods_used_count is not None and methods_used_count <= 2:
        concerns.append("TOO_FEW_VALUATION_METHODS")
    if data_quality_warning:
        concerns.append("DATA_QUALITY_WARNING")
    if earnings_date_status == EarningsDateStatus.STALE_PAST_DATE:
        concerns.append("STALE_EARNINGS_DATE")
    if excluded_outlier_count >= 1:
        concerns.append("VALUATION_OUTLIER_EXCLUDED")
    if outlier_filter_blocking_reason is not None:
        concerns.append(outlier_filter_blocking_reason)

    secondary_concerns = [c for c in concerns if c != "ENTRY_MARGIN_EXCEEDS_CAP"]
    is_low = (
        exceeds_entry_cap
        or outlier_filter_blocking_reason is not None
        or len(secondary_concerns) >= _MIN_CONCERNS_FOR_LOW
    )

    return BuyPriceReliabilityResult(
        reliability=BuyPriceReliability.LOW if is_low else BuyPriceReliability.OK,
        concerns=concerns,
    )

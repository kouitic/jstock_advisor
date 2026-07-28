"""データ品質検証サービス(要求仕様3節・4節・17節)。

分析前の株式分割整合性チェック(check_split_consistency)と、通知直前の
異常値検知(detect_anomalies)を担う。判定/価格の内部整合性チェックは
別関心事のため services/recommendation_consistency_validator.py が担当する
(このサービスは「データそのものが信頼できるか」、後者は「判定結果の
論理が一貫しているか」を検証する)。

いずれかでBLOCKING判定が出た場合、呼び出し側は通常の売買推奨を生成/送信
せず、DataQualityAlertを代わりに送信すること。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from jstock_advisor.config.models import AnomalyDetectionConfig, SplitConsistencyConfig
from jstock_advisor.domain.entities.enums import CorporateActionType, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.interfaces.types import CorporateActionEvent


class DataQualityIssueSeverity(StrEnum):
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


@dataclass(frozen=True)
class DataQualityIssue:
    check_name: str
    severity: DataQualityIssueSeverity
    description: str
    affected_fields: list[str]
    suppressed_values: dict[str, str]


def _closest_ratio_match(
    ratio: float, typical_ratios: list[float], tolerance_pct: float
) -> float | None:
    """ratio(またはその逆数)が典型的な分割比率の許容誤差内にあれば、その比率を返す。"""
    if ratio <= 0:
        return None
    candidates = typical_ratios + [1 / r for r in typical_ratios if r != 0]
    for candidate in candidates:
        if candidate == 0:
            continue
        deviation_pct = abs(ratio - candidate) / candidate * 100
        if deviation_pct <= tolerance_pct:
            return candidate
    return None


def check_split_consistency(
    stock_code: str,
    current_price: Decimal,
    bars_close_by_date: list[tuple[dt.date, Decimal]],
    fair_value: Decimal | None,
    actual_annual_dividend_per_share: Decimal | None,
    previous_fiscal_year_dividend_per_share: Decimal | None,
    corporate_action_events: list[CorporateActionEvent],
    holding: Holding | None,
    now: dt.datetime,
    config: SplitConsistencyConfig,
) -> list[DataQualityIssue]:
    """分析前の株式分割整合性チェック(要求仕様4節)。

    典型的な分割比率に近い乖離を検出した場合、それが既知の企業行動(分割)で
    説明できるかを確認し、説明できない場合はBLOCKING(=売買推奨を生成しない)
    とする。既知の分割で説明できる乖離は正常(=既に調整済みか、調整が
    必要であることが分かっている状態)として扱い、issueを出さない。
    """
    issues: list[DataQualityIssue] = []
    lookback_start = now.date() - dt.timedelta(days=365 * config.lookback_years)
    recent_split_dates = {
        e.effective_date
        for e in corporate_action_events
        if e.event_type in (CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT)
        and e.effective_date is not None
        and e.effective_date >= lookback_start
    }

    def _has_split_unaccounted_for_since(as_of: dt.date | None) -> bool:
        """as_of(調整基準日)より後に発生した既知の分割があればTrue
        (=as_of時点の値は、その後の分割が未反映の可能性がある)。"""
        if as_of is None:
            return bool(recent_split_dates)
        return any(d > as_of for d in recent_split_dates)

    # 1) 価格帯の不連続チェック: 前営業日比で典型的な分割比率に近い急変があり、
    #    かつその日付の前後に既知の分割イベントが無い場合は異常候補とする。
    sorted_bars = sorted(bars_close_by_date, key=lambda item: item[0])
    for i in range(1, len(sorted_bars)):
        prev_date, prev_close = sorted_bars[i - 1]
        cur_date, cur_close = sorted_bars[i]
        if prev_close <= 0 or cur_close <= 0:
            continue
        change_pct = abs(float(cur_close / prev_close - 1)) * 100
        if change_pct < config.price_discontinuity_threshold_pct:
            continue
        ratio = float(prev_close / cur_close)
        matched = _closest_ratio_match(
            ratio, config.typical_split_ratios, config.ratio_tolerance_pct
        )
        if matched is not None and not any(
            abs((d - cur_date).days) <= 5 for d in recent_split_dates
        ):
            issues.append(
                DataQualityIssue(
                    check_name="price_discontinuity_unexplained",
                    severity=DataQualityIssueSeverity.BLOCKING,
                    description=(
                        f"{prev_date}→{cur_date}の株価が約{matched}倍(またはその逆数)"
                        "変化しているが、該当時期に既知の株式分割・併合イベントが無い"
                    ),
                    affected_fields=["bars"],
                    suppressed_values={"price_jump_ratio": str(ratio)},
                )
            )

    # 2) 適正価格と現在株価の乖離が分割比率に近くないか
    if fair_value is not None and fair_value > 0:
        ratio = float(current_price / fair_value)
        matched = _closest_ratio_match(
            ratio, config.typical_split_ratios, config.ratio_tolerance_pct
        )
        if matched is not None and not recent_split_dates:
            issues.append(
                DataQualityIssue(
                    check_name="fair_value_divergence_resembles_split_ratio",
                    severity=DataQualityIssueSeverity.BLOCKING,
                    description=(
                        f"現在株価と適正価格の乖離が約{matched}倍で、典型的な分割比率に近い"
                        "(適正価格の入力データが分割前基準のまま混在している可能性)"
                    ),
                    affected_fields=["fair_value"],
                    suppressed_values={
                        "current_price": str(current_price),
                        "fair_value": str(fair_value),
                    },
                )
            )

    # 3) DPSが分割比率に近い倍率で急変していないか(分割が無いのに急変)
    if (
        actual_annual_dividend_per_share is not None
        and previous_fiscal_year_dividend_per_share is not None
        and previous_fiscal_year_dividend_per_share > 0
    ):
        ratio = float(
            previous_fiscal_year_dividend_per_share / actual_annual_dividend_per_share
        )
        matched = _closest_ratio_match(
            ratio, config.typical_split_ratios, config.ratio_tolerance_pct
        )
        if matched is not None and not recent_split_dates:
            issues.append(
                DataQualityIssue(
                    check_name="dividend_change_resembles_split_ratio",
                    severity=DataQualityIssueSeverity.BLOCKING,
                    description=(
                        f"前期比配当の変化が約{matched}倍で、典型的な分割比率に近いにもかかわらず"
                        "該当時期に既知の分割イベントが無い(分割調整の誤りの可能性)"
                    ),
                    affected_fields=["actual_annual_dividend_per_share"],
                    suppressed_values={
                        "previous": str(previous_fiscal_year_dividend_per_share),
                        "actual": str(actual_annual_dividend_per_share),
                    },
                )
            )

    # 4) 取得単価と現在株価が同じ基準へ調整されているか
    if holding is not None and holding.average_purchase_price > 0:
        ratio = float(holding.average_purchase_price / current_price)
        matched = _closest_ratio_match(
            ratio, config.typical_split_ratios, config.ratio_tolerance_pct
        )
        if matched is not None:
            unadjusted = _has_split_unaccounted_for_since(
                holding.shares_and_price_adjustment_basis_date
            )
            if unadjusted:
                issues.append(
                    DataQualityIssue(
                        check_name="purchase_price_basis_mismatch",
                        severity=DataQualityIssueSeverity.BLOCKING,
                        description=(
                            f"平均取得単価と現在株価の比率が約{matched}倍で、株式分割の影響を"
                            "受けている可能性があるにもかかわらず、保有銘柄が分割調整済みで"
                            "ない(shares_and_price_adjustment_basis_dateが未設定または古い)"
                        ),
                        affected_fields=["average_purchase_price", "shares"],
                        suppressed_values={
                            "average_purchase_price": str(holding.average_purchase_price),
                            "current_price": str(current_price),
                        },
                    )
                )

    return issues


def detect_anomalies(
    stock_code: str,
    current: Recommendation,
    previous: Recommendation | None,
    config: AnomalyDetectionConfig,
) -> list[DataQualityIssue]:
    """通知直前の異常値検知(要求仕様17節)。前回のRecommendationとの比較が
    中心。前回データが無い(初回分析)場合は、絶対値レンジのチェックのみ行う。

    判定/価格の内部整合性(同一通知内での矛盾)はrecommendation_consistency_
    validator.pyが担当するため、ここでは重複実装しない。
    """
    issues: list[DataQualityIssue] = []
    fv = current.fair_value_at_recommendation
    price = current.price_at_recommendation

    if fv is not None and price > 0:
        ratio = float(fv / price)
        if ratio < config.fair_value_min_ratio or ratio > config.fair_value_max_ratio:
            issues.append(
                DataQualityIssue(
                    check_name="fair_value_out_of_plausible_range",
                    severity=DataQualityIssueSeverity.BLOCKING,
                    description=(
                        f"適正価格({fv}円)が現在株価({price}円)の{config.fair_value_min_ratio}"
                        f"〜{config.fair_value_max_ratio}倍の範囲外"
                    ),
                    affected_fields=["fair_value_at_recommendation"],
                    suppressed_values={"fair_value": str(fv), "current_price": str(price)},
                )
            )

    if previous is not None:
        prev_fv = previous.fair_value_at_recommendation
        if fv is not None and prev_fv is not None and prev_fv > 0:
            change_pct = abs(float(fv / prev_fv - 1)) * 100
            if change_pct >= config.fair_value_change_threshold_pct:
                issues.append(
                    DataQualityIssue(
                        check_name="fair_value_changed_sharply",
                        severity=DataQualityIssueSeverity.BLOCKING,
                        description=(
                            f"前回分析から適正価格が{change_pct:.1f}%変化"
                            f"({prev_fv}円 → {fv}円)"
                        ),
                        affected_fields=["fair_value_at_recommendation"],
                        suppressed_values={"previous": str(prev_fv), "current": str(fv)},
                    )
                )

        prev_price = previous.price_at_recommendation
        if prev_price > 0:
            price_change_pct = abs(float(price / prev_price - 1)) * 100
            typical_ratios = [2.0, 3.0, 4.0, 5.0, 10.0]
            matched = _closest_ratio_match(
                float(prev_price / price) if price > 0 else 0, typical_ratios, 10.0
            )
            if matched is not None and price_change_pct >= 30.0:
                issues.append(
                    DataQualityIssue(
                        check_name="price_change_resembles_split_ratio",
                        severity=DataQualityIssueSeverity.BLOCKING,
                        description=(
                            f"前回分析からの株価変化が約{matched}倍で、未反映の株式分割の"
                            "可能性がある"
                        ),
                        affected_fields=["price_at_recommendation"],
                        suppressed_values={"previous": str(prev_price), "current": str(price)},
                    )
                )

        cur_recommended = (
            current.sell_prices.recommended_limit_price if current.sell_prices else None
        )
        prev_recommended = (
            previous.sell_prices.recommended_limit_price if previous.sell_prices else None
        )
        if (
            cur_recommended is not None
            and prev_recommended is not None
            and prev_recommended.price > 0
        ):
            change_pct = abs(float(cur_recommended.price / prev_recommended.price - 1)) * 100
            if change_pct >= config.profit_take_price_change_threshold_pct:
                issues.append(
                    DataQualityIssue(
                        check_name="profit_take_price_changed_sharply",
                        severity=DataQualityIssueSeverity.WARNING,
                        description=(
                            f"利確推奨価格が前回値から{change_pct:.1f}%変化"
                            f"({prev_recommended.price}円 → {cur_recommended.price}円)"
                        ),
                        affected_fields=["sell_prices.recommended_limit_price"],
                        suppressed_values={},
                    )
                )

        cur_yield = current.dividend_yield_pct_at_recommendation
        prev_yield = previous.dividend_yield_pct_at_recommendation
        if (
            cur_yield is not None
            and prev_yield is not None
            and abs(cur_yield - prev_yield) >= config.dividend_yield_change_threshold_pts
        ):
                issues.append(
                    DataQualityIssue(
                        check_name="dividend_yield_changed_sharply",
                        severity=DataQualityIssueSeverity.WARNING,
                        description=(
                            f"配当利回りが前回値から{abs(cur_yield - prev_yield):.2f}pt変化"
                            f"({prev_yield:.2f}% → {cur_yield:.2f}%)"
                        ),
                        affected_fields=["dividend_yield_pct_at_recommendation"],
                        suppressed_values={},
                    )
                )

        if previous.dividend_record_date is not None and current.dividend_record_date is None:
            issues.append(
                DataQualityIssue(
                    check_name="record_date_regressed_to_unknown",
                    severity=DataQualityIssueSeverity.WARNING,
                    description="権利確定日が前回取得済みだったにもかかわらず、今回は不明になった",
                    affected_fields=["dividend_record_date"],
                    suppressed_values={"previous": str(previous.dividend_record_date)},
                )
            )

    if current.recommendation_type == RecommendationType.FULL_PROFIT_TAKE:
        avg = current.average_purchase_price_at_recommendation
        if avg is not None and avg > 0:
            gain_pct = float(price / avg - 1) * 100
            if gain_pct < 0:
                issues.append(
                    DataQualityIssue(
                        check_name="full_profit_take_with_unrealized_loss",
                        severity=DataQualityIssueSeverity.BLOCKING,
                        description=(
                            f"FULL_PROFIT_TAKEなのに含み損益率が{gain_pct:.1f}%(含み損)になっている"
                        ),
                        affected_fields=["recommendation_type"],
                        suppressed_values={"unrealized_pnl_pct": str(gain_pct)},
                    )
                )

    return issues

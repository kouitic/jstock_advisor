"""domain/signals/entry_price_range.pyのテスト(判定精度向上機能次フェーズ
STEP2: Entry Price Range Shadow)。

信頼度tier別base margin表 -> Historical Valuation adjustment(floor 0) ->
Timing nudge(preferredのみtarget+strength方式) -> top-down正規化(min()に
よる一方向キャップ)という算出過程の各段階と、EntryPriceRangeResultの
Entity不変条件(model_validator)の両方を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    EntryMarginByConfidence,
    EntryMarginByConfidenceTier,
    EntryPriceRangeConfig,
    HistoricalValuationMarginAdjustmentConfig,
    TimingNudgeStrengthConfig,
)
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    PriceRangeEvaluationState,
    TimingScoreCategory,
    TimingScoreEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.signals.entry_price_range import (
    REASON_COVERAGE_BELOW_MINIMUM,
    REASON_ENTRY_ORDER_ADJUSTED,
    REASON_TIMING_NUDGE_TARGET_UNAVAILABLE,
    REASON_VALUATION_ANCHOR_UNAVAILABLE,
    REASON_VALUATION_CONFIDENCE_TOO_LOW,
    entry_price_range_config_values,
    evaluate_entry_price_range,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> EntryPriceRangeConfig:
    defaults: dict[str, object] = dict(
        model_version="entry_price_range_v1",
        margin_by_confidence_fraction=EntryMarginByConfidence(
            high=EntryMarginByConfidenceTier(max=0.02, starter=0.05, preferred=0.10, strong=0.18),
            medium=EntryMarginByConfidenceTier(
                max=0.04, starter=0.08, preferred=0.14, strong=0.24
            ),
        ),
        historical_valuation_margin_adjustment_fraction=HistoricalValuationMarginAdjustmentConfig(
            historically_very_cheap=-0.03,
            cheap=-0.015,
            normal=0.0,
            expensive=0.02,
            very_expensive=0.04,
        ),
        timing_nudge_strength_fraction=TimingNudgeStrengthConfig(
            strong_tailwind=0.20, tailwind=0.10, neutral=0.0, headwind=0.10, strong_headwind=0.20
        ),
        min_price_gap_fraction=0.02,
        historical_valuation_overlay_weight=0.34,
        timing_overlay_weight=0.33,
        technical_ma_overlay_weight=0.33,
        min_coverage_required=0.34,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.34,
    )
    defaults.update(overrides)
    return EntryPriceRangeConfig.model_validate(defaults)


_CONFIG = _config()


def _fair_value_range(
    *,
    neutral: Decimal | None = Decimal("1200"),
    bear: Decimal | None = Decimal("900"),
    bull: Decimal | None = Decimal("1500"),
    overall_confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    usable_for_trading_judgment: bool = True,
) -> FairValueRange:
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=overall_confidence,
        methods_used=[],
        methods_excluded=[],
        usable_for_trading_judgment=usable_for_trading_judgment,
    )


def _historical_valuation(
    category: HistoricalValuationCategory | None,
) -> HistoricalValuationResult:
    if category is None:
        return HistoricalValuationResult(
            state=HistoricalValuationEvaluationState.NOT_EVALUATED,
            evaluated_at=_NOW,
            model_version="test-fixture",
        )
    return HistoricalValuationResult(
        state=HistoricalValuationEvaluationState.EVALUATED,
        score=0.0,
        category=category,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _timing(category: TimingScoreCategory | None) -> TimingScoreResult:
    if category is None:
        return TimingScoreResult(
            state=TimingScoreEvaluationState.NOT_EVALUATED,
            evaluated_at=_NOW,
            model_version="test-fixture",
        )
    return TimingScoreResult(
        state=TimingScoreEvaluationState.EVALUATED,
        score=0.0,
        category=category,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _momentum(
    ma20: Decimal | None = Decimal("1000"), ma60: Decimal | None = Decimal("950")
) -> MomentumSnapshot:
    return MomentumSnapshot(
        ma20=ma20,
        ma60=ma60,
        trend_classification=TrendClassification.NEUTRAL,
        confidence=ConfidenceLevel.HIGH,
        trend_evaluable=True,
        price_history_aligned=True,
        price_history_has_future_bars=False,
    )


# --- 必須ゲート ------------------------------------------------------------


def test_not_evaluated_when_neutral_fair_value_missing() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(neutral=None),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert result.max_entry_price is None
    assert result.confidence is None
    assert result.stop_review_price is None
    assert REASON_VALUATION_ANCHOR_UNAVAILABLE in result.reason_codes


def test_not_evaluated_when_fair_value_confidence_low() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=ConfidenceLevel.LOW),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert REASON_VALUATION_CONFIDENCE_TOO_LOW in result.reason_codes


def test_not_evaluated_when_not_usable_for_trading_judgment() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(usable_for_trading_judgment=False),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED


def test_not_evaluated_when_coverage_below_minimum() -> None:
    # HV/Timing/MAすべて欠損 -> coverage=0 < min_coverage_required
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(None),
        _timing(None),
        _momentum(ma20=None, ma60=None),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert REASON_COVERAGE_BELOW_MINIMUM in result.reason_codes
    assert result.max_entry_price is None


# --- margin表 x 信頼度tier ---------------------------------------------


@pytest.mark.parametrize("confidence", [ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM])
def test_evaluated_result_satisfies_ordering_and_ceiling(confidence: ConfidenceLevel) -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=confidence),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert result.confidence is not None
    assert result.strong_entry_price is not None
    assert result.preferred_entry_price is not None
    assert result.starter_entry_price is not None
    assert result.max_entry_price is not None
    assert result.valuation_ceiling is not None
    assert (
        result.strong_entry_price
        <= result.preferred_entry_price
        <= result.starter_entry_price
        <= result.max_entry_price
        <= result.valuation_ceiling
    )


def test_medium_confidence_produces_larger_margins_than_high() -> None:
    high = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=ConfidenceLevel.HIGH),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    medium = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=ConfidenceLevel.MEDIUM),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert high.strong_entry_price is not None
    assert medium.strong_entry_price is not None
    # MEDIUM confidenceはmarginが大きい(=価格が低い、より安全側)
    assert medium.strong_entry_price < high.strong_entry_price


# --- Historical Valuation adjustment(floor 0) --------------------------


def test_historical_valuation_cheap_lowers_margin() -> None:
    normal = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    very_cheap = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert normal.max_entry_price is not None
    assert very_cheap.max_entry_price is not None
    # CHEAP系はmargin縮小(=より高い価格でも「打診買い」帯に入る)
    assert very_cheap.max_entry_price > normal.max_entry_price


def test_historical_valuation_adjustment_floors_at_zero_not_negative() -> None:
    # maxのbase marginは0.02、very_expensive adjustmentが-100%(-1.0)でも
    # adjusted_marginは0未満にならない(floor)ため、max_entry_price ==
    # valuation_ceiling(margin=0相当)を超えることはない。
    config = _config(
        historical_valuation_margin_adjustment_fraction=HistoricalValuationMarginAdjustmentConfig(
            historically_very_cheap=-0.5,
            cheap=-0.015,
            normal=0.0,
            expensive=0.02,
            very_expensive=0.04,
        )
    )
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        config,
    )
    assert result.max_entry_price is not None
    assert result.valuation_ceiling is not None
    assert result.max_entry_price <= result.valuation_ceiling


@pytest.mark.parametrize(
    "category",
    list(HistoricalValuationCategory),
)
def test_historical_valuation_category_mapping_is_exhaustive(
    category: HistoricalValuationCategory,
) -> None:
    """全てのHistoricalValuationCategoryが例外なくconfig値へ解決されること
    (コードレビュー対応STEP2 §10)。"""
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(category),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED


# --- Timing nudge(preferredのみ) ----------------------------------------


def test_timing_tailwind_nudges_preferred_toward_current_price() -> None:
    neutral = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1190"),
        _NOW,
        _CONFIG,
    )
    tailwind = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.STRONG_TAILWIND),
        _momentum(),
        Decimal("1190"),
        _NOW,
        _CONFIG,
    )
    assert neutral.preferred_entry_price is not None
    assert tailwind.preferred_entry_price is not None
    # 現在値(1190)がpreferredより高いため、tailwindはpreferredを現在値側へ
    # 引き上げる(ただしmax以下のtop-down制約により最終的にはmax以下)。
    assert tailwind.preferred_entry_price >= neutral.preferred_entry_price
    assert tailwind.max_entry_price is not None
    assert tailwind.preferred_entry_price <= tailwind.max_entry_price


def test_timing_headwind_nudges_preferred_toward_technical_target() -> None:
    neutral = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(ma20=Decimal("900"), ma60=Decimal("850")),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    headwind = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.STRONG_HEADWIND),
        _momentum(ma20=Decimal("900"), ma60=Decimal("850")),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert neutral.preferred_entry_price is not None
    assert headwind.preferred_entry_price is not None
    # HEADWIND系はpreferredを技術的target(MA)側=より安い方へ引き下げる
    assert headwind.preferred_entry_price <= neutral.preferred_entry_price


def test_timing_headwind_without_technical_target_skips_nudge_with_reason_code() -> None:
    # MAが両方ともNoneでcoverageが最低限を満たすよう、HV/Timingで補う設定にする。
    config = _config(min_coverage_required=0.1)
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.STRONG_HEADWIND),
        _momentum(ma20=None, ma60=None),
        Decimal("1000"),
        _NOW,
        config,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert REASON_TIMING_NUDGE_TARGET_UNAVAILABLE in result.reason_codes


# --- top-down正規化 ---------------------------------------------------------


def test_ordering_adjusted_flag_true_when_nudge_pushes_beyond_gap() -> None:
    # timingのnudge strengthを極端に大きくし、preferredが現在値近くまで
    # 押し上げられてstarterとの最低ギャップを侵害する状況を作る。
    config = _config(
        timing_nudge_strength_fraction=TimingNudgeStrengthConfig(
            strong_tailwind=5.0, tailwind=0.0, neutral=0.0, headwind=0.0, strong_headwind=0.0
        )
    )
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.STRONG_TAILWIND),
        _momentum(),
        Decimal("1195"),
        _NOW,
        config,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert result.ordering_adjusted is True
    assert REASON_ENTRY_ORDER_ADJUSTED in result.reason_codes
    # それでも不変条件(strong<=preferred<=starter<=max<=ceiling)は保たれる。
    assert result.strong_entry_price is not None
    assert result.max_entry_price is not None
    assert result.strong_entry_price <= result.max_entry_price


# --- MA coverageの部分点 -----------------------------------------------


def test_ma_partial_availability_contributes_half_coverage() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(),
        _historical_valuation(None),
        _timing(None),
        _momentum(ma20=Decimal("1000"), ma60=None),
        Decimal("1000"),
        _NOW,
        _config(min_coverage_required=0.1),
    )
    # technical_ma_overlay_weight(0.33) * 0.5 = 0.165程度のcoverageになる
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert result.coverage == pytest.approx(_CONFIG.technical_ma_overlay_weight * 0.5)


# --- confidence組み合わせマトリクス ---------------------------------------


def test_confidence_is_weaker_of_fair_value_and_overlay() -> None:
    # overlay coverageを高信頼(HIGH)にできる設定、FV=MEDIUMの場合は
    # 全体としてMEDIUMになる(弱い方が採用される)。
    result = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=ConfidenceLevel.MEDIUM),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_confidence_overlay_low_caps_fair_value_high() -> None:
    # MA両方欠損 -> coverage = hv_weight(0.34) + timing_weight(0.33) = 0.67。
    # coverage_medium_threshold=0.9より低いためoverlay側はLOWとなり、
    # FV=HIGHより弱い方(LOW)が採用される。
    config = _config(
        min_coverage_required=0.1, coverage_medium_threshold=0.9, coverage_high_threshold=0.95
    )
    result = evaluate_entry_price_range(
        _fair_value_range(overall_confidence=ConfidenceLevel.HIGH),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(ma20=None, ma60=None),
        Decimal("1000"),
        _NOW,
        config,
    )
    assert result.confidence == ConfidenceLevel.LOW


# --- stop_review_price ----------------------------------------------------


def test_stop_review_price_none_without_bear() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(bear=None),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert result.stop_review_price is None


def test_stop_review_price_equals_bear_when_present() -> None:
    result = evaluate_entry_price_range(
        _fair_value_range(bear=Decimal("850")),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        _momentum(),
        Decimal("1000"),
        _NOW,
        _CONFIG,
    )
    assert result.stop_review_price == Decimal("850")


# --- Entity不変条件(model_validator) ------------------------------------


def test_entity_rejects_ordering_violation() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1000"),
            valuation_ceiling=Decimal("1200"),
            starter_entry_price=Decimal("1100"),
            preferred_entry_price=Decimal("1150"),  # starterより高い(逆転)
            strong_entry_price=Decimal("900"),
            max_entry_price=Decimal("1190"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_max_exceeding_valuation_ceiling() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1000"),
            valuation_ceiling=Decimal("1000"),
            starter_entry_price=Decimal("1050"),
            preferred_entry_price=Decimal("1000"),
            strong_entry_price=Decimal("900"),
            max_entry_price=Decimal("1100"),  # ceiling(1000)超過
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_evaluated_with_missing_price() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1000"),
            starter_entry_price=Decimal("1100"),
            preferred_entry_price=Decimal("1050"),
            strong_entry_price=Decimal("900"),
            max_entry_price=None,  # EVALUATEDなのに欠損
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_not_evaluated_with_nonnull_price() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=Decimal("1000"),
            max_entry_price=Decimal("1100"),
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_not_evaluated_with_nonnull_stop_review_price() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=Decimal("1000"),
            stop_review_price=Decimal("800"),
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_evaluated_without_valuation_ceiling() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1000"),
            valuation_ceiling=None,  # EVALUATEDなのに欠損
            starter_entry_price=Decimal("1100"),
            preferred_entry_price=Decimal("1050"),
            strong_entry_price=Decimal("900"),
            max_entry_price=Decimal("1150"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_rejects_nonpositive_valuation_ceiling() -> None:
    with pytest.raises(ValidationError):
        EntryPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1000"),
            valuation_ceiling=Decimal("0"),
            starter_entry_price=Decimal("1100"),
            preferred_entry_price=Decimal("1050"),
            strong_entry_price=Decimal("900"),
            max_entry_price=Decimal("1150"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="entry_price_range_v1",
        )


def test_entity_accepts_not_evaluated_with_all_none() -> None:
    result = EntryPriceRangeResult(
        state=PriceRangeEvaluationState.NOT_EVALUATED,
        current_price=Decimal("1000"),
        evaluated_at=_NOW,
        model_version="entry_price_range_v1",
    )
    assert result.max_entry_price is None
    assert result.confidence is None


# --- config_values ----------------------------------------------------------


def test_entry_price_range_config_values_includes_all_settings() -> None:
    values = entry_price_range_config_values(_CONFIG)
    assert values["model_version"] == "entry_price_range_v1"
    assert "margin_by_confidence_fraction" in values
    assert "historical_valuation_margin_adjustment_fraction" in values
    assert "timing_nudge_strength_fraction" in values
    assert values["min_coverage_required"] == _CONFIG.min_coverage_required

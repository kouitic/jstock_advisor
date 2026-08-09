"""domain/signals/timing_score.pyのテスト(判定精度向上機能Phase B第二弾)。

既存MomentumSnapshotを基にした成分別スコア変換(trend/RSI/MACD/TOPIX・
セクター相対強度/直近高値からの下落率)、成分欠損時の除外、coverage/
confidence判定、min_coverage_requiredによるNOT_EVALUATEDゲート、カテゴリ
分類を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import (
    TimingScoreCategoryThresholds,
    TimingScoreRulesConfig,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    TimingScoreCategory,
    TimingScoreEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MacdResult, MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.signals.timing_score import evaluate_timing_score

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> TimingScoreRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="timing_score_v1",
        trend_weight=1.0,
        rsi_weight=1.0,
        macd_weight=1.0,
        topix_relative_strength_weight=1.0,
        sector_relative_strength_weight=1.0,
        drawdown_weight=1.0,
        relative_strength_full_scale_pct=15.0,
        drawdown_full_scale_pct=20.0,
        min_coverage_required=0.3,
        coverage_high_threshold=0.8,
        coverage_medium_threshold=0.4,
        category_thresholds=TimingScoreCategoryThresholds(
            strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
        ),
    )
    defaults.update(overrides)
    return TimingScoreRulesConfig.model_validate(defaults)


_CONFIG = _config()


def _momentum(
    *,
    trend_classification: TrendClassification = TrendClassification.NEUTRAL,
    rsi: float | None = None,
    macd: MacdResult | None = None,
    relative_strength_vs_topix_pct: float | None = None,
    relative_strength_vs_sector_pct: float | None = None,
    drawdown_from_recent_high_pct: float | None = None,
) -> MomentumSnapshot:
    return MomentumSnapshot(
        trend_classification=trend_classification,
        rsi=rsi,
        macd=macd,
        relative_strength_vs_topix_pct=relative_strength_vs_topix_pct,
        relative_strength_vs_sector_pct=relative_strength_vs_sector_pct,
        drawdown_from_recent_high_pct=drawdown_from_recent_high_pct,
        confidence=ConfidenceLevel.HIGH,
    )


def _evaluate(
    momentum: MomentumSnapshot, config: TimingScoreRulesConfig | None = None
) -> TimingScoreResult:
    return evaluate_timing_score(momentum, _NOW, config or _CONFIG)


# ===== trend成分(1-5) =====


def test_trend_strong_uptrend_scores_positive_100() -> None:
    result = _evaluate(_momentum(trend_classification=TrendClassification.STRONG_UPTREND))
    assert result.trend_component == 100.0


def test_trend_uptrend_scores_positive_50() -> None:
    result = _evaluate(_momentum(trend_classification=TrendClassification.UPTREND))
    assert result.trend_component == 50.0


def test_trend_neutral_scores_zero() -> None:
    result = _evaluate(_momentum(trend_classification=TrendClassification.NEUTRAL))
    assert result.trend_component == 0.0


def test_trend_downtrend_scores_negative_50() -> None:
    result = _evaluate(_momentum(trend_classification=TrendClassification.DOWNTREND))
    assert result.trend_component == -50.0


def test_trend_strong_downtrend_scores_negative_100() -> None:
    result = _evaluate(_momentum(trend_classification=TrendClassification.STRONG_DOWNTREND))
    assert result.trend_component == -100.0


# ===== RSI成分(6-10) =====


def test_rsi_none_is_unavailable() -> None:
    result = _evaluate(_momentum(rsi=None))
    assert result.rsi_component is None
    assert "RSI_UNAVAILABLE" in result.reason_codes


def test_rsi_50_scores_zero() -> None:
    result = _evaluate(_momentum(rsi=50.0))
    assert result.rsi_component == 0.0


def test_rsi_100_scores_positive_100() -> None:
    result = _evaluate(_momentum(rsi=100.0))
    assert result.rsi_component == 100.0


def test_rsi_0_scores_negative_100() -> None:
    result = _evaluate(_momentum(rsi=0.0))
    assert result.rsi_component == -100.0


def test_rsi_70_scores_positive_40() -> None:
    result = _evaluate(_momentum(rsi=70.0))
    assert result.rsi_component == 40.0


# ===== MACD成分(11-13) =====


def test_macd_none_is_unavailable() -> None:
    result = _evaluate(_momentum(macd=None))
    assert result.macd_component is None
    assert "MACD_UNAVAILABLE" in result.reason_codes


def test_macd_positive_histogram_scores_positive_100() -> None:
    macd = MacdResult(
        macd_line=Decimal("1.0"), signal_line=Decimal("0.5"), histogram=Decimal("0.5")
    )
    result = _evaluate(_momentum(macd=macd))
    assert result.macd_component == 100.0


def test_macd_negative_histogram_scores_negative_100() -> None:
    macd = MacdResult(
        macd_line=Decimal("0.5"), signal_line=Decimal("1.0"), histogram=Decimal("-0.5")
    )
    result = _evaluate(_momentum(macd=macd))
    assert result.macd_component == -100.0


# ===== TOPIX/セクター相対強度成分(14-17) =====


def test_topix_relative_strength_none_is_unavailable() -> None:
    result = _evaluate(_momentum(relative_strength_vs_topix_pct=None))
    assert result.topix_relative_strength_component is None
    assert "TOPIX_RELATIVE_STRENGTH_UNAVAILABLE" in result.reason_codes


def test_topix_relative_strength_within_scale_is_proportional() -> None:
    result = _evaluate(_momentum(relative_strength_vs_topix_pct=7.5))
    assert result.topix_relative_strength_component == 50.0


def test_topix_relative_strength_beyond_scale_is_clamped() -> None:
    result = _evaluate(_momentum(relative_strength_vs_topix_pct=30.0))
    assert result.topix_relative_strength_component == 100.0


def test_sector_relative_strength_none_is_unavailable() -> None:
    result = _evaluate(_momentum(relative_strength_vs_sector_pct=None))
    assert result.sector_relative_strength_component is None
    assert "SECTOR_RELATIVE_STRENGTH_UNAVAILABLE" in result.reason_codes


# ===== drawdown成分(18-21) =====


def test_drawdown_none_is_unavailable() -> None:
    result = _evaluate(_momentum(drawdown_from_recent_high_pct=None))
    assert result.drawdown_component is None
    assert "DRAWDOWN_UNAVAILABLE" in result.reason_codes


def test_drawdown_at_recent_high_scores_positive_100() -> None:
    result = _evaluate(_momentum(drawdown_from_recent_high_pct=0.0))
    assert result.drawdown_component == 100.0


def test_drawdown_at_full_scale_scores_zero() -> None:
    result = _evaluate(_momentum(drawdown_from_recent_high_pct=-20.0))
    assert result.drawdown_component == 0.0


def test_drawdown_beyond_full_scale_is_clamped() -> None:
    result = _evaluate(_momentum(drawdown_from_recent_high_pct=-50.0))
    assert result.drawdown_component == -100.0


# ===== coverage/confidence/NOT_EVALUATEDゲート(22-27) =====


def test_all_components_available_yields_full_coverage_and_high_confidence() -> None:
    macd = MacdResult(
        macd_line=Decimal("1.0"), signal_line=Decimal("0.5"), histogram=Decimal("0.5")
    )
    result = _evaluate(
        _momentum(
            trend_classification=TrendClassification.UPTREND,
            rsi=60.0,
            macd=macd,
            relative_strength_vs_topix_pct=5.0,
            relative_strength_vs_sector_pct=5.0,
            drawdown_from_recent_high_pct=-2.0,
        )
    )
    assert result.state == TimingScoreEvaluationState.EVALUATED
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH
    assert result.score is not None


def test_trend_only_below_min_coverage_returns_not_evaluated() -> None:
    """trend成分のみ利用可能(1/6の重み=coverage約0.167)はmin_coverage_required
    (既定0.3)未満のためNOT_EVALUATEDとする(未来情報が無くても低品質スコアを
    出さない安全側の設計)。"""
    result = _evaluate(_momentum(trend_classification=TrendClassification.UPTREND))
    assert result.state == TimingScoreEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.confidence is None


def test_coverage_at_min_required_is_evaluated() -> None:
    """coverageがmin_coverage_required以上になるよう、2/6成分(trend+rsi)を
    利用可能にすると評価される(閾値0.3に対しcoverage=0.333)。"""
    result = _evaluate(_momentum(trend_classification=TrendClassification.NEUTRAL, rsi=50.0))
    assert result.state == TimingScoreEvaluationState.EVALUATED
    assert round(result.coverage, 3) == round(2 / 6, 3)


def test_medium_coverage_yields_medium_confidence() -> None:
    result = _evaluate(
        _momentum(
            trend_classification=TrendClassification.NEUTRAL, rsi=50.0, macd=None,
            relative_strength_vs_topix_pct=1.0,
        ),
        config=_config(coverage_high_threshold=0.9, coverage_medium_threshold=0.3),
    )
    assert result.state == TimingScoreEvaluationState.EVALUATED
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_no_evaluable_data_returns_not_evaluated_with_none_score() -> None:
    # min_coverage_requiredを0にしてもtrendだけでは意味のあるテストにならないため、
    # 全成分の重みを持つconfigのままtrendのみ利用可能な状態(既定ゲート)で確認済み
    # (test_trend_only_below_min_coverage_returns_not_evaluated参照)。ここでは
    # 明示的にmin_coverage_requiredを1.0(全成分必須)にしてゲートを確認する。
    macd = MacdResult(
        macd_line=Decimal("1.0"), signal_line=Decimal("0.5"), histogram=Decimal("0.5")
    )
    result = _evaluate(
        _momentum(
            trend_classification=TrendClassification.UPTREND,
            rsi=60.0,
            macd=macd,
            relative_strength_vs_topix_pct=5.0,
            relative_strength_vs_sector_pct=None,
            drawdown_from_recent_high_pct=-2.0,
        ),
        config=_config(min_coverage_required=1.0),
    )
    assert result.state == TimingScoreEvaluationState.NOT_EVALUATED
    assert result.score is None


# ===== カテゴリ分類・model_version(28-29) =====


def test_category_classification_matches_thresholds() -> None:
    result = _evaluate(
        _momentum(trend_classification=TrendClassification.STRONG_UPTREND, rsi=100.0)
    )
    assert result.category == TimingScoreCategory.STRONG_TAILWIND


def test_model_version_matches_config() -> None:
    result = _evaluate(
        _momentum(trend_classification=TrendClassification.NEUTRAL, rsi=50.0),
        config=_config(model_version="test_v99"),
    )
    assert result.model_version == "test_v99"

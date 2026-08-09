"""domain/signals/timing_score.pyのテスト(判定精度向上機能Phase B第二弾、
コードレビュー対応でv2へ全面改修)。

v1は「モメンタムが強いほど高得点」になっていた(RSI高値・直近高値ぴったり・
STRONG_UPTREND満点が最高評価)。v2では「良いトレンドを維持しながら、過熱して
おらず、エントリーしやすい価格位置にあるか」を評価する。trend_qualityは
RSIを使わず(既存classify_trend()のSTRONG判定にRSIが使われているため、
二重評価を防ぐためTiming Score専用に独立算出)、price_vs_ma20/ma60・
drawdownの「適度な押し目」評価はtrend_qualityが0以下の場合0へキャップされる。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    TimingScoreCategoryThresholds,
    TimingScoreRulesConfig,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    TimingScoreCategory,
    TimingScoreEvaluationState,
)
from jstock_advisor.domain.entities.momentum import MacdResult, MomentumSnapshot
from jstock_advisor.domain.signals.timing_score import evaluate_timing_score

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> TimingScoreRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="timing_score_v2",
        trend_quality_weight=1.5,
        price_vs_ma20_weight=1.0,
        price_vs_ma60_weight=0.75,
        rsi_weight=1.0,
        macd_weight=0.5,
        drawdown_weight=1.0,
        volume_weight=0.5,
        overheat_penalty_weight=0.75,
        trend_slope_full_scale_pct=10.0,
        rsi_oversold_boundary=30.0,
        rsi_neutral_boundary=45.0,
        rsi_sweet_spot_boundary=60.0,
        rsi_caution_boundary=70.0,
        rsi_overheat_boundary=80.0,
        drawdown_near_high_pct=-2.0,
        drawdown_pullback_pct=-8.0,
        drawdown_neutral_pct=-15.0,
        ma20_breakdown_pct=-15.0,
        ma20_pullback_low_pct=-8.0,
        ma20_near_high_pct=3.0,
        ma20_overheat_pct=10.0,
        ma60_breakdown_pct=-20.0,
        ma60_pullback_low_pct=-10.0,
        ma60_near_high_pct=5.0,
        ma60_overheat_pct=15.0,
        volume_low_threshold=0.7,
        volume_moderate_low=1.2,
        volume_moderate_high=2.5,
        volume_extreme_threshold=3.0,
        overheat_five_day_return_pct_threshold=15.0,
        overheat_rsi_threshold=80.0,
        overheat_drawdown_pct_threshold=-2.0,
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
    ma20: Decimal | None = None,
    ma60: Decimal | None = None,
    ma20_slope_pct: float | None = None,
    trend_evaluable: bool | None = None,
    rsi: float | None = None,
    macd: MacdResult | None = None,
    drawdown_from_recent_high_pct: float | None = None,
    volume_ratio: float | None = None,
    one_day_return_pct: float | None = None,
    five_day_return_pct: float | None = None,
    relative_strength_vs_topix_pct: float | None = None,
    relative_strength_vs_sector_pct: float | None = None,
) -> MomentumSnapshot:
    from jstock_advisor.domain.entities.enums import TrendClassification

    if trend_evaluable is None:
        # 実装のcompute_momentum_snapshot()と同じ規則: ma20/ma60/ma20_slope_pct
        # が全て揃っている場合のみtrend_evaluable=True(不整合なfixtureを防ぐ)。
        trend_evaluable = ma20 is not None and ma60 is not None and ma20_slope_pct is not None

    return MomentumSnapshot(
        ma20=ma20,
        ma60=ma60,
        ma20_slope_pct=ma20_slope_pct,
        trend_classification=TrendClassification.NEUTRAL,
        trend_evaluable=trend_evaluable,
        rsi=rsi,
        macd=macd,
        drawdown_from_recent_high_pct=drawdown_from_recent_high_pct,
        volume_ratio=volume_ratio,
        one_day_return_pct=one_day_return_pct,
        five_day_return_pct=five_day_return_pct,
        relative_strength_vs_topix_pct=relative_strength_vs_topix_pct,
        relative_strength_vs_sector_pct=relative_strength_vs_sector_pct,
        confidence=ConfidenceLevel.HIGH,
    )


def _evaluate(
    momentum: MomentumSnapshot,
    current_price: Decimal = Decimal("1000"),
    config: TimingScoreRulesConfig | None = None,
):
    return evaluate_timing_score(momentum, current_price, _NOW, config or _CONFIG)


def _macd(histogram: str) -> MacdResult:
    return MacdResult(
        macd_line=Decimal("1.0"), signal_line=Decimal("0.5"), histogram=Decimal(histogram)
    )


# ===== trend_quality成分(1-6) =====


def test_trend_quality_unavailable_when_not_evaluable() -> None:
    result = _evaluate(_momentum(trend_evaluable=False))
    assert result.trend_quality_component is None
    assert "TREND_UNAVAILABLE" in result.reason_codes


def test_trend_quality_bullish_stack_and_positive_slope_scores_high() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0),
        current_price=Decimal("1000"),
    )
    # stack_score=+100(price>ma20>ma60), slope_score=+100(10/10*100) -> (100+100)/2=100
    assert result.trend_quality_component == 100.0


def test_trend_quality_bearish_stack_and_negative_slope_scores_low() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("1050"), ma60=Decimal("1100"), ma20_slope_pct=-10.0),
        current_price=Decimal("1000"),
    )
    assert result.trend_quality_component == -100.0


def test_trend_quality_mixed_stack_uses_slope_only() -> None:
    # price<ma20だがma20>ma60(整列していない) -> stack_score=0、slopeのみ反映。
    result = _evaluate(
        _momentum(ma20=Decimal("1050"), ma60=Decimal("900"), ma20_slope_pct=5.0),
        current_price=Decimal("1000"),
    )
    assert result.trend_quality_component == 25.0  # (0 + 50)/2


def test_trend_quality_does_not_use_rsi() -> None:
    """RSIの二重評価防止(コードレビュー最重要指摘): 同じMA/slope条件で
    RSIだけを変えてもtrend_quality_componentは変化しない。"""
    momentum_a = _momentum(
        ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0, rsi=55.0
    )
    momentum_b = _momentum(
        ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0, rsi=90.0
    )
    result_a = _evaluate(momentum_a, current_price=Decimal("1000"))
    result_b = _evaluate(momentum_b, current_price=Decimal("1000"))
    assert result_a.trend_quality_component == result_b.trend_quality_component


def test_trend_quality_slope_is_clamped() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("950"), ma60=Decimal("1000"), ma20_slope_pct=50.0),
        current_price=Decimal("1000"),
    )
    # stack=0(price>ma20だがma20<ma60), slope=clamp(50/10*100,...)=100 -> (0+100)/2=50
    assert result.trend_quality_component == 50.0


# ===== RSI成分(7-13) =====


def test_rsi_none_is_unavailable() -> None:
    result = _evaluate(_momentum(rsi=None))
    assert result.rsi_component is None
    assert "RSI_UNAVAILABLE" in result.reason_codes


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [
        (20.0, -20.0),
        (35.0, -10.0),
        (55.0, 80.0),
        (65.0, 30.0),
        (75.0, -40.0),
        (90.0, -100.0),
    ],
)
def test_rsi_zone_scores(rsi: float, expected: float) -> None:
    result = _evaluate(_momentum(rsi=rsi))
    assert result.rsi_component == expected


# ===== MACD成分(14-15、既存どおり) =====


def test_macd_none_is_unavailable() -> None:
    result = _evaluate(_momentum(macd=None))
    assert result.macd_component is None
    assert "MACD_UNAVAILABLE" in result.reason_codes


def test_macd_positive_histogram_scores_positive_100() -> None:
    result = _evaluate(_momentum(macd=_macd("0.5")))
    assert result.macd_component == 100.0


# ===== drawdown成分・trend条件付け(16-20) =====


def test_drawdown_none_is_unavailable() -> None:
    result = _evaluate(_momentum(drawdown_from_recent_high_pct=None))
    assert result.drawdown_component is None
    assert "DRAWDOWN_UNAVAILABLE" in result.reason_codes


def test_drawdown_pullback_scores_higher_than_near_high_when_trend_positive() -> None:
    """UPTREND相当(trend_quality>0)で高値から-5%(押し目)は、0%(高値ぴったり)
    より高く評価される(コードレビュー対応: 高値ぴったりを最高点にしない)。"""
    momentum = _momentum(
        ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0
    )
    result_pullback = _evaluate(
        _momentum(
            ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0,
            drawdown_from_recent_high_pct=-5.0,
        )
    )
    result_at_high = _evaluate(
        _momentum(
            ma20=Decimal("950"), ma60=Decimal("900"), ma20_slope_pct=10.0,
            drawdown_from_recent_high_pct=0.0,
        )
    )
    assert momentum.trend_evaluable
    assert result_pullback.drawdown_component == 80.0
    assert result_at_high.drawdown_component == 20.0
    assert result_pullback.drawdown_component > result_at_high.drawdown_component


def test_drawdown_pullback_is_capped_when_trend_not_positive() -> None:
    """下降トレンド中は、-5%(押し目区分)でも押し目ボーナスを与えない
    (コードレビュー対応: 「高値から下がった」=「押し目」としない)。"""
    result = _evaluate(
        _momentum(
            ma20=Decimal("1050"), ma60=Decimal("1100"), ma20_slope_pct=-10.0,
            drawdown_from_recent_high_pct=-5.0,
        )
    )
    assert result.drawdown_component == 0.0


def test_drawdown_pullback_is_capped_when_trend_unavailable() -> None:
    result = _evaluate(
        _momentum(trend_evaluable=False, drawdown_from_recent_high_pct=-5.0)
    )
    assert result.drawdown_component == 0.0


def test_drawdown_strong_downtrend_deep_drop_scores_low() -> None:
    result = _evaluate(
        _momentum(
            ma20=Decimal("1150"), ma60=Decimal("1200"), ma20_slope_pct=-10.0,
            drawdown_from_recent_high_pct=-20.0,
        )
    )
    assert result.drawdown_component == -80.0


# ===== MA20/MA60成分・trend条件付け(21-27) =====


def test_ma20_near_high_scores_high_when_trend_positive() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("990"), ma60=Decimal("900"), ma20_slope_pct=10.0),
        current_price=Decimal("1000"),  # dev = +1.01%
    )
    assert result.price_vs_ma20_component == 80.0


def test_ma20_near_high_is_capped_when_trend_not_positive() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("990"), ma60=Decimal("1100"), ma20_slope_pct=-10.0),
        current_price=Decimal("1000"),  # dev = +1.01%, ma20<ma60は下向き整列扱い
    )
    assert result.price_vs_ma20_component == 0.0


def test_ma20_overheat_scores_penalty() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("847"), ma60=Decimal("800"), ma20_slope_pct=5.0),
        current_price=Decimal("1000"),  # dev = +18.06%
    )
    assert result.price_vs_ma20_component == -100.0


def test_ma20_breakdown_scores_penalty() -> None:
    result = _evaluate(
        _momentum(ma20=Decimal("1200"), ma60=Decimal("1300"), ma20_slope_pct=-5.0),
        current_price=Decimal("1000"),  # dev = -16.67%
    )
    assert result.price_vs_ma20_component == -100.0


def test_ma20_none_is_unavailable() -> None:
    result = _evaluate(_momentum(ma20=None))
    assert result.price_vs_ma20_component is None
    assert "MA20_UNAVAILABLE" in result.reason_codes


def test_ma60_undercut_with_downtrend_scores_low() -> None:
    """MA60を大幅に下回る状態を「近いから高評価」としない(コードレビュー対応、
    abs()による対称評価の撤回)。"""
    result = _evaluate(
        _momentum(ma20=Decimal("1100"), ma60=Decimal("1300"), ma20_slope_pct=-10.0),
        current_price=Decimal("1000"),  # ma60からdev≈-23.1%(breakdown境界-20%を明確に下回る)
    )
    assert result.price_vs_ma60_component == -100.0


def test_ma60_none_is_unavailable() -> None:
    result = _evaluate(_momentum(ma60=None))
    assert result.price_vs_ma60_component is None
    assert "MA60_UNAVAILABLE" in result.reason_codes


# ===== volume成分(28-30) =====


def test_volume_none_is_unavailable() -> None:
    result = _evaluate(_momentum(volume_ratio=None))
    assert result.volume_component is None
    assert "VOLUME_UNAVAILABLE" in result.reason_codes


def test_volume_moderate_increase_scores_positive() -> None:
    result = _evaluate(_momentum(volume_ratio=1.5))
    assert result.volume_component == 50.0


def test_volume_extreme_surge_alone_is_not_rewarded() -> None:
    """単純に出来高が多いほど加点しない(コードレビュー対応)。"""
    result = _evaluate(_momentum(volume_ratio=5.0))
    assert result.volume_component == -50.0


# ===== overheat_penalty成分(31-33) =====


def test_overheat_penalty_unavailable_when_input_missing() -> None:
    result = _evaluate(
        _momentum(five_day_return_pct=None, rsi=85.0, drawdown_from_recent_high_pct=-1.0)
    )
    assert result.overheat_penalty_component is None
    assert "OVERHEAT_PENALTY_UNAVAILABLE" in result.reason_codes


def test_overheat_penalty_triggers_when_all_conditions_met() -> None:
    result = _evaluate(
        _momentum(five_day_return_pct=18.0, rsi=85.0, drawdown_from_recent_high_pct=-0.5)
    )
    assert result.overheat_penalty_component == -100.0


def test_overheat_penalty_does_not_trigger_when_not_all_conditions_met() -> None:
    result = _evaluate(
        _momentum(five_day_return_pct=3.0, rsi=85.0, drawdown_from_recent_high_pct=-0.5)
    )
    assert result.overheat_penalty_component == 0.0


# ===== TOPIX/セクター相対強度の非影響(34-35) =====


def test_topix_relative_strength_does_not_affect_score() -> None:
    base = _momentum(rsi=50.0, relative_strength_vs_topix_pct=1.0)
    changed = _momentum(rsi=50.0, relative_strength_vs_topix_pct=50.0)
    assert _evaluate(base).score == _evaluate(changed).score


def test_sector_relative_strength_does_not_affect_score() -> None:
    base = _momentum(rsi=50.0, relative_strength_vs_sector_pct=1.0)
    changed = _momentum(rsi=50.0, relative_strength_vs_sector_pct=-50.0)
    assert _evaluate(base).score == _evaluate(changed).score


# ===== coverage/confidence/NOT_EVALUATEDゲート(36-39) =====


def test_min_coverage_gate_returns_not_evaluated() -> None:
    """rsiのみ利用可能(coverage=1.0/7.0≈0.14)はmin_coverage_required(0.3)未満
    のためNOT_EVALUATED。"""
    result = _evaluate(_momentum(trend_evaluable=False, rsi=50.0))
    assert result.state == TimingScoreEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.confidence is None


def test_missing_components_are_not_counted_as_zero() -> None:
    """欠損成分は0点として加算されない(利用可能な成分の重みのみで正規化)。"""
    result = _evaluate(
        _momentum(
            ma20=Decimal("990"), ma60=Decimal("900"), ma20_slope_pct=10.0, rsi=55.0
        )
    )
    assert result.state == TimingScoreEvaluationState.EVALUATED
    # trend/ma20/ma60/rsiの4成分のみ利用可能、他は欠損。
    assert result.macd_component is None
    assert result.volume_component is None


def test_all_components_available_yields_full_coverage_and_high_confidence() -> None:
    result = _evaluate(
        _momentum(
            ma20=Decimal("990"),
            ma60=Decimal("950"),
            ma20_slope_pct=5.0,
            rsi=55.0,
            macd=_macd("0.5"),
            drawdown_from_recent_high_pct=-5.0,
            volume_ratio=1.5,
            one_day_return_pct=1.0,
            five_day_return_pct=3.0,
        )
    )
    assert result.state == TimingScoreEvaluationState.EVALUATED
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH


def test_model_version_matches_config() -> None:
    result = _evaluate(_momentum(rsi=50.0), config=_config(model_version="test_v99"))
    assert result.model_version == "test_v99"


# ===== 総合シナリオ回帰テスト(40-45、最重要) =====


def _uptrend_momentum(**overrides: object) -> MomentumSnapshot:
    base = dict(
        ma20=Decimal("990"),
        ma60=Decimal("950"),
        ma20_slope_pct=5.0,
        rsi=55.0,
        macd=_macd("0.5"),
        drawdown_from_recent_high_pct=-5.0,
        volume_ratio=1.5,
        five_day_return_pct=3.0,
    )
    base.update(overrides)
    return _momentum(**base)  # type: ignore[arg-type]


def test_case_a_good_entry_scores_higher_than_case_b_overheated_strong_momentum() -> None:
    """最重要回帰テスト: 「強い銘柄ほど高得点」ではなく「入りやすい状態ほど
    高得点」になっていることを保証する。"""
    # ケースA: UPTREND相当、RSI55、MA20近辺、MA60より上、高値から-5%、急騰なし。
    case_a = _uptrend_momentum()
    # ケースB: 非常に強い上昇、RSI85、MA20から大幅上方乖離、高値圏、5日急騰。
    case_b = _momentum(
        ma20=Decimal("847"),
        ma60=Decimal("900"),
        ma20_slope_pct=15.0,
        rsi=85.0,
        macd=_macd("0.5"),
        drawdown_from_recent_high_pct=-0.5,
        volume_ratio=1.5,
        five_day_return_pct=18.0,
    )
    score_a = _evaluate(case_a, current_price=Decimal("1000")).score
    score_b = _evaluate(case_b, current_price=Decimal("1000")).score
    assert score_a is not None
    assert score_b is not None
    assert score_a > score_b


def test_case_c_good_trend_pullback_scores_higher_than_case_d_downtrend_pullback() -> None:
    case_c = _uptrend_momentum(drawdown_from_recent_high_pct=-5.0)
    case_d = _momentum(
        ma20=Decimal("1050"),
        ma60=Decimal("1100"),
        ma20_slope_pct=-10.0,
        rsi=55.0,
        macd=_macd("0.5"),
        drawdown_from_recent_high_pct=-5.0,
        volume_ratio=1.5,
        five_day_return_pct=3.0,
    )
    result_c = _evaluate(case_c, current_price=Decimal("1000"))
    result_d = _evaluate(case_d, current_price=Decimal("1000"))
    assert result_c.score is not None
    assert result_d.score is not None
    assert result_c.score > result_d.score
    assert result_d.drawdown_component is not None
    assert result_d.drawdown_component <= 0.0


def test_downtrend_with_low_rsi_does_not_score_high() -> None:
    """下降トレンド+RSI低値を「売られすぎだから買い時」として高評価しない。"""
    momentum = _momentum(
        ma20=Decimal("1050"), ma60=Decimal("1100"), ma20_slope_pct=-10.0, rsi=20.0
    )
    result = _evaluate(momentum, current_price=Decimal("1000"))
    assert result.score is not None
    assert result.score < 0.0


def test_high_zone_extreme_rsi_and_surge_does_not_reach_tailwind() -> None:
    momentum = _momentum(
        ma20=Decimal("847"),
        ma60=Decimal("900"),
        ma20_slope_pct=15.0,
        rsi=85.0,
        drawdown_from_recent_high_pct=-0.5,
        five_day_return_pct=18.0,
    )
    result = _evaluate(momentum, current_price=Decimal("1000"))
    assert result.category not in (
        TimingScoreCategory.TAILWIND,
        TimingScoreCategory.STRONG_TAILWIND,
    )


def test_extreme_ma20_deviation_lowers_score_versus_moderate_deviation() -> None:
    moderate = _uptrend_momentum()
    extreme = _uptrend_momentum(ma20=Decimal("847"), ma20_slope_pct=15.0)
    score_moderate = _evaluate(moderate, current_price=Decimal("1000")).score
    score_extreme = _evaluate(extreme, current_price=Decimal("1000")).score
    assert score_moderate is not None
    assert score_extreme is not None
    assert score_extreme < score_moderate


# ===== Config validation(46-53) =====


def test_config_rejects_negative_coverage_medium_threshold() -> None:
    with pytest.raises(ValidationError):
        _config(coverage_medium_threshold=-0.1)


def test_config_rejects_coverage_high_threshold_above_one() -> None:
    with pytest.raises(ValidationError):
        _config(coverage_high_threshold=1.1)


def test_config_rejects_category_threshold_above_100() -> None:
    with pytest.raises(ValidationError):
        _config(
            category_thresholds=TimingScoreCategoryThresholds(
                strong_tailwind=150.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
            )
        )


def test_config_rejects_category_threshold_below_negative_100() -> None:
    with pytest.raises(ValidationError):
        _config(
            category_thresholds=TimingScoreCategoryThresholds(
                strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-150.0
            )
        )


def test_config_rejects_unordered_rsi_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(rsi_neutral_boundary=80.0, rsi_sweet_spot_boundary=60.0)


def test_config_rejects_positive_drawdown_near_high_pct() -> None:
    with pytest.raises(ValidationError):
        _config(drawdown_near_high_pct=1.0)


def test_config_rejects_unordered_ma20_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(ma20_pullback_low_pct=5.0, ma20_near_high_pct=3.0)


def test_config_rejects_unordered_volume_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(volume_moderate_low=3.0, volume_moderate_high=2.5)


def test_config_rejects_min_coverage_above_medium_threshold() -> None:
    with pytest.raises(ValidationError):
        _config(min_coverage_required=0.5, coverage_medium_threshold=0.4)


def test_config_rejects_zero_weight_sum() -> None:
    with pytest.raises(ValidationError):
        _config(
            trend_quality_weight=0.0,
            price_vs_ma20_weight=0.0,
            price_vs_ma60_weight=0.0,
            rsi_weight=0.0,
            macd_weight=0.0,
            drawdown_weight=0.0,
            volume_weight=0.0,
            overheat_penalty_weight=0.0,
        )

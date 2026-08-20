"""domain/signals/timing_score.pyのテスト(判定精度向上機能Phase B第二弾、
コードレビュー対応でv2→v3へ全面改修)。

v1は「モメンタムが強いほど高得点」になっていた(RSI高値・直近高値ぴったり・
STRONG_UPTREND満点が最高評価)。v2では「良いトレンドを維持しながら、過熱して
おらず、エントリーしやすい価格位置にあるか」を評価する。trend_qualityは
RSIを使わず(既存classify_trend()のSTRONG判定にRSIが使われているため、
二重評価を防ぐためTiming Score専用に独立算出)、price_vs_ma20/ma60・
drawdownの正のスコア区分は全てtrend_qualityが0以下の場合0以下へキャップ
される。v3ではoverheat penaltyを通常の加重平均成分からbase_score算出後の
modifierへ分離し(過熱情報欠損時にスコアが底上げされる不整合を解消)、
current_priceとPriceBarの時点整合性チェック(price_history_aligned)を追加した。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
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
from jstock_advisor.domain.signals.timing_score import (
    evaluate_timing_score,
    timing_score_config_values,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> TimingScoreRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="timing_score_v4",
        trend_quality_weight=1.5,
        price_vs_ma20_weight=1.0,
        price_vs_ma60_weight=0.75,
        rsi_weight=1.0,
        macd_weight=0.5,
        drawdown_weight=1.0,
        volume_weight=0.5,
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
        overheat_penalty_points=25.0,
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
    price_history_aligned: bool = True,
    price_history_has_future_bars: bool = False,
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
        price_history_aligned=price_history_aligned,
        price_history_has_future_bars=price_history_has_future_bars,
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


def test_drawdown_near_high_is_capped_when_trend_not_positive() -> None:
    """高値圏区分(+20)もtrend_quality<=0の場合0以下へキャップする
    (コードレビュー対応v3で全正区分へ拡張)。"""
    result = _evaluate(
        _momentum(
            ma20=Decimal("1050"), ma60=Decimal("1100"), ma20_slope_pct=-10.0,
            drawdown_from_recent_high_pct=-1.0,
        ),
        current_price=Decimal("1000"),
    )
    assert result.drawdown_component is not None
    assert result.drawdown_component <= 0.0


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


def test_ma20_slightly_overheat_zone_is_capped_when_trend_not_positive() -> None:
    """やや過熱気味区分(+30)もtrend_quality<=0の場合0以下へキャップする
    (コードレビュー対応v3で全正区分へ拡張)。"""
    result = _evaluate(
        _momentum(ma20=Decimal("934"), ma60=Decimal("1100"), ma20_slope_pct=-10.0),
        current_price=Decimal("1000"),  # dev≈+7.07%(near_high3〜overheat10の間)
    )
    assert result.price_vs_ma20_component is not None
    assert result.price_vs_ma20_component <= 0.0


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


# ===== overheat penalty modifier(コードレビュー対応v3、31-38) =====


def _full_base_momentum(**overrides: object) -> MomentumSnapshot:
    """7 base成分(trend/ma20/ma60/rsi/macd/drawdown/volume)が全て揃った
    momentum(overheatは条件を満たさない中立値)。"""
    base = dict(
        ma20=Decimal("990"),
        ma60=Decimal("950"),
        ma20_slope_pct=5.0,
        rsi=55.0,
        macd=_macd("0.5"),
        drawdown_from_recent_high_pct=-5.0,
        volume_ratio=1.5,
    )
    base.update(overrides)
    return _momentum(**base)  # type: ignore[arg-type]


def test_overheat_unavailable_does_not_raise_score_above_base() -> None:
    """過熱情報欠損時、final_score(score)はbase_scoreと一致する(欠損に
    よってスコアが有利になることを禁止する、コードレビュー対応v3の最重要
    指摘)。"""
    result = _evaluate(
        _full_base_momentum(five_day_return_pct=None), current_price=Decimal("1000")
    )
    assert result.base_score is not None
    assert result.score == result.base_score
    assert result.overheat_penalty_applied is None
    assert result.overheat_penalty_points is None
    assert "OVERHEAT_PENALTY_UNAVAILABLE" in result.reason_codes


def test_overheat_not_triggered_final_equals_base() -> None:
    result = _evaluate(
        _full_base_momentum(five_day_return_pct=3.0), current_price=Decimal("1000")
    )
    assert result.overheat_penalty_applied is False
    assert result.overheat_penalty_points == 0.0
    assert result.score == result.base_score


def test_overheat_triggered_final_lower_than_base() -> None:
    result = _evaluate(
        _full_base_momentum(
            rsi=85.0, drawdown_from_recent_high_pct=-0.5, five_day_return_pct=18.0
        ),
        current_price=Decimal("1000"),
    )
    assert result.overheat_penalty_applied is True
    assert result.overheat_penalty_points == _CONFIG.overheat_penalty_points
    assert result.base_score is not None
    assert result.score is not None
    assert result.score < result.base_score
    assert result.score == pytest.approx(
        max(-100.0, result.base_score - _CONFIG.overheat_penalty_points)
    )


def test_overheat_penalty_result_is_clamped_to_score_range() -> None:
    result = _evaluate(
        _momentum(
            ma20=Decimal("1050"),
            ma60=Decimal("1100"),
            ma20_slope_pct=-10.0,
            rsi=85.0,
            drawdown_from_recent_high_pct=-0.5,
            five_day_return_pct=18.0,
        ),
        current_price=Decimal("1000"),
    )
    assert result.score is not None
    assert -100.0 <= result.score <= 100.0


def test_confidence_capped_at_medium_when_overheat_unavailable() -> None:
    """過熱判定が不能な場合、coverageがHIGH相当でもconfidenceはHIGHへ
    到達しない(コードレビュー対応v3)。"""
    result = _evaluate(
        _full_base_momentum(five_day_return_pct=None), current_price=Decimal("1000")
    )
    assert result.coverage == 1.0  # 7 base成分は全て揃っている
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_confidence_reaches_high_when_overheat_evaluable_and_not_triggered() -> None:
    result = _evaluate(
        _full_base_momentum(five_day_return_pct=3.0), current_price=Decimal("1000")
    )
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH


def test_price_history_misaligned_reason_code_recorded() -> None:
    result = _evaluate(_momentum(rsi=50.0, price_history_aligned=False))
    assert "PRICE_HISTORY_NOT_ALIGNED_WITH_CURRENT_PRICE" in result.reason_codes
    # コードレビュー対応(v4): 「historyが古い(behind)」であることを示す
    # 専用reason codeも併せて記録される。
    assert "PRICE_HISTORY_BEHIND_CURRENT_PRICE" in result.reason_codes


def test_price_history_future_bars_excluded_reason_code_recorded() -> None:
    """未来バーを除外した場合(price_history_has_future_bars=True)、除外後も
    整合している(price_history_aligned=True)ケースであっても専用reason code
    が記録される(コードレビュー対応v4)。"""
    result = _evaluate(
        _momentum(rsi=50.0, price_history_aligned=True, price_history_has_future_bars=True)
    )
    assert "PRICE_HISTORY_FUTURE_BARS_EXCLUDED" in result.reason_codes
    assert "PRICE_HISTORY_NOT_ALIGNED_WITH_CURRENT_PRICE" not in result.reason_codes
    assert "PRICE_HISTORY_BEHIND_CURRENT_PRICE" not in result.reason_codes


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
    """rsiのみ利用可能(coverage=rsi_weight/7成分合計weight)はmin_coverage_
    required(0.3)未満のためNOT_EVALUATED。"""
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
# テストコード削減対応2026-08: 12関数はいずれも`pytest.raises(ValidationError)`
# のみのため、入力ケースを1件も減らさずparametrizeへ統合(旧関数名をidsに使い、
# 失敗時にどの境界値ケースかを即座に特定できるようにする)。


# make_overridesはcallableで渡す(TimingScoreCategoryThresholds自体が
# コンストラクタ時点でscore範囲[-100,100]を検証するため、parametrizeリスト
# 構築時=collection時に評価すると期待するValidationErrorがpytest.raises()の
# 外(collection時)で送出されてしまう。テスト実行時まで評価を遅延させる)。
@pytest.mark.parametrize(
    "make_overrides",
    [
        lambda: {"coverage_medium_threshold": -0.1},
        lambda: {"coverage_high_threshold": 1.1},
        lambda: {
            "category_thresholds": TimingScoreCategoryThresholds(
                strong_tailwind=150.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
            )
        },
        lambda: {
            "category_thresholds": TimingScoreCategoryThresholds(
                strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-150.0
            )
        },
        lambda: {"rsi_neutral_boundary": 80.0, "rsi_sweet_spot_boundary": 60.0},
        lambda: {"drawdown_near_high_pct": 1.0},
        lambda: {"ma20_pullback_low_pct": 5.0, "ma20_near_high_pct": 3.0},
        lambda: {"volume_moderate_low": 3.0, "volume_moderate_high": 2.5},
        lambda: {"min_coverage_required": 0.5, "coverage_medium_threshold": 0.4},
        lambda: {
            "trend_quality_weight": 0.0,
            "price_vs_ma20_weight": 0.0,
            "price_vs_ma60_weight": 0.0,
            "rsi_weight": 0.0,
            "macd_weight": 0.0,
            "drawdown_weight": 0.0,
            "volume_weight": 0.0,
        },
        # drawdown区分境界は同値を許容しない(near_high > pullback > neutral、
        # コードレビュー対応v3で厳格化)。
        lambda: {"drawdown_near_high_pct": -2.0, "drawdown_pullback_pct": -2.0},
        lambda: {"overheat_penalty_points": 0.0},
    ],
    ids=[
        "negative_coverage_medium_threshold",
        "coverage_high_threshold_above_one",
        "category_threshold_above_100",
        "category_threshold_below_negative_100",
        "unordered_rsi_boundaries",
        "positive_drawdown_near_high_pct",
        "unordered_ma20_boundaries",
        "unordered_volume_boundaries",
        "min_coverage_above_medium_threshold",
        "zero_weight_sum",
        "equal_drawdown_boundaries",
        "non_positive_overheat_penalty_points",
    ],
)
def test_config_rejects_invalid_values(
    make_overrides: Callable[[], dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        _config(**make_overrides())


# ===== timing_score_config_valuesの監査情報(コードレビュー対応v4、Low改善) =====


def test_config_values_include_category_thresholds() -> None:
    """categoryがどの閾値で分類されたかを完全に監査できるよう、
    category_thresholds自体もconfig_values_usedへ保存する。"""
    values = timing_score_config_values(_CONFIG)
    assert values["category_thresholds"] == _CONFIG.category_thresholds.model_dump()

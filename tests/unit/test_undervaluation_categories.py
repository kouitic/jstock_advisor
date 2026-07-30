from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.scoring.score import UndervaluationSignals
from jstock_advisor.domain.scoring.undervaluation_categories import (
    score_undervaluation_categories,
)

_CONFIG = load_config().buy_decision.undervaluation_category_caps


def test_no_signals_available_scores_zero() -> None:
    score, formula = score_undervaluation_categories(UndervaluationSignals(), _CONFIG)
    assert score == 0.0
    assert "データがない" in formula


def test_all_signals_true_scores_full_20_points() -> None:
    signals = UndervaluationSignals(
        per_below_median=True,
        pbr_below_median=True,
        dividend_yield_above_historical_average=True,
        drawdown_from_52w_high=True,
        below_fair_value=True,
        price_down_despite_stable_earnings=True,
    )
    score, _ = score_undervaluation_categories(signals, _CONFIG)
    assert score == 20.0


def test_price_drawdown_alone_does_not_max_market_price_action_category() -> None:
    """52週高値からの下落や前日比マイナスだけで高い割安点を付けない(要求仕様15節)。

    market_price_actionカテゴリはdrawdown_from_52w_highとprice_down_despite_
    stable_earningsの2信号で構成される。両方が判定可能で、片方(drawdown)しか
    該当しない場合(1/2件該当)は、カテゴリ上限(2点)の半分(1点)にしかならない。
    """
    signals = UndervaluationSignals(
        drawdown_from_52w_high=True, price_down_despite_stable_earnings=False
    )
    score, _ = score_undervaluation_categories(signals, _CONFIG)
    assert score == 1.0  # market_price_action cap(2.0) * 1/2


def test_financial_health_alone_does_not_dominate_undervaluation_score() -> None:
    """財務健全性は割安度カテゴリの対象外であり、undervaluation側の得点には
    一切寄与しない(スコア設計上、別コンポーネントfinancial_healthで評価される)。
    """
    signals = UndervaluationSignals(per_below_median=True)
    score, _ = score_undervaluation_categories(signals, _CONFIG)
    # valuation_multipleカテゴリ(上限6点)のうち1/1件該当のみ = 6点
    assert score == 6.0


def test_categories_are_independently_capped() -> None:
    # fair_value単独(1/1件該当) = 上限8点そのまま。yield等には影響しない。
    signals = UndervaluationSignals(below_fair_value=True)
    score, _ = score_undervaluation_categories(signals, _CONFIG)
    assert score == 8.0

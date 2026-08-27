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

# --- Issue #22 Phase 3.5(2026-08-28、観測性強化) ---------------------------
# build_undervaluation_category_details()は観測用の判定時点明細であり、
# score_undervaluation_categories()(v1スコア)と同一計算の単一情報源から
# 導出されることを検証する。


def test_details_score_sum_matches_v1_score_for_partial_signals() -> None:
    from jstock_advisor.domain.scoring.undervaluation_categories import (
        build_undervaluation_category_details,
    )

    signals = UndervaluationSignals(
        per_below_median=True,
        pbr_below_median=False,
        drawdown_from_52w_high=True,
        # dividend_yield/below_fair_value/price_down は判定不能(None)
    )
    details = build_undervaluation_category_details(signals, _CONFIG)
    v1_score, _ = score_undervaluation_categories(signals, _CONFIG)

    assert len(details) == 4  # available 0件のカテゴリも必ず含まれる
    assert sum(d.score for d in details) == v1_score


def test_details_include_unavailable_categories_as_not_evaluated() -> None:
    """available 0件のカテゴリはv1のformulaには現れないが、明細には
    state=NOT_EVALUATEDとして必ず残る(「データ無しだった」ことの観測)。
    NOT_APPLICABLEは判定基準が存在しないため生成されない。"""
    from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus
    from jstock_advisor.domain.scoring.undervaluation_categories import (
        build_undervaluation_category_details,
    )

    details = build_undervaluation_category_details(
        UndervaluationSignals(per_below_median=True), _CONFIG
    )
    by_category = {d.category: d for d in details}
    assert by_category["valuation_multiple"].state == EvidenceCoverageStatus.EVALUATED
    assert by_category["yield"].state == EvidenceCoverageStatus.NOT_EVALUATED
    assert by_category["fair_value"].state == EvidenceCoverageStatus.NOT_EVALUATED
    assert by_category["market_price_action"].state == EvidenceCoverageStatus.NOT_EVALUATED
    assert all(d.state != EvidenceCoverageStatus.NOT_APPLICABLE for d in details)
    # signal_resultsはNoneを落とさない(available()と違い判定不能も残す)
    assert by_category["valuation_multiple"].signal_results == {
        "per_below_median": True,
        "pbr_below_median": None,
    }
    assert by_category["valuation_multiple"].signals_available == 1
    assert by_category["valuation_multiple"].signals_defined == 2


def test_details_do_not_change_v1_formula_output() -> None:
    """リファクタ後もv1のformula文字列が従来仕様と完全一致することの回帰。"""
    signals = UndervaluationSignals(
        drawdown_from_52w_high=True, price_down_despite_stable_earnings=False
    )
    score, formula = score_undervaluation_categories(signals, _CONFIG)
    assert score == 1.0
    assert formula == (
        "割安度(カテゴリ別上限点): 株価下落(財務悪化以外の理由に限る):1/2件×2.0点"
    )

"""ウォッチリスト判定サマリ文言(LINE UI第二弾、表示専用、2026-08)のテスト。

到達可能な12パターン(区分理由)+弱い項目0/1/複数(一意)/複数(同率)の4パターン
(補足懸念)を網羅する。いずれも既存判定結果(固定フィクスチャ)を入力として
表示文字列を検証する、純粋関数への単体テストであり、投資判断ロジック自体
(decide_buy_action/compute_score等)は一切呼び出さない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import ScoreWeights
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale, ScoreBreakdown
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.watchlist_judgment_summary_formatter import (
    category_label,
    format_watchlist_line,
)

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)

# 本番config/scoring_weights.yamlと同じ実配点(20/20/20/20/10/5/5)。
_WEIGHTS = ScoreWeights(
    total_yield_attractiveness=20,
    dividend_sustainability=20,
    financial_health=20,
    undervaluation=20,
    shareholder_benefit_value=10,
    earnings_stability=5,
    price_stability=5,
)


def _breakdown(
    total_yield_attractiveness: float = 15,
    dividend_sustainability: float = 15,
    financial_health: float = 15,
    undervaluation: float = 15,
    shareholder_benefit_value: float = 8,
    earnings_stability: float = 4,
    price_stability: float = 4,
) -> ScoreBreakdown:
    return ScoreBreakdown(
        total_yield_attractiveness=total_yield_attractiveness,
        dividend_sustainability=dividend_sustainability,
        financial_health=financial_health,
        undervaluation=undervaluation,
        shareholder_benefit_value=shareholder_benefit_value,
        earnings_stability=earnings_stability,
        price_stability=price_stability,
        total=(
            total_yield_attractiveness
            + dividend_sustainability
            + financial_health
            + undervaluation
            + shareholder_benefit_value
            + earnings_stability
            + price_stability
        ),
    )


def _reason(code: str) -> BuyDecisionReason:
    return BuyDecisionReason(code=code, message="test")


def _recommendation(
    reasons: tuple[BuyDecisionReason, ...],
    score_breakdown: ScoreBreakdown | None = None,
    recommendation_id: str = "rec-1",
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="9432",
        stock_name="銘柄9432",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("3200"),
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        base_buy_action=BuyAction.BUY,
        company_quality_score=60.0,
        purchase_attractiveness_score=50.0,
        score_breakdown=score_breakdown or _breakdown(),
        buy_decision_reasons=reasons,
    )


def _record(
    purchase_category: PurchaseCategory,
    final_buy_action: BuyAction | None,
    recommendation_id: str | None = "rec-1",
) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id="batch-1:9432",
        batch_id="batch-1",
        stock_code="9432",
        evaluated_at=_NOW,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=purchase_category,
        final_buy_action=final_buy_action,
        raw_buy_action=final_buy_action,
        recommendation_id=recommendation_id,
    )


# --- 判定履歴なし / recommendation_idなし(理由データ自体が存在しない) -----------


def test_no_record_shows_no_history() -> None:
    line = format_watchlist_line("NTT", "9432", None, None, _WEIGHTS)
    assert line == "NTT（9432）｜判定履歴なし"


def test_excluded_category_without_recommendation_shows_label_only() -> None:
    record = _record(PurchaseCategory.EXCLUDED, final_buy_action=None, recommendation_id=None)
    line = format_watchlist_line("〇〇", "1234", record, None, _WEIGHTS)
    assert line == "〇〇（1234）｜買い対象外"


def test_data_insufficient_shows_label_only() -> None:
    record = _record(
        PurchaseCategory.DATA_INSUFFICIENT, final_buy_action=None, recommendation_id=None
    )
    line = format_watchlist_line("〇〇", "1234", record, None, _WEIGHTS)
    assert line == "〇〇（1234）｜データ不足"


def test_failed_shows_label_only() -> None:
    record = _record(PurchaseCategory.FAILED, final_buy_action=None, recommendation_id=None)
    line = format_watchlist_line("〇〇", "1234", record, None, _WEIGHTS)
    assert line == "〇〇（1234）｜処理失敗"


# --- 到達可能な12パターン(区分理由) --------------------------------------------


def test_price_tier_strong_buy() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.STRONG_BUY)
    rec = _recommendation(reasons)
    assert category_label(record.purchase_category) == "買い候補"
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い候補｜現在値が積極買付価格以内" in line


def test_price_tier_buy() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い候補｜現在値が標準買付価格以内" in line


def test_price_tier_small_entry() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.SMALL_ENTRY)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い候補｜現在値が打診買付価格以内" in line


def test_price_tier_watch_for_price() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.WATCH_FOR_PRICE, BuyAction.WATCH_FOR_PRICE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い待ち｜現在値が買付価格を上回る" in line


def test_no_valuation_anchor() -> None:
    reasons = (_reason("NO_VALUATION_ANCHOR"),)
    record = _record(PurchaseCategory.WATCH_FOR_PRICE, BuyAction.WATCH_FOR_PRICE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い待ち｜適正価格を算出できず" in line


def test_score_below_threshold_final_buy() -> None:
    """買い候補まま残る場合、「総合スコアが基準に届かず」という矛盾表現は
    出さず、価格帯ベースの文言を使う。"""
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い候補｜現在値が標準買付価格以内" in line
    assert "基準に届かず" not in line
    assert "総合スコア" not in line


def test_score_below_threshold_final_small_entry() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.SMALL_ENTRY)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い候補｜現在値が打診買付価格以内" in line


def test_score_below_threshold_final_watch_for_price() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.WATCH_FOR_PRICE, BuyAction.WATCH_FOR_PRICE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い待ち｜総合評価により買付を見送り" in line


def test_score_below_threshold_final_not_attractive() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.NOT_ATTRACTIVE, BuyAction.NOT_ATTRACTIVE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("〇〇", "1234", record, rec, _WEIGHTS)
    assert "買い対象外｜総合評価が購入基準を下回る" in line


def test_earnings_window() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("EARNINGS_WINDOW"))
    record = _record(PurchaseCategory.WATCH_BEFORE_EARNINGS, BuyAction.WATCH_BEFORE_EARNINGS)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い待ち｜次回決算が近いため保留" in line


def test_buy_price_reliability_low() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("BUY_PRICE_RELIABILITY_LOW"))
    record = _record(PurchaseCategory.WATCH_FOR_PRICE, BuyAction.WATCH_FOR_PRICE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "買い待ち｜価格算出の信頼度が低い" in line


def test_valuation_dispersion_too_high() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("VALUATION_DISPERSION_TOO_HIGH"))
    record = _record(PurchaseCategory.MANUAL_REVIEW, BuyAction.MANUAL_REVIEW)
    rec = _recommendation(reasons)
    line = format_watchlist_line("〇〇", "1234", record, rec, _WEIGHTS)
    assert "要確認｜評価手法間のばらつきが大きい" in line


def test_near_buy_category_label_with_price_tier_reason() -> None:
    """買い間近はWATCH_FOR_PRICEの一種であり、区分理由の文言自体は
    買い待ちと共通(カテゴリーラベルのみ異なる)。"""
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.NEAR_BUY, BuyAction.WATCH_FOR_PRICE)
    rec = _recommendation(reasons)
    line = format_watchlist_line("明治HD", "2269", record, rec, _WEIGHTS)
    assert "買い間近｜現在値が買付価格を上回る" in line


# --- 補足懸念: 弱い項目0件/1件/複数(一意)/複数(同率) ------------------------------


def test_supplementary_concern_none_when_no_weak_item_and_no_score_below_threshold() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # 全項目が0.3以上(弱い項目なし)。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(15, 15, 15, 15, 8, 4, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert line == "NTT（9432）｜買い候補｜現在値が標準買付価格以内"


def test_supplementary_concern_single_weak_item() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # financial_health = 5/20 = 0.25 < 0.3 のみ弱い。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(15, 15, 5, 15, 8, 4, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert line == "NTT（9432）｜買い候補｜現在値が標準買付価格以内、財務健全性に懸念"


def test_supplementary_concern_multiple_weak_items_unique_max() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # dividend_sustainability: 2/20=0.1(不足18、最大) / shareholder: 1/10=0.1(不足9)
    # / earnings: 0.5/5=0.1(不足4.5)。いずれも弱いが不足点は配当持続性が最大。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(19, 2, 10, 15, 1, 0.5, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert line.endswith("、配当持続性に懸念")


def test_supplementary_concern_multiple_weak_items_tied_max_lists_all() -> None:
    reasons = (_reason("PRICE_TIER"),)
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # dividend_sustainability・financial_healthともに0点(不足20点で同率最大)。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(15, 0, 0, 15, 8, 4, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert line.endswith("、配当持続性・財務健全性に懸念")
    assert "総合評価に一部懸念あり" not in line


def test_supplementary_concern_no_weak_item_but_score_below_threshold_shows_relative_wording() -> (
    None
):
    """0.3基準の「弱い項目」は無いが、SCORE_BELOW_THRESHOLDが発生している場合、
    抽象表現へフォールバックせず、不足点最大の具体項目を「相対的に低め」という
    非断定的な表現で示す。"""
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # undervaluation: 8/20=0.4(不足12、最大) 他は0.3以上で弱くない。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(15, 15, 15, 8, 7, 4, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert line == (
        "NTT（9432）｜買い候補｜現在値が標準買付価格以内、評価内訳では割安度が相対的に低め"
    )
    assert "総合評価が基準未満" not in line
    assert "主な弱点" not in line
    assert "一部懸念あり" not in line


def test_supplementary_concern_no_weak_item_but_score_below_threshold_tied_lists_all() -> None:
    reasons = (_reason("PRICE_TIER"), _reason("SCORE_BELOW_THRESHOLD"))
    record = _record(PurchaseCategory.BUY_CANDIDATE, BuyAction.BUY)
    # total_yield_attractiveness・dividend_sustainabilityがともに不足5点で同率最大
    # (financial_health/undervaluationは不足3点、いずれも0.3以上のため
    # 「弱い項目」には該当しない)。
    rec = _recommendation(
        reasons,
        score_breakdown=_breakdown(15, 15, 17, 17, 7, 4, 4),
    )
    line = format_watchlist_line("NTT", "9432", record, rec, _WEIGHTS)
    assert "評価内訳では総合利回りの魅力度・配当持続性が相対的に低め" in line

"""銘柄分析(Phase 2-B、LINE表示専用、2026-08)のテスト。

文言設計ルール(断定禁止・懸念なし省略・SHADOW非表示・数量非合成・保存事実の
範囲限定)が実際の出力に反映されていることを検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale, ScoreBreakdown
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    EarningsDateStatus,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding_evaluation_record import (
    HoldingEvaluationRecord,
    build_holding_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.stock_analysis_view_service import StockAnalysisViewService

_NOW = dt.datetime(2026, 8, 25, 7, 0, tzinfo=dt.UTC)
_HOLDING_ID = "本人#8306"


def _service(store_dir: Path) -> StockAnalysisViewService:
    return StockAnalysisViewService(
        evaluation_record_repository=BuyCandidateEvaluationRecordRepository(store_dir=store_dir),
        latest_batch_pointer_repository=LatestBuyCandidateBatchPointerRepository(
            store_dir=store_dir
        ),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        holding_evaluation_record_repository=HoldingEvaluationRecordRepository(
            store_dir=store_dir
        ),
        audit_log_repository=AuditLogRepository(store_dir=store_dir),
    )


def _seed_batch(store_dir: Path, batch_id: str, stock_code: str) -> None:
    LatestBuyCandidateBatchPointerRepository(store_dir=store_dir).update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id=batch_id, completed_at=_NOW, total_candidates=1
        )
    )


def _save_buy_recommendation(store_dir: Path, **overrides) -> Recommendation:
    defaults: dict = dict(
        recommendation_id="rec-1",
        stock_code="8306",
        stock_name="x",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH_BUY,
        price_at_recommendation=Decimal("150"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1",
        buy_action=BuyAction.BUY,
        raw_buy_action=BuyAction.BUY,
        company_quality_score=62.77,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("160"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("155"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("145"), rationale="x"),
        ),
        buy_decision_reasons=(
            BuyDecisionReason(
                code="PRICE_TIER", message="x", actual_value=Decimal("150"), threshold_value=None
            ),
        ),
    )
    defaults.update(overrides)
    rec = Recommendation(**defaults)
    RecommendationRepository(store_dir=store_dir).save(rec)
    return rec


def _save_eval_record(
    store_dir: Path,
    batch_id: str,
    stock_code: str,
    *,
    purchase_category: PurchaseCategory,
    final_buy_action: BuyAction | None,
    recommendation_id: str | None,
    exclusion_reasons: tuple[str, ...] | None = None,
) -> None:
    BuyCandidateEvaluationRecordRepository(store_dir=store_dir).upsert(
        BuyCandidateEvaluationRecord(
            evaluation_id=f"{batch_id}:{stock_code}",
            batch_id=batch_id,
            stock_code=stock_code,
            evaluated_at=_NOW,
            rule_version="v1",
            candidate_source=CandidateSource.WATCHLIST,
            purchase_category=purchase_category,
            final_buy_action=final_buy_action,
            raw_buy_action=final_buy_action,
            recommendation_id=recommendation_id,
            exclusion_reasons=exclusion_reasons,
        )
    )


# --- BUY側 -------------------------------------------------------------


def test_buy_price_tier_shows_exact_prices(tmp_path: Path) -> None:
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(tmp_path)
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "買い候補" in text
    assert "標準買付価格155円以内でした" in text
    assert "積極買付：145円以下" in text
    assert "打診買付：160円以下" in text


def test_buy_facts_section_shows_yields_and_per_pbr_from_snapshot(tmp_path: Path) -> None:
    """追加調査(2026-08)対応: BUYスナップショット拡張後、判断根拠となった事実
    セクションに企業魅力度スコア・利回り・PER/PBR(自社過去中央値付き)が
    実際に表示されること。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        dividend_yield_pct_at_recommendation=3.2,
        shareholder_benefit_yield_pct_at_recommendation=1.0,
        total_yield_pct_at_recommendation=4.2,
        buy_score_input_facts={
            "current_per": "9.8",
            "current_pbr": "0.82",
            "historical_per_median": "12.3",
            "historical_pbr_median": "1.05",
        },
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "■ 判断根拠となった事実" in text
    assert "配当利回り：3.20%" in text
    assert "優待利回り：1.00%" in text
    assert "総合利回り：4.20%" in text
    assert "PER：9.8倍（自社の過去中央値12.3倍）" in text
    assert "PBR：0.82倍（自社の過去中央値1.05倍）" in text


def test_buy_facts_section_shows_remaining_score_component_inputs(tmp_path: Path) -> None:
    """レビュー対応(2026-08、修正条件1): 事実セクションに、割安度・PER/PBR
    以外のスコア項目(配当持続性・財務健全性・業績安定性・株価安定性)が実際に
    使用した判定時点入力も表示されること(解釈セクションでの説明の土台となる
    生の事実)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        buy_score_input_facts={
            "equity_ratio_pct": 55.5,
            "payout_ratio_pct": 25.0,
            "consecutive_dividend_increase_years": 4,
            "is_progressive_or_doe_policy": True,
            "operating_income_non_decrease_ratio": 0.33,
            "annualized_volatility_pct": 38.5,
        },
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "自己資本比率：55.5%" in text
    assert "配当性向：25.0%" in text
    assert "連続増配年数：4年" in text
    assert "累進配当/DOE方針：あり" in text
    assert "営業利益が前期比で悪化しなかった割合：33%" in text
    assert "年率換算ボラティリティ：38.5%" in text


def test_buy_interpretation_covers_remaining_score_components(tmp_path: Path) -> None:
    """レビュー対応(2026-08、修正条件1): 割安度・財務健全性以外の残りの
    スコア項目(総合利回り・配当持続性・株主優待価値・業績安定性・株価安定性)
    についても、保存済みの判定時点事実からスコアへの寄与を説明する文章に
    なっていること。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        score_breakdown=ScoreBreakdown(
            total_yield_attractiveness=18.0,
            dividend_sustainability=16.0,
            financial_health=12.0,
            undervaluation=12.0,
            shareholder_benefit_value=8.0,
            earnings_stability=1.0,
            price_stability=1.0,
            total=68.0,
        ),
        config_values_used={
            "scoring_weights": {
                "total_yield_attractiveness": 20,
                "dividend_sustainability": 20,
                "financial_health": 20,
                "undervaluation": 20,
                "shareholder_benefit_value": 10,
                "earnings_stability": 5,
                "price_stability": 5,
            }
        },
        buy_score_input_facts={
            "total_yield_pct": 5.5,
            "is_progressive_or_doe_policy": True,
            "consecutive_dividend_increase_years": 4,
            "payout_ratio_pct": 25.0,
            "benefit_yield_pct": 1.8,
            "operating_income_non_decrease_ratio": 0.33,
            "annualized_volatility_pct": 38.5,
        },
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert (
        "総合利回り(配当+優待)は5.50%です、総合利回りの魅力度評価のプラス要因となっています。"
        in text
    )
    assert "総合利回りの魅力度は18.0/20点です。" in text
    assert (
        "累進配当/DOE方針を採用、連続増配4年、配当性向25.0%、配当持続性評価の"
        "プラス要因となっています。" in text
    )
    assert "配当持続性は16.0/20点です。" in text
    assert "株主優待利回りは1.80%です、株主優待価値評価のプラス要因となっています。" in text
    assert "株主優待価値は8.0/10点です。" in text
    assert (
        "四半期営業利益が前期比で悪化しなかった期間の割合は33%です、業績安定性評価の"
        "注意材料となっています。" in text
    )
    assert "業績安定性は1.0/5点です。" in text
    assert "年率換算ボラティリティは38.5%です、株価安定性評価の注意材料となっています。" in text
    assert "株価安定性は1.0/5点です。" in text


def test_undervaluation_fact_clause_true_and_false_wording_for_all_signals() -> None:
    """レビュー対応(2026-08、事実反転バグ修正): UndervaluationSignalsの6項目
    すべてについて、True/Falseそれぞれの文言が正しい(反転していない)ことを
    直接検証する(必須テスト1・2・5)。"""
    from jstock_advisor.services.stock_analysis_view_service import _component_fact_clause

    expected = {
        "per_below_median": (
            "PERが自社の過去中央値を下回っている",
            "PERは自社の過去中央値を下回っていない",
        ),
        "pbr_below_median": (
            "PBRが自社の過去中央値を下回っている",
            "PBRは自社の過去中央値を下回っていない",
        ),
        "dividend_yield_above_historical_average": (
            "配当利回りが自社の過去平均を上回っている",
            "配当利回りは自社の過去平均を上回っていない",
        ),
        "drawdown_from_52w_high": (
            "52週高値から一定以上下落している",
            "52週高値から一定以上の下落には該当していない",
        ),
        "below_fair_value": (
            "現在値が算出された適正価格を下回っている",
            "現在値は算出された適正価格を下回っていない",
        ),
        "price_down_despite_stable_earnings": (
            "業績は安定している一方で株価が下落している",
            "「業績安定下の株価下落」条件には該当していない",
        ),
    }
    for signal_name, (true_wording, false_wording) in expected.items():
        true_facts = {"undervaluation_signals": {signal_name: True}}
        false_facts = {"undervaluation_signals": {signal_name: False}}

        # True値はプラス材料側(is_positive=True)でのみ、True用の文言になる。
        assert _component_fact_clause("undervaluation", true_facts, True) == true_wording
        # True値は注意材料側(is_positive=False)では抽出対象外(該当なし)。
        assert _component_fact_clause("undervaluation", true_facts, False) is None

        # False値は注意材料側(is_positive=False)でのみ、False用の文言になる。
        assert _component_fact_clause("undervaluation", false_facts, False) == false_wording
        # False値はプラス材料側(is_positive=True)では抽出対象外(該当なし)。
        assert _component_fact_clause("undervaluation", false_facts, True) is None


def test_undervaluation_fact_clause_none_or_unset_generates_no_text() -> None:
    """必須テスト4: None/未保存のシグナルについては文言を生成しない。"""
    from jstock_advisor.services.stock_analysis_view_service import _component_fact_clause

    # シグナル自体が保存されていない(キー無し)。
    assert _component_fact_clause("undervaluation", {}, True) is None
    assert _component_fact_clause("undervaluation", {}, False) is None
    # シグナルはあるが値がNone(判定不能で保存対象外だった項目)。
    facts_with_none = {"undervaluation_signals": {"per_below_median": None}}
    assert _component_fact_clause("undervaluation", facts_with_none, True) is None
    assert _component_fact_clause("undervaluation", facts_with_none, False) is None


def test_buy_interpretation_undervaluation_negative_uses_false_wording_not_reversed(
    tmp_path: Path,
) -> None:
    """必須テスト2・3: per_below_median=Falseのまま割安度が注意材料(スコア比が
    低い)側に回った場合、保存済み事実と逆の「下回っている」ではなく、正しく
    「下回っていない」と表示されること(実際に報告された事実反転バグの回帰
    テスト)。プラス材料側の他項目(総合利回り)は別途正しく分離されること。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        score_breakdown=ScoreBreakdown(
            total_yield_attractiveness=18.0,
            dividend_sustainability=10.0,
            financial_health=10.0,
            undervaluation=2.0,
            shareholder_benefit_value=5.0,
            earnings_stability=2.5,
            price_stability=2.5,
            total=50.0,
        ),
        config_values_used={
            "scoring_weights": {
                "total_yield_attractiveness": 20,
                "dividend_sustainability": 20,
                "financial_health": 20,
                "undervaluation": 20,
                "shareholder_benefit_value": 10,
                "earnings_stability": 5,
                "price_stability": 5,
            }
        },
        buy_score_input_facts={
            "total_yield_pct": 5.5,
            "undervaluation_signals": {
                "per_below_median": False,
                "pbr_below_median": False,
            },
        },
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    # 修正後: 保存済みFalse事実どおりの文言。
    assert (
        "PERは自社の過去中央値を下回っていない、PBRは自社の過去中央値を下回っていない、"
        "割安度評価の注意材料となっています。" in text
    )
    # バグ再発防止: True用の文言(意味が逆)が注意材料として出てはならない。
    assert (
        "PERが自社の過去中央値を下回っている、PBRが自社の過去中央値を下回っている、割安度"
        not in text
    )
    # プラス材料側(総合利回り)は正しく分離されていること。
    assert "主なプラス材料" in text
    assert (
        "総合利回り(配当+優待)は5.50%です、総合利回りの魅力度評価のプラス要因となっています。"
        in text
    )
    assert "注意材料" in text


def test_buy_interpretation_ranks_score_components_not_raw_per(tmp_path: Path) -> None:
    """追加調査(2026-08)対応・レビュー対応(2026-08、修正条件1): 「解釈」は
    PER単体の絶対値から独自に割安と断定せず、既存score_breakdownを配点比で
    ランキングしたうえで、保存済みのUndervaluationSignals等の判定時点事実が
    そのスコアへどう寄与したかを自然文で説明する。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        score_breakdown=ScoreBreakdown(
            total_yield_attractiveness=18.0,
            dividend_sustainability=10.0,
            financial_health=1.0,
            undervaluation=16.0,
            shareholder_benefit_value=8.0,
            earnings_stability=4.0,
            price_stability=4.0,
            total=61.0,
        ),
        config_values_used={
            "scoring_weights": {
                "total_yield_attractiveness": 20,
                "dividend_sustainability": 20,
                "financial_health": 20,
                "undervaluation": 20,
                "shareholder_benefit_value": 10,
                "earnings_stability": 5,
                "price_stability": 5,
            }
        },
        buy_score_input_facts={
            "undervaluation_signals": {
                "per_below_median": True,
                "pbr_below_median": True,
            },
        },
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "■ 解釈" in text
    assert "主なプラス材料" in text
    assert (
        "PERが自社の過去中央値を下回っている、PBRが自社の過去中央値を下回っている、"
        "割安度評価のプラス要因となっています。" in text
    )
    assert "割安度は16.0/20点です。" in text
    assert "注意材料" in text
    assert "自己資本比率のデータがありません、財務健全性評価の注意材料となっています。" in text
    assert "財務健全性は1.0/20点です。" in text
    # PER/PBRの実数値から表示層で独自に「割安」と断定していないこと。
    assert "PERが低い" not in text
    assert "だから割安" not in text


def test_score_below_threshold_shows_exact_numbers_when_snapshot_present(
    tmp_path: Path,
) -> None:
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        raw_buy_action=BuyAction.STRONG_BUY,
        buy_action=BuyAction.BUY,
        company_quality_score=62.77,
        config_values_used={"score_thresholds": {"strong_buy": 70.0, "buy": 60.0}},
        buy_decision_reasons=(
            BuyDecisionReason(
                code="SCORE_BELOW_THRESHOLD",
                message="x",
                actual_value=62.77,
                threshold_value=45.0,
            ),
        ),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    # 定性語(「わずかに」「大きく」)ではなく、実数値で表示する(修正10)。
    assert "62.77点" in text
    assert "70.0点" in text
    assert "わずかに" not in text
    assert "大きく" not in text


def test_score_below_threshold_watch_for_price_shows_exact_numbers_when_snapshot_present(
    tmp_path: Path,
) -> None:
    """本番実データUAT(2026-08)で発覚した修正条件A: raw_buy_action=
    WATCH_FOR_PRICE(価格条件は打診買付水準を満たしていたが企業魅力度スコアで
    さらにNOT_ATTRACTIVEへ格下げされたケース)でも、score_thresholds
    スナップショットにwatch閾値が実際に保存されていれば、実数値で説明する
    (以前は_TIER_THRESHOLD_FIELDにWATCH_FOR_PRICEが無く、スナップショットが
    実在するにもかかわらず「スナップショットが無いため実数値は表示できない」
    という事実に反する文言が表示されていた)。"""
    _seed_batch(tmp_path, "batch-1", "6653")
    _save_buy_recommendation(
        tmp_path,
        recommendation_id="rec-6653",
        stock_code="6653",
        raw_buy_action=BuyAction.WATCH_FOR_PRICE,
        buy_action=BuyAction.NOT_ATTRACTIVE,
        company_quality_score=29.84,
        config_values_used={
            "score_thresholds": {
                "strong_buy": 70.0,
                "buy": 60.0,
                "small_entry": 55.0,
                "watch": 45.0,
            }
        },
        buy_decision_reasons=(
            BuyDecisionReason(
                code="PRICE_TIER", message="x", actual_value=Decimal("2177.0"), threshold_value=None
            ),
            BuyDecisionReason(
                code="SCORE_BELOW_THRESHOLD", message="x", actual_value=29.84, threshold_value=45.0
            ),
        ),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "6653",
        purchase_category=PurchaseCategory.NOT_ATTRACTIVE,
        final_buy_action=BuyAction.NOT_ATTRACTIVE,
        recommendation_id="rec-6653",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("6653")

    assert "29.84点" in text
    assert "45.0点" in text
    assert "スナップショットが無いため" not in text


def test_score_below_threshold_watch_for_price_falls_back_when_snapshot_missing(
    tmp_path: Path,
) -> None:
    """修正条件B: 同じraw_buy_action=WATCH_FOR_PRICE+SCORE_BELOW_THRESHOLDでも、
    score_thresholdsスナップショット自体が保存されていない旧データでは、
    実数値を捏造せず非断定のフォールバック文言のままであること。"""
    _seed_batch(tmp_path, "batch-1", "6653")
    _save_buy_recommendation(
        tmp_path,
        recommendation_id="rec-6653",
        stock_code="6653",
        raw_buy_action=BuyAction.WATCH_FOR_PRICE,
        buy_action=BuyAction.NOT_ATTRACTIVE,
        company_quality_score=29.84,
        config_values_used={},
        buy_decision_reasons=(
            BuyDecisionReason(
                code="PRICE_TIER", message="x", actual_value=Decimal("2177.0"), threshold_value=None
            ),
            BuyDecisionReason(
                code="SCORE_BELOW_THRESHOLD", message="x", actual_value=29.84, threshold_value=45.0
            ),
        ),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "6653",
        purchase_category=PurchaseCategory.NOT_ATTRACTIVE,
        final_buy_action=BuyAction.NOT_ATTRACTIVE,
        recommendation_id="rec-6653",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("6653")

    assert "スナップショットが無いため" in text
    assert "45.0点" not in text


def test_score_below_threshold_falls_back_without_fabricating_numbers(
    tmp_path: Path,
) -> None:
    """判定時点のscore_thresholdsスナップショットが無い過去データでは、
    実数値を捏造せず非断定の代替文言に留める。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        raw_buy_action=BuyAction.STRONG_BUY,
        buy_action=BuyAction.BUY,
        company_quality_score=62.77,
        config_values_used={},
        buy_decision_reasons=(
            BuyDecisionReason(
                code="SCORE_BELOW_THRESHOLD", message="x", actual_value=62.77, threshold_value=45.0
            ),
        ),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    # 企業魅力度スコア自体は既存Recommendationに保存済みの事実であり、
    # 「判断根拠となった事実」セクションに表示してよい(捏造ではない)。
    assert "企業魅力度スコア：62.77点" in text
    # 一方、格下げの「解釈・総合判断」側では、判定時点の閾値スナップショットが
    # 無い場合に実数値の比較(例:「基準（70.0点）」)を捏造してはならない。
    assert "の基準（" not in text
    assert "スナップショットが無いため" in text


def test_no_valuation_anchor_shows_per_method_exclusion_reasons_when_stored(
    tmp_path: Path,
) -> None:
    """本番実データUAT横断確認(2026-08)で発覚した同種バグ: NO_VALUATION_ANCHOR
    は、recommendation.valuation_methods(既存フィールド)に各方式の実際の
    除外理由(exclusion_reason)が保存されているにもかかわらず、一律
    「具体的な要因は現行データからは区別できません」と表示していた。
    保存済みの除外理由をそのまま表示するよう修正した(表示層で新しい
    除外理由を推測・算出しない)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        buy_decision_reasons=(
            BuyDecisionReason(
                code="NO_VALUATION_ANCHOR", message="x", actual_value=None, threshold_value=None
            ),
        ),
        valuation_methods=(
            FairValueMethodResult(
                method="per",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason="現在値の10%未満のため外れ値として除外",
            ),
            FairValueMethodResult(
                method="dcf",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason="単年度キャッシュフローの歪みにより算出不能",
            ),
        ),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.NOT_ATTRACTIVE,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "PER法: 現在値の10%未満のため外れ値として除外" in text
    assert "DCF法: 単年度キャッシュフローの歪みにより算出不能" in text
    assert "現行データからは区別できません" not in text


def test_no_valuation_anchor_falls_back_without_fabricating_when_no_reasons_stored(
    tmp_path: Path,
) -> None:
    """valuation_methodsに除外理由が一件も保存されていない(旧データ等)場合は、
    従来どおり非断定のフォールバック文言のままであること(捏造しない)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_buy_recommendation(
        tmp_path,
        buy_decision_reasons=(
            BuyDecisionReason(
                code="NO_VALUATION_ANCHOR", message="x", actual_value=None, threshold_value=None
            ),
        ),
        valuation_methods=(),
    )
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.NOT_ATTRACTIVE,
        recommendation_id="rec-1",
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "現行データからは区別できません" in text
    assert "PER法" not in text


def _save_reliability_low_recommendation(tmp_path: Path, **overrides) -> None:
    defaults: dict = dict(
        buy_decision_reasons=(
            BuyDecisionReason(
                code="BUY_PRICE_RELIABILITY_LOW",
                message="x",
                actual_value=None,
                threshold_value=None,
            ),
        ),
    )
    defaults.update(overrides)
    _save_buy_recommendation(tmp_path, **defaults)
    _save_eval_record(
        tmp_path,
        "batch-1",
        "8306",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.WATCH_FOR_PRICE,
        recommendation_id="rec-1",
    )


def test_reliability_low_entry_margin_exceeds_cap_shows_actual_and_cap_values(
    tmp_path: Path,
) -> None:
    """必須テストC: ENTRY_MARGIN_EXCEEDS_CAPについて、保存された判定時点の
    実数値(entry_margin_before_cap)と判定時点上限(config_values_used
    ["maximum_entry_margin"])が正しく%表示されること。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["ENTRY_MARGIN_EXCEEDS_CAP"],
            "entry_margin_before_cap": "0.42",
        },
        config_values_used={"maximum_entry_margin": 0.30},
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "安全余裕率が上限を超過" in text
    assert "判定時の安全余裕率42.0%" in text
    assert "上限30.0%" in text


def test_reliability_low_high_valuation_dispersion_shows_actual_and_threshold(
    tmp_path: Path,
) -> None:
    """必須テストD: HIGH_VALUATION_DISPERSIONについて、既存フィールド
    valuation_dispersion_ratioと判定時点threshold(config_values_used
    ["valuation_dispersion_medium_max"])が正しく表示されること。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        valuation_dispersion_ratio=Decimal("1.75"),
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["HIGH_VALUATION_DISPERSION"],
        },
        config_values_used={"valuation_dispersion_medium_max": 1.60},
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "適正価格のばらつきが大きい" in text
    assert "判定時のばらつき1.75倍" in text
    assert "基準1.60倍以下" in text


def test_reliability_low_too_few_valuation_methods_uses_stored_concern_not_rejudged(
    tmp_path: Path,
) -> None:
    """必須テストE: TOO_FEW_VALUATION_METHODSは保存されたconcernを発火根拠
    とし、表示層では再判定しないこと。件数は判定時と同一条件で保存された
    valuation_methods_used_countのみを使う(valuation_methodsから独自に
    数え直さない)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["TOO_FEW_VALUATION_METHODS"],
            "valuation_methods_used_count": 2,
        },
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "適正価格の算出に使えた手法が少ない" in text
    assert "判定時に使用できた手法2件" in text
    assert "2件以下が対象" in text


def test_reliability_low_data_quality_earnings_outlier_and_blocking_reason(
    tmp_path: Path,
) -> None:
    """必須テストF: DATA_QUALITY_WARNING・STALE_EARNINGS_DATE・
    VALUATION_OUTLIER_EXCLUDED・outlier_filter_blocking_reason(TOO_FEW_
    METHODS_AFTER_OUTLIER_FILTER)について、それぞれ保存済み事実の範囲で
    正しく表示されること(このテストは複数concern同時発生時に保存順で
    全件表示されること(必須テストG)も兼ねる)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        business_days_to_earnings=None,
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        # レビュー対応(2026-08、commit f546473再レビュー): valuation_methods
        # (外れ値フィルタ適用「前」のオブジェクト)に、外れ値とは無関係な
        # 通常のexclusion_reason(算出不能)を持つ方式を混ぜておき、これが
        # VALUATION_OUTLIER_EXCLUDEDの理由として誤って表示されないことを
        # あわせて確認する(必須テスト3)。
        valuation_methods=(
            FairValueMethodResult(
                method="dcf",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason="EPSが負数のため算出不可",
            ),
        ),
        buy_score_input_facts={
            "buy_price_reliability_concerns": [
                "DATA_QUALITY_WARNING",
                "STALE_EARNINGS_DATE",
                "VALUATION_OUTLIER_EXCLUDED",
                "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER",
            ],
            "data_age_business_days": 3,
            # 実際に外れ値フィルタで除外された方式・理由の判定時点スナップ
            # ショット(dcfの「算出不能」とは別の、per方式の外れ値除外)。
            "valuation_outlier_exclusions": [
                {
                    "method": "per",
                    "code": "BELOW_CURRENT_PRICE_THRESHOLD",
                    "message": "現在値の10%未満のため外れ値として除外",
                    "actual_value": "50",
                    "reference_value": "500",
                }
            ],
        },
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "データ品質に懸念がある" in text
    assert "取得データの経過日数3営業日" in text
    assert "次回決算予定日が未確定" in text
    assert "次回決算予定日の情報が古い" in text
    assert "判定時のステータス: STALE_PAST_DATE" in text
    assert "適正価格の算出方式に外れ値が含まれていた" in text
    assert "PER法: 現在値の10%未満のため外れ値として除外" in text
    assert "外れ値除外の結果、比較に使える手法が不足したため除外前の結果へ戻した" in text

    # 必須テスト3: 算出不能(dcf、外れ値とは無関係)がVALUATION_OUTLIER_EXCLUDED
    # の理由として混在していないこと。
    assert "EPSが負数のため算出不可" not in text
    assert "DCF法" not in text

    # G: 保存順(concernsリストの並び)のまま表示されること。
    order = [
        text.index("データ品質に懸念がある"),
        text.index("次回決算予定日の情報が古い"),
        text.index("適正価格の算出方式に外れ値が含まれていた"),
        text.index("外れ値除外の結果"),
    ]
    assert order == sorted(order)


def test_reliability_low_outlier_excluded_shows_only_actually_excluded_methods(
    tmp_path: Path,
) -> None:
    """レビュー対応(2026-08、commit f546473再レビュー、必須テスト2): 実際に
    外れ値フィルタで除外された方式(valuation_outlier_exclusions)だけを
    表示し、他の理由(算出不能・業種モデル未実装等でvaluation_methodsに
    保存されている通常のexclusion_reason)は一切混在させないことを、
    VALUATION_OUTLIER_EXCLUDED単独のケースで確認する。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        valuation_methods=(
            FairValueMethodResult(
                method="historical_range",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason="価格レンジ算出に必要な履歴データが不足",
            ),
        ),
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["VALUATION_OUTLIER_EXCLUDED"],
            "valuation_outlier_exclusions": [
                {
                    "method": "dcf",
                    "code": "EXTREME_HIGH_RELATIVE_TO_MEDIAN",
                    "message": "算出値が他方式の中央値を大きく上回るため外れ値として除外",
                    "actual_value": "5000",
                    "reference_value": "1500",
                }
            ],
        },
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "適正価格の算出方式に外れ値が含まれていた" in text
    assert "DCF法: 算出値が他方式の中央値を大きく上回るため外れ値として除外" in text
    assert "価格レンジ算出に必要な履歴データが不足" not in text
    assert "価格レンジ法" not in text


def test_reliability_low_outlier_excluded_shows_label_only_for_old_data(
    tmp_path: Path,
) -> None:
    """必須テスト4: VALUATION_OUTLIER_EXCLUDEDが発火しているが、詳細
    スナップショット(valuation_outlier_exclusions)自体が保存されていない
    旧データでは、concernラベルのみを表示し、valuation_methodsの通常
    exclusion_reasonを外れ値理由として流用しないこと。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        valuation_methods=(
            FairValueMethodResult(
                method="per",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason="PERが算出できないため除外",
            ),
        ),
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["VALUATION_OUTLIER_EXCLUDED"],
            # valuation_outlier_exclusionsキー自体が存在しない(旧データ)。
        },
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "適正価格の算出方式に外れ値が含まれていた" in text
    assert "PERが算出できないため除外" not in text
    assert "PER法" not in text


def test_reliability_low_falls_back_when_concerns_not_stored(tmp_path: Path) -> None:
    """必須テストH: buy_price_reliability_concernsが保存されていない
    (旧レコード)場合は、従来どおり非断定のフォールバック文言のままである
    こと(concernを推測・再計算して補完しない)。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(tmp_path, buy_score_input_facts={})
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "信頼性低下の具体的な要因は、現行データからは一意に特定できません" in text


def test_reliability_low_uses_stored_snapshot_not_current_config(tmp_path: Path) -> None:
    """必須テストI: 判定時snapshotと(実際に想定される)現在configを意図的に
    異なる値にし、表示が保存済みsnapshotの値を使うこと(現在configを
    参照していないこと)を確認する。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        buy_score_input_facts={
            "buy_price_reliability_concerns": ["ENTRY_MARGIN_EXCEEDS_CAP"],
            "entry_margin_before_cap": "0.99",
        },
        # 現実の設定ファイル(config/buy_decision_rules.yaml)のmaximum_margin.
        # entry(0.30)とは意図的に異なる値を保存し、この非現実的な値がそのまま
        # 表示されること(=現在configの0.30が紛れ込んでいないこと)を検証する。
        config_values_used={"maximum_entry_margin": 0.77},
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    assert "判定時の安全余裕率99.0%" in text
    assert "上限77.0%" in text
    assert "30.0%" not in text


def test_reliability_low_does_not_reverse_or_invent_concern_from_contradicting_facts(
    tmp_path: Path,
) -> None:
    """必須テストJ: 保存されたconcernと補足事実が意図的に矛盾するテスト
    データを用意し、「発火したかどうか」はconcernsを正として扱い、表示層が
    補足事実から独自に発火判定を追加・反転させないことを確認する。ここでは
    concernsに一切何も含めていないにもかかわらず、HIGH_VALUATION_DISPERSION/
    TOO_FEW_VALUATION_METHODSが本来発火してもおかしくない値
    (valuation_dispersion_ratio超過・使用手法1件)を補足事実側に置き、
    それでも該当行が生成されないことを確認する。"""
    _seed_batch(tmp_path, "batch-1", "8306")
    _save_reliability_low_recommendation(
        tmp_path,
        valuation_dispersion_ratio=Decimal("5.00"),  # 閾値を大きく超える値
        buy_score_input_facts={
            "buy_price_reliability_concerns": [],  # 空: 何も発火していない
            "valuation_methods_used_count": 1,  # 閾値以下だが対応concern無し
        },
        config_values_used={"valuation_dispersion_medium_max": 1.60},
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("8306")

    # concernsが空の場合は「保存されているが空」であり、旧データ(キー自体が
    # 無い)とは区別しつつ、該当行が1件も無いため非断定フォールバックへ
    # 倒れることを確認する(該当concern無し=表示すべき具体的要因が無い)。
    assert "信頼性低下の具体的な要因は、現行データからは一意に特定できません" in text
    assert "適正価格のばらつきが大きい" not in text
    assert "適正価格の算出に使えた手法が少ない" not in text


def test_excluded_shows_stored_exclusion_reasons(tmp_path: Path) -> None:
    _seed_batch(tmp_path, "batch-1", "9999")
    _save_eval_record(
        tmp_path,
        "batch-1",
        "9999",
        purchase_category=PurchaseCategory.EXCLUDED,
        final_buy_action=BuyAction.EXCLUDED,
        recommendation_id=None,
        exclusion_reasons=("直近決算で重大な業績悪化(営業利益が前期比30%超悪化)",),
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("9999")

    assert "買い対象外" in text
    assert "直近決算で重大な業績悪化" in text


def test_excluded_without_stored_reasons_says_not_saved(tmp_path: Path) -> None:
    _seed_batch(tmp_path, "batch-1", "9999")
    _save_eval_record(
        tmp_path,
        "batch-1",
        "9999",
        purchase_category=PurchaseCategory.EXCLUDED,
        final_buy_action=BuyAction.EXCLUDED,
        recommendation_id=None,
        exclusion_reasons=None,
    )
    service = _service(tmp_path)

    text = service.build_buy_analysis_text("9999")

    assert "保存していません" in text


def test_no_buy_data_reports_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path)
    text = service.build_buy_analysis_text("1234")
    assert "見つかりませんでした" in text


# --- SELL/HOLD側 ---------------------------------------------------------


def _save_holding_eval_record(store_dir: Path, **overrides) -> None:
    defaults: dict = dict(
        holding_evaluation_id=build_holding_evaluation_id(_HOLDING_ID, _NOW),
        holding_id=_HOLDING_ID,
        owner="本人",
        stock_code="8306",
        evaluated_at=_NOW,
        rule_version="v1",
        authoritative_engine="PROFIT_TAKING",
        authoritative_outcome_category="sold_partial",
    )
    defaults.update(overrides)
    HoldingEvaluationRecordRepository(store_dir=store_dir).save(
        HoldingEvaluationRecord(**defaults)
    )


def test_partial_profit_take_shows_quantity_flow_with_ratio_snapshot(tmp_path: Path) -> None:
    rec = Recommendation(
        recommendation_id="rec-sell-1",
        stock_code="8306",
        stock_name="x",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        price_at_recommendation=Decimal("3000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        shares_at_recommendation=500,
        suggested_sell_shares=200,
        suggested_sell_ratio=0.4,
        sell_intensity="STANDARD",
        reasons=["含み益率が利確検討の基準に達しました"],
        config_values_used={"partial_sell_ratios": {"standard": 0.5}},
    )
    RecommendationRepository(store_dir=tmp_path).save(rec)
    _save_holding_eval_record(
        tmp_path, authoritative_recommendation_id="rec-sell-1", authoritative_engine="PROFIT_TAKING"
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert "一部売却を検討" in text
    assert "含み益率が利確検討の基準に達しました" in text
    assert "保有株数：500株" in text
    assert "目標売却比率：50%" in text
    assert "理論株数：250株相当" in text
    assert "売却目安：200株" in text


def test_full_profit_take_does_not_fabricate_a_ratio(tmp_path: Path) -> None:
    rec = Recommendation(
        recommendation_id="rec-sell-2",
        stock_code="8306",
        stock_name="x",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        price_at_recommendation=Decimal("3000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        shares_at_recommendation=400,
        reasons=["含み益が全部売却検討の基準に達しました"],
    )
    RecommendationRepository(store_dir=tmp_path).save(rec)
    _save_holding_eval_record(
        tmp_path, authoritative_recommendation_id="rec-sell-2", authoritative_engine="PROFIT_TAKING"
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert "全部売却を検討" in text
    # FULL_PROFIT_TAKEには数量算出ロジックが無いため、比率・株数セクション自体を
    # 出力しない(修正12: 「目標売却比率100%」等を新しく作らない)。
    assert "■ 売却目安の根拠" not in text
    assert "100%" not in text


def test_legacy_sell_does_not_show_quantity_section(tmp_path: Path) -> None:
    """Legacy SELLエンジンには数量算出ロジックが無いため、他エンジンの比率
    ロジックを合成表示しない(修正8)。"""
    rec = Recommendation(
        recommendation_id="rec-sell-3",
        stock_code="8306",
        stock_name="x",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("2400"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        shares_at_recommendation=600,
        reasons=["該当ルール: 投資前提の重大な悪化"],
    )
    RecommendationRepository(store_dir=tmp_path).save(rec)
    _save_holding_eval_record(
        tmp_path, authoritative_recommendation_id="rec-sell-3", authoritative_engine="LEGACY_SELL"
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert "売却を検討" in text
    assert "該当ルール: 投資前提の重大な悪化" in text
    assert "■ 売却目安の根拠" not in text


def test_pure_hold_shows_unrestorable_reason_not_shadow_data(tmp_path: Path) -> None:
    """SHADOW中のholding_decision_serviceのスコア内訳等は初期実装では一切
    参照・表示しない(修正7)。HoldingEvaluationRecordにはengine種別のみが
    記録され、詳細ペイロードは保持していないため、構造的にも漏れない。"""
    _save_holding_eval_record(
        tmp_path,
        authoritative_recommendation_id=None,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert "保有継続" in text
    assert "現行データでは" in text


def test_no_holding_evaluation_record_reports_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path)
    text = service.build_holding_analysis_text("本人", "9999")
    assert "見つかりませんでした" in text


def test_legacy_sell_hold_shows_facts_from_linked_audit_log(tmp_path: Path) -> None:
    """追加調査(2026-08)対応・レビュー対応(2026-08、修正条件3): authoritative_
    audit_log_id経由で、Legacy SELLの純粋HOLD時にも監査ログに残る実数値(3節の
    証跡拡張で追加した項目)を事実として表示できる。owner取り違えは、
    HoldingEvaluationRecord自身が保持するポインタのため構造的に発生しない。

    単純な真偽値/検出レベルのみのルールが軒並み「該当なし」の場合は、
    LINE表示では個別に列挙せず1行に集約する(監査ログ自体には全ルードの
    証跡がそのまま残っていることを別途確認する)。一方、実際に該当ありの
    ルール(major_scandal)は個別行のまま表示する。
    """
    from jstock_advisor.domain.entities.audit import AuditLogEntry

    audit_entry = AuditLogEntry(
        audit_id="audit-1",
        timestamp=_NOW,
        stock_code="8306",
        decision_type="sell_signal",
        input_values={
            "rule_evidence_details": [
                {
                    "rule_name": "financial_health_severe_deterioration",
                    "status": "NOT_TRIGGERED",
                    "current_value": "45.0%",
                    "previous_value": None,
                    "threshold": "15.0%",
                    "comparison_period": None,
                },
                {
                    "rule_name": "dividend_cut",
                    "status": "NOT_TRIGGERED",
                    "current_value": "False",
                    "previous_value": None,
                    "threshold": None,
                    "comparison_period": None,
                },
                {
                    "rule_name": "shareholder_benefit_abolished",
                    "status": "NOT_TRIGGERED",
                    "current_value": "False",
                    "previous_value": None,
                    "threshold": None,
                    "comparison_period": None,
                },
                {
                    "rule_name": "major_scandal",
                    "status": "TRIGGERED",
                    "current_value": "RISK_KEYWORD_DETECTED",
                    "previous_value": None,
                    "threshold": None,
                    "comparison_period": None,
                },
                {
                    "rule_name": "unfavorable_dividend_policy_change",
                    "status": "NOT_EVALUATED",
                    "current_value": None,
                    "previous_value": None,
                    "threshold": None,
                    "comparison_period": None,
                },
            ]
        },
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1",
    )
    AuditLogRepository(store_dir=tmp_path).save(audit_entry)
    _save_holding_eval_record(
        tmp_path,
        authoritative_recommendation_id=None,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
        authoritative_audit_log_id="audit-1",
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert "保有継続" in text
    # 実数値を持つルールは従来どおり個別表示するが、レビュー対応(2026-08)で
    # status(NOT_TRIGGERED)を明示するようになった(explanation無しのため
    # 現在値・基準値で安全に補足)。
    assert "財務健全性の重大な悪化(一般事業会社基準)：該当なし（45.0%、基準15.0%）" in text
    # 実際に該当ありのルールは、単純な真偽値ルールであっても個別表示する。
    assert "重大な不祥事：リスクキーワードのみ検出" in text
    # 単純な真偽値ルールが「該当なし」のものは、個別列挙せず1行に集約する。
    assert "減配(推測)：該当なし" not in text
    assert "株主優待の廃止：該当なし" not in text
    assert "その他の投資前提悪化ルール(" in text
    assert "減配" in text
    assert "株主優待の廃止" in text
    # NOT_EVALUATED(current_value無し)のルールは表示しない(捏造しない)。
    assert "配当方針の不利な変更" not in text


def test_legacy_sell_hold_quantitative_rules_show_status_and_do_not_reverse(
    tmp_path: Path,
) -> None:
    """本番実データUAT(2026-08)で発覚した修正条件C/D/E: 定量値+閾値形式の
    ルール(balance_sheet_insolvency等)は、数値だけでなくstatus(該当あり/
    該当なし)を明示する。explanationが保存されていればそれを優先して使う。
    同じ表示関数を使う複数ルールで、TRIGGERED/NOT_TRIGGEREDの意味が反転
    しないことも確認する(balance_sheet_insolvency=NOT_TRIGGERED、
    financial_health_severe_deterioration=TRIGGEREDという逆方向の2件を
    同時に検証)。"""
    from jstock_advisor.domain.entities.audit import AuditLogEntry

    audit_entry = AuditLogEntry(
        audit_id="audit-2",
        timestamp=_NOW,
        stock_code="7203",
        decision_type="sell_signal",
        input_values={
            "rule_evidence_details": [
                {
                    "rule_name": "balance_sheet_insolvency",
                    "status": "NOT_TRIGGERED",
                    "current_value": "36.4%",
                    "previous_value": None,
                    "threshold": "0%",
                    "comparison_period": None,
                    "explanation": "自己資本比率はマイナスではない(債務超過ではない)",
                },
                {
                    "rule_name": "financial_health_severe_deterioration",
                    "status": "TRIGGERED",
                    "current_value": "8.0%",
                    "previous_value": None,
                    "threshold": "15.0%",
                    "comparison_period": None,
                    "explanation": "自己資本比率が閾値を下回っている",
                },
            ]
        },
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1",
    )
    AuditLogRepository(store_dir=tmp_path).save(audit_entry)
    _save_holding_eval_record(
        tmp_path,
        authoritative_recommendation_id=None,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
        authoritative_audit_log_id="audit-2",
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    # C: NOT_TRIGGERED + explanationあり → 「該当なし」+ explanationがそのまま使われる。
    assert "債務超過：該当なし（自己資本比率はマイナスではない(債務超過ではない)）" in text
    # D: TRIGGERED → 「該当あり」が明示される。
    assert (
        "財務健全性の重大な悪化(一般事業会社基準)：該当あり（自己資本比率が閾値を下回っている）"
        in text
    )
    # E: 同じ表示関数を使う2ルールで意味が反転していないこと
    # (該当なし側に「該当あり」、該当あり側に「該当なし」が紛れ込んでいない)。
    assert "債務超過：該当あり" not in text
    assert "財務健全性の重大な悪化(一般事業会社基準)：該当なし" not in text


def test_legacy_sell_hold_continuous_decline_shows_status_without_reversal(
    tmp_path: Path,
) -> None:
    """継続悪化2ルールの表示関数でも、statusの反転が起きないことを確認する
    (前期→今期の実数値表示はそのまま維持しつつ、該当あり/なしを付記する)。"""
    from jstock_advisor.domain.entities.audit import AuditLogEntry

    audit_entry = AuditLogEntry(
        audit_id="audit-3",
        timestamp=_NOW,
        stock_code="7203",
        decision_type="sell_signal",
        input_values={
            "rule_evidence_details": [
                {
                    "rule_name": "continuous_operating_income_decline",
                    "status": "NOT_TRIGGERED",
                    "current_value": "520",
                    "previous_value": "500",
                    "threshold": None,
                    "comparison_period": "直近2四半期",
                    "explanation": "営業利益の継続悪化は検出されなかった",
                },
                {
                    "rule_name": "continuous_operating_cashflow_decline",
                    "status": "TRIGGERED",
                    "current_value": "300",
                    "previous_value": "500",
                    "threshold": None,
                    "comparison_period": "直近2四半期",
                    "explanation": (
                        "営業キャッシュフローが継続悪化しており、本業要因が主因と確認できた"
                    ),
                },
            ]
        },
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1",
    )
    AuditLogRepository(store_dir=tmp_path).save(audit_entry)
    _save_holding_eval_record(
        tmp_path,
        authoritative_recommendation_id=None,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
        authoritative_audit_log_id="audit-3",
    )
    service = _service(tmp_path)

    text = service.build_holding_analysis_text("本人", "8306")

    assert (
        "営業利益の継続悪化：該当なし（前期500円→今期520円、直近2四半期、"
        "営業利益の継続悪化は検出されなかった）" in text
    )
    assert (
        "営業キャッシュフローの継続悪化：該当あり（前期500円→今期300円、直近2四半期、"
        "営業キャッシュフローが継続悪化しており、本業要因が主因と確認できた）" in text
    )
    assert "営業利益の継続悪化：該当あり" not in text
    assert "営業キャッシュフローの継続悪化：該当なし" not in text
    assert "現行データでは" not in text

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
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding_evaluation_record import (
    HoldingEvaluationRecord,
    build_holding_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
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

    assert "62.77点" not in text
    assert "スナップショットが無いため" in text


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

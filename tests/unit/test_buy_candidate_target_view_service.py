"""対象確認(直近NORMAL完了BUY候補batch、カテゴリー別一覧、LINE UI第二弾、
読み取り専用、2026-08)のテスト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.models import ScoreWeights
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
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.buy_candidate_target_view_service import (
    CATEGORY_DISPLAY_LABELS,
    BuyCandidateTargetViewService,
    is_valid_category_label,
)
from jstock_advisor.services.latest_batch_records_provider import STILL_PROPAGATING_MESSAGE
from jstock_advisor.services.watchlist_judgment_summary_formatter import format_watchlist_line_body

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)
_WEIGHTS = ScoreWeights(
    total_yield_attractiveness=20,
    dividend_sustainability=20,
    financial_health=20,
    undervaluation=20,
    shareholder_benefit_value=10,
    earnings_stability=5,
    price_stability=5,
)


def _eval_record(
    batch_id: str,
    stock_code: str,
    purchase_category: PurchaseCategory,
    unified_rank: int | None = None,
    recommendation_id: str | None = None,
    final_buy_action: BuyAction | None = None,
) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id=f"{batch_id}:{stock_code}",
        batch_id=batch_id,
        stock_code=stock_code,
        evaluated_at=_NOW,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=purchase_category,
        unified_rank=unified_rank,
        recommendation_id=recommendation_id,
        final_buy_action=final_buy_action,
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


def _recommendation(
    recommendation_id: str,
    stock_code: str,
    reasons: tuple[BuyDecisionReason, ...],
    score_breakdown: ScoreBreakdown | None = None,
    buy_score_input_facts: dict | None = None,
    final_buy_action: BuyAction = BuyAction.WATCH_FOR_PRICE,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
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
        buy_action=final_buy_action,
        base_buy_action=final_buy_action,
        company_quality_score=60.0,
        purchase_attractiveness_score=50.0,
        score_breakdown=score_breakdown or _breakdown(),
        buy_decision_reasons=reasons,
        buy_score_input_facts=buy_score_input_facts,
    )


def _service(
    store_dir: Path, recommendation_repository: RecommendationRepository | None = None
) -> BuyCandidateTargetViewService:
    return BuyCandidateTargetViewService(
        evaluation_record_repository=BuyCandidateEvaluationRecordRepository(store_dir=store_dir),
        latest_batch_pointer_repository=LatestBuyCandidateBatchPointerRepository(
            store_dir=store_dir
        ),
        display_name_resolver=None,
        recommendation_repository=recommendation_repository
        or RecommendationRepository(store_dir=store_dir),
        fallback_score_weights=_WEIGHTS,
    )


def _set_pointer(store_dir: Path, batch_id: str, total: int) -> None:
    LatestBuyCandidateBatchPointerRepository(store_dir=store_dir).update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id=batch_id, completed_at=_NOW, total_candidates=total
        )
    )


def test_seven_category_labels_confirmed() -> None:
    assert CATEGORY_DISPLAY_LABELS == (
        "買い候補",
        "買い間近",
        "買い待ち",
        "買い対象外",
        "要確認",
        "データ不足",
        "処理失敗",
    )
    for label in CATEGORY_DISPLAY_LABELS:
        assert is_valid_category_label(label)
    assert is_valid_category_label("謎のカテゴリ") is False


def test_no_completed_batch_returns_empty_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build_lines("買い候補") == []


def test_unknown_category_label_returns_empty_list(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path)
    assert service.build_lines("謎のカテゴリ") == []


def test_buy_candidate_category_filters_correctly(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    eval_repo.upsert(_eval_record("batch-1", "8306", PurchaseCategory.WATCH_FOR_PRICE))
    _set_pointer(tmp_path, "batch-1", 2)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert lines == ["9432（9432）"]


def test_watch_wait_category_aggregates_watch_for_price_and_watch_before_earnings(
    tmp_path: Path,
) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.WATCH_FOR_PRICE))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.WATCH_BEFORE_EARNINGS))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い待ち")

    assert {line[:4] for line in lines} == {"1111", "2222"}


def test_not_attractive_category_aggregates_not_attractive_and_excluded(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.NOT_ATTRACTIVE))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.EXCLUDED))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.MANUAL_REVIEW))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い対象外")

    assert {line[:4] for line in lines} == {"1111", "2222"}


def test_near_buy_manual_review_data_insufficient_failed_are_each_independent(
    tmp_path: Path,
) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.NEAR_BUY))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.MANUAL_REVIEW))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.DATA_INSUFFICIENT))
    eval_repo.upsert(_eval_record("batch-1", "4444", PurchaseCategory.FAILED))
    _set_pointer(tmp_path, "batch-1", 4)
    service = _service(tmp_path)

    assert [line[:4] for line in service.build_lines("買い間近")] == ["1111"]
    assert [line[:4] for line in service.build_lines("要確認")] == ["2222"]
    assert [line[:4] for line in service.build_lines("データ不足")] == ["3333"]
    assert [line[:4] for line in service.build_lines("処理失敗")] == ["4444"]


def test_zero_matches_for_category_returns_empty_list(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path)

    assert service.build_lines("要確認") == []


def test_multiple_matches_sorted_by_unified_rank_ascending(tmp_path: Path) -> None:
    category = PurchaseCategory.BUY_CANDIDATE
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "3333", category, unified_rank=3))
    eval_repo.upsert(_eval_record("batch-1", "1111", category, unified_rank=1))
    eval_repo.upsert(_eval_record("batch-1", "2222", category, unified_rank=2))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert [line[:4] for line in lines] == ["1111", "2222", "3333"]


def test_unranked_records_sort_after_ranked_ones_by_stock_code(tmp_path: Path) -> None:
    """ランクを持たない銘柄(買い対象外/要確認/データ不足/処理失敗等)は
    stock_code順で安定的に末尾へ。"""
    category = PurchaseCategory.BUY_CANDIDATE
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9999", category, unified_rank=None))
    eval_repo.upsert(_eval_record("batch-1", "1111", category, unified_rank=2))
    eval_repo.upsert(_eval_record("batch-1", "5555", category, unified_rank=None))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert [line[:4] for line in lines] == ["1111", "5555", "9999"]


def test_old_batch_records_are_not_mixed_in(tmp_path: Path) -> None:
    """古いbatchが混ざらないこと(latest_completed_batch_id以外は対象外)。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-old", "9999", PurchaseCategory.BUY_CANDIDATE))
    eval_repo.upsert(_eval_record("batch-new", "1111", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-new", 1)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert lines == ["1111（1111）"]


def test_still_propagating_returns_message_string(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 5)  # 実際のレコード数(1)と不一致=反映待ち
    service = _service(tmp_path)

    assert service.build_lines("買い候補") == STILL_PROPAGATING_MESSAGE


def test_does_not_expose_write_methods(tmp_path: Path) -> None:
    """読み取り専用機能としての安全性(19節)。"""
    service = _service(tmp_path)
    assert not hasattr(service, "upsert")
    assert not hasattr(service, "delete")


# --- 要件2: 短文表示(2026-08、ウォッチリスト表示改善) --------------------------
# watchlist_judgment_summary_formatter.format_watchlist_line_body()を対象確認
# 側でも再利用する。ウォッチリスト側(test_watchlist_judgment_summary_formatter.py)
# と全く同じ入力に対して完全に同一の短文を生成することを、実際に
# format_watchlist_line_body()を直接呼んだ結果と突き合わせて検証する
# (対象確認側が独自の短文生成ロジックを新設していないことの保証)。


def _setup_case(
    tmp_path: Path,
    reasons: tuple[BuyDecisionReason, ...],
    score_breakdown: ScoreBreakdown | None = None,
    buy_score_input_facts: dict | None = None,
    final_buy_action: BuyAction = BuyAction.WATCH_FOR_PRICE,
    category: PurchaseCategory = PurchaseCategory.WATCH_FOR_PRICE,
) -> str:
    """batch-1に銘柄9432を1件登録し、build_lines()が返す唯一の行を返す。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo.upsert(
        _eval_record(
            "batch-1",
            "9432",
            category,
            recommendation_id="rec-9432",
            final_buy_action=final_buy_action,
        )
    )
    rec_repo.save(
        _recommendation(
            "rec-9432",
            "9432",
            reasons,
            score_breakdown=score_breakdown,
            buy_score_input_facts=buy_score_input_facts,
            final_buy_action=final_buy_action,
        )
    )
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path, recommendation_repository=rec_repo)

    label = "買い間近" if category == PurchaseCategory.NEAR_BUY else "買い待ち"
    lines = service.build_lines(label)
    assert isinstance(lines, list)
    assert len(lines) == 1
    return lines[0]


def _expected_line(
    reasons: tuple[BuyDecisionReason, ...],
    score_breakdown: ScoreBreakdown | None = None,
    buy_score_input_facts: dict | None = None,
    final_buy_action: BuyAction = BuyAction.WATCH_FOR_PRICE,
) -> str:
    """ウォッチリスト側と全く同じformat_watchlist_line_body()を直接呼び、
    期待値を独立に計算する(対象確認側の実装がこれと一致することを検証する)。"""
    record = _eval_record(
        "batch-1",
        "9432",
        PurchaseCategory.WATCH_FOR_PRICE,
        recommendation_id="rec-9432",
        final_buy_action=final_buy_action,
    )
    recommendation = _recommendation(
        "rec-9432",
        "9432",
        reasons,
        score_breakdown=score_breakdown,
        buy_score_input_facts=buy_score_input_facts,
        final_buy_action=final_buy_action,
    )
    return format_watchlist_line_body("9432", "9432", record, recommendation, _WEIGHTS)


def test_price_tier_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト11: PRICE_TIERの区分理由がウォッチリストと完全一致すること。"""
    reasons = (BuyDecisionReason(code="PRICE_TIER", message="x"),)
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert actual == "9432（9432）｜現在値が買付価格を上回る"


def test_score_below_threshold_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト12: SCORE_BELOW_THRESHOLDの区分理由がウォッチリストと完全
    一致すること(補足懸念側もSCORE_BELOW_THRESHOLD発生時の「相対的に低め」
    分岐が既定のscore_breakdownで自然に付随するため、reason部分の一致を
    厳密に確認する)。"""
    reasons = (
        BuyDecisionReason(code="PRICE_TIER", message="x"),
        BuyDecisionReason(code="SCORE_BELOW_THRESHOLD", message="x"),
    )
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert "9432（9432）｜総合評価により買付を見送り" in actual


def test_no_valuation_anchor_four_codes_match_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト13: NO_VALUATION_ANCHORの4codeがいずれもウォッチリストと
    完全一致すること。"""
    expected_labels = {
        "NO_VALID_VALUATION_METHODS": "適正価格を算出できず",
        "TOO_FEW_VALUATION_METHODS": "適正価格の算出方式が不足",
        "VALUATION_DISPERSION_TOO_HIGH": "適正価格のばらつき大",
        "VALUATION_ANCHOR_CALCULATION_FAILED": "購入基準価格を算出できず",
    }
    for code, label in expected_labels.items():
        facts = {
            "no_valuation_anchor_reason": {
                "code": code,
                "actual_value": "1.0",
                "threshold_value": "2.0",
            }
        }
        reasons = (BuyDecisionReason(code="NO_VALUATION_ANCHOR", message="x"),)
        # 各回ごとに独立したtmp_pathが必要なため、サブディレクトリへ分離する。
        case_dir = tmp_path / code
        case_dir.mkdir()
        actual = _setup_case(case_dir, reasons, buy_score_input_facts=facts)
        expected = _expected_line(reasons, buy_score_input_facts=facts)
        assert actual == expected, f"code={code}"
        assert actual == f"9432（9432）｜{label}"


def test_no_valuation_anchor_old_data_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト14: no_valuation_anchor_reasonが無い旧データの表示が
    ウォッチリストと完全一致すること(原因を推測しない非断定表示)。"""
    reasons = (BuyDecisionReason(code="NO_VALUATION_ANCHOR", message="x"),)
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert actual == "9432（9432）｜購入基準価格を決定できず"


def test_no_valuation_anchor_unknown_code_matches_watchlist_formatter_and_hides_code(
    tmp_path: Path,
) -> None:
    """必須テスト15: 未知codeの場合もウォッチリストと完全一致し、かつ未知code
    自体をユーザー向け文言へ露出しないこと。"""
    facts = {
        "no_valuation_anchor_reason": {
            "code": "UNKNOWN_FUTURE_REASON",
            "actual_value": "1.0",
            "threshold_value": "2.0",
        }
    }
    reasons = (BuyDecisionReason(code="NO_VALUATION_ANCHOR", message="x"),)
    actual = _setup_case(tmp_path, reasons, buy_score_input_facts=facts)
    assert actual == _expected_line(reasons, buy_score_input_facts=facts)
    assert actual == "9432（9432）｜購入基準価格を決定できず"
    assert "UNKNOWN_FUTURE_REASON" not in actual


def test_earnings_window_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト16: EARNINGS_WINDOWがウォッチリストと完全一致すること。"""
    reasons = (
        BuyDecisionReason(code="PRICE_TIER", message="x"),
        BuyDecisionReason(code="EARNINGS_WINDOW", message="x"),
    )
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert actual == "9432（9432）｜次回決算が近いため保留"


def test_buy_price_reliability_low_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト17: BUY_PRICE_RELIABILITY_LOWがウォッチリストと完全一致
    すること。"""
    reasons = (
        BuyDecisionReason(code="PRICE_TIER", message="x"),
        BuyDecisionReason(code="BUY_PRICE_RELIABILITY_LOW", message="x"),
    )
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert actual == "9432（9432）｜価格算出の信頼度が低い"


def test_valuation_dispersion_too_high_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト18: VALUATION_DISPERSION_TOO_HIGH(BuyDecisionReason側)が
    ウォッチリストと完全一致すること。"""
    reasons = (
        BuyDecisionReason(code="PRICE_TIER", message="x"),
        BuyDecisionReason(code="VALUATION_DISPERSION_TOO_HIGH", message="x"),
    )
    actual = _setup_case(tmp_path, reasons)
    assert actual == _expected_line(reasons)
    assert actual == "9432（9432）｜評価手法間のばらつきが大きい"


def test_supplementary_concern_matches_watchlist_formatter(tmp_path: Path) -> None:
    """必須テスト10: 補足懸念もウォッチリストと完全一致すること。"""
    reasons = (BuyDecisionReason(code="PRICE_TIER", message="x"),)
    # financial_health=5/20=0.25<0.3のみ弱い。
    breakdown = _breakdown(15, 15, 5, 15, 8, 4, 4)
    actual = _setup_case(tmp_path, reasons, score_breakdown=breakdown)
    assert actual == _expected_line(reasons, score_breakdown=breakdown)
    assert actual == "9432（9432）｜現在値が買付価格を上回る｜財務健全性に懸念"


def test_reason_only_has_no_trailing_separator(tmp_path: Path) -> None:
    """必須テスト19: 区分理由だけ存在する場合、不要な区切り文字が付かない
    (末尾に「｜」が残らない)。"""
    reasons = (BuyDecisionReason(code="PRICE_TIER", message="x"),)
    # 全項目が0.3以上(弱い項目なし、補足懸念なし)。
    breakdown = _breakdown(15, 15, 15, 15, 8, 4, 4)
    actual = _setup_case(tmp_path, reasons, score_breakdown=breakdown)
    assert actual == "9432（9432）｜現在値が買付価格を上回る"
    assert not actual.endswith("｜")
    assert not actual.endswith("、")


def test_concern_only_is_displayed_correctly(tmp_path: Path) -> None:
    """必須テスト20: 補足懸念だけ存在する場合(区分理由が空文字列)も正しく
    表示されること。"""
    # buy_decision_reasonsが空の場合、_category_reason_text()は""を返す
    # (recommendation.buy_decision_reasonsが空タプルのケース)。
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo.upsert(
        _eval_record(
            "batch-1", "9432", PurchaseCategory.WATCH_FOR_PRICE, recommendation_id="rec-9432"
        )
    )
    breakdown = _breakdown(15, 15, 5, 15, 8, 4, 4)  # financial_healthのみ弱い
    rec_repo.save(_recommendation("rec-9432", "9432", (), score_breakdown=breakdown))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path, recommendation_repository=rec_repo)

    lines = service.build_lines("買い待ち")

    assert lines == ["9432（9432）｜財務健全性に懸念"]


def test_neither_reason_nor_concern_shows_bare_name_and_code(tmp_path: Path) -> None:
    """必須テスト21: 区分理由・補足懸念のいずれも存在しない場合、
    「社名（コード）」だけになること。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert lines == ["9432（9432）"]


def test_count_display_unaffected_by_enrichment(tmp_path: Path) -> None:
    """必須テスト22: 件数表示(len(lines))は短文追加の前後で意味が変わらない
    (全一致件数を表す、build_lines()が返す行数と一致)。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    reasons = (BuyDecisionReason(code="PRICE_TIER", message="x"),)
    for code in ("1111", "2222", "3333"):
        eval_repo.upsert(
            _eval_record(
                "batch-1", code, PurchaseCategory.WATCH_FOR_PRICE, recommendation_id=f"rec-{code}"
            )
        )
        rec_repo.save(_recommendation(f"rec-{code}", code, reasons))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path, recommendation_repository=rec_repo)

    lines = service.build_lines("買い待ち")

    assert isinstance(lines, list)
    assert len(lines) == 3  # 件数表示(呼び出し元がlen(lines)をそのまま使う)は3件のまま

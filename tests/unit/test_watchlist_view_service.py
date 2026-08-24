"""ウォッチリスト一覧表示(LINE UI第二弾、読み取り専用、2026-08)のテスト。"""

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
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    Priority,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.latest_batch_records_provider import STILL_PROPAGATING_MESSAGE
from jstock_advisor.services.watchlist_view_service import WatchlistViewService

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


def _watchlist_item(stock_code: str, priority: Priority = Priority.MEDIUM) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        priority=priority,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _eval_record(
    batch_id: str,
    stock_code: str,
    purchase_category: PurchaseCategory = PurchaseCategory.WATCH_FOR_PRICE,
    final_buy_action: BuyAction | None = BuyAction.WATCH_FOR_PRICE,
    recommendation_id: str | None = "rec-1",
) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id=f"{batch_id}:{stock_code}",
        batch_id=batch_id,
        stock_code=stock_code,
        evaluated_at=_NOW,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=purchase_category,
        final_buy_action=final_buy_action,
        raw_buy_action=final_buy_action,
        recommendation_id=recommendation_id,
    )


def _recommendation(recommendation_id: str, stock_code: str) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH_BUY,
        buy_prices=BuyPriceLevels(entry=PriceWithRationale(price=Decimal("3500"), rationale="x")),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        base_buy_action=BuyAction.WATCH_FOR_PRICE,
        buy_decision_reasons=(BuyDecisionReason(code="PRICE_TIER", message="x"),),
    )


def _service(store_dir: Path) -> WatchlistViewService:
    return WatchlistViewService(
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
        evaluation_record_repository=BuyCandidateEvaluationRecordRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        latest_batch_pointer_repository=LatestBuyCandidateBatchPointerRepository(
            store_dir=store_dir
        ),
        display_name_resolver=None,
        fallback_score_weights=_WEIGHTS,
    )


def test_empty_watchlist_returns_empty_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build_lines() == []


def test_no_completed_batch_shows_no_history_for_all_items(tmp_path: Path) -> None:
    WatchlistRepository(store_dir=tmp_path).upsert(_watchlist_item("9432"))
    service = _service(tmp_path)

    lines = service.build_lines()

    assert lines == ["銘柄9432（9432）｜判定履歴なし"]


def test_item_not_in_latest_batch_shows_no_history(tmp_path: Path) -> None:
    """直近NORMAL完了batchの候補ユニバースに含まれなかった銘柄は
    「判定履歴なし」(全履歴からの最新判定を遡らない)。"""
    WatchlistRepository(store_dir=tmp_path).upsert(_watchlist_item("9432"))
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-old", "9432"))  # 古いbatchにのみ存在
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-new", completed_at=_NOW, total_candidates=0
        )
    )
    service = _service(tmp_path)

    lines = service.build_lines()

    assert lines == ["銘柄9432（9432）｜判定履歴なし"]


def test_item_in_latest_batch_shows_judgment(tmp_path: Path) -> None:
    WatchlistRepository(store_dir=tmp_path).upsert(_watchlist_item("9432"))
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", recommendation_id="rec-9432"))
    RecommendationRepository(store_dir=tmp_path).save(_recommendation("rec-9432", "9432"))
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=1
        )
    )
    service = _service(tmp_path)

    lines = service.build_lines()

    assert lines == ["銘柄9432（9432）｜買い待ち｜現在値が買付価格を上回る"]


def test_sorted_by_priority_then_stock_code(tmp_path: Path) -> None:
    repo = WatchlistRepository(store_dir=tmp_path)
    repo.upsert(_watchlist_item("2222", priority=Priority.LOW))
    repo.upsert(_watchlist_item("1111", priority=Priority.HIGH))
    repo.upsert(_watchlist_item("3333", priority=Priority.HIGH))
    repo.upsert(_watchlist_item("4444", priority=Priority.MEDIUM))
    service = _service(tmp_path)

    lines = service.build_lines()

    codes_in_order = [line.split("（")[1][:4] for line in lines]
    assert codes_in_order == ["1111", "3333", "4444", "2222"]


def test_still_propagating_returns_message_string(tmp_path: Path) -> None:
    WatchlistRepository(store_dir=tmp_path).upsert(_watchlist_item("9432"))
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432"))
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    # total_candidates(2)がGSI(ローカルfind)結果件数(1)と一致しない=反映待ちを模す。
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=2
        )
    )
    service = _service(tmp_path)

    result = service.build_lines()

    assert result == STILL_PROPAGATING_MESSAGE


def test_does_not_write_to_watchlist_or_evaluation_records(tmp_path: Path) -> None:
    """読み取り専用機能としての安全性(19節)。"""
    service = _service(tmp_path)
    assert not hasattr(service, "upsert")
    assert not hasattr(service, "delete")

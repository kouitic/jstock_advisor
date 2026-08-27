"""ウォッチリスト一覧表示(LINE UI第二弾、読み取り専用、2026-08)のテスト。

ウォッチリスト表示改善(2026-08、Phase 2-B文章仕様最終案): 7区分固定順・
0件は「対象なし」・区分ラベルは見出しに1回だけ(各行には含めない)・
複数LINEメッセージ(最大5件、各4500文字)への分割。
"""

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
from jstock_advisor.services.buy_candidate_target_view_service import CATEGORY_DISPLAY_LABELS
from jstock_advisor.services.latest_batch_records_provider import STILL_PROPAGATING_MESSAGE
from jstock_advisor.services.watchlist_view_service import (
    MESSAGE_CHAR_BUDGET,
    WatchlistViewService,
    _MessagePacker,
    _pack_category_groups,
)

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


def _single_message(service: WatchlistViewService) -> list[str]:
    """テスト用: 単一メッセージ(通常件数)を前提に最初のメッセージだけ返す。"""
    groups = service.build_message_groups()
    assert isinstance(groups, list)
    assert len(groups) == 1
    return groups[0]


def _lines_for_category(lines: list[str], label: str) -> list[str]:
    """指定区分見出しの直後から、次の見出し(または末尾)までの行を返す
    (見出し自身は含まない)。

    レビュー対応(2026-08、ウォッチリスト表示改善): 次区分見出しの直前に
    挿入される区切り空行は、区分同士の境界を示すものであり、この区分
    自体の内容ではないため、末尾に付いていれば取り除いて比較する。
    """
    header = f"【{label}】"
    start = lines.index(header) + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("【"):
            end = i
            break
    category_lines = lines[start:end]
    if category_lines and category_lines[-1] == "":
        category_lines = category_lines[:-1]
    return category_lines


def test_all_seven_category_headers_always_present(tmp_path: Path) -> None:
    service = _service(tmp_path)
    lines = _single_message(service)
    for label in CATEGORY_DISPLAY_LABELS:
        assert f"【{label}】" in lines


def test_empty_watchlist_shows_no_target_for_every_category(tmp_path: Path) -> None:
    service = _service(tmp_path)
    lines = _single_message(service)
    for label in CATEGORY_DISPLAY_LABELS:
        assert _lines_for_category(lines, label) == ["対象なし"]


def test_no_completed_batch_groups_under_data_insufficient(tmp_path: Path) -> None:
    WatchlistRepository(store_dir=tmp_path).upsert(_watchlist_item("9432"))
    service = _service(tmp_path)

    lines = _single_message(service)

    assert _lines_for_category(lines, "データ不足") == ["銘柄9432（9432）｜判定履歴なし"]
    assert _lines_for_category(lines, "買い待ち") == ["対象なし"]


def test_item_not_in_latest_batch_groups_under_data_insufficient(tmp_path: Path) -> None:
    """直近NORMAL完了batchの候補ユニバースに含まれなかった銘柄は
    「データ不足」区分へ分類される(全履歴からの最新判定を遡らない)。"""
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

    lines = _single_message(service)

    assert _lines_for_category(lines, "データ不足") == ["銘柄9432（9432）｜判定履歴なし"]


def test_item_in_latest_batch_groups_under_its_category_without_per_line_label(
    tmp_path: Path,
) -> None:
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

    lines = _single_message(service)

    # 区分ラベル(買い待ち)は見出し側にのみ表示され、銘柄行には含まれない。
    assert _lines_for_category(lines, "買い待ち") == [
        "銘柄9432（9432）｜現在値が買付価格を上回る"
    ]


def test_sorted_by_priority_then_stock_code_within_category(tmp_path: Path) -> None:
    repo = WatchlistRepository(store_dir=tmp_path)
    repo.upsert(_watchlist_item("2222", priority=Priority.LOW))
    repo.upsert(_watchlist_item("1111", priority=Priority.HIGH))
    repo.upsert(_watchlist_item("3333", priority=Priority.HIGH))
    repo.upsert(_watchlist_item("4444", priority=Priority.MEDIUM))
    service = _service(tmp_path)

    lines = _single_message(service)
    category_lines = _lines_for_category(lines, "データ不足")

    codes_in_order = [line.split("（")[1][:4] for line in category_lines]
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

    result = service.build_message_groups()

    assert result == STILL_PROPAGATING_MESSAGE


def test_overflow_splits_into_multiple_messages_without_dropping_headers(
    tmp_path: Path,
) -> None:
    """1メッセージに収まらない件数の場合、複数メッセージへ分割されるが、
    どのメッセージ群全体を通じても7区分の見出しは必ず一度は出現する。"""
    watchlist_repo = WatchlistRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    for i in range(400):
        stock_code = f"{1000 + i}"
        watchlist_repo.upsert(_watchlist_item(stock_code))
        eval_repo.upsert(
            _eval_record(
                "batch-1",
                stock_code,
                purchase_category=PurchaseCategory.NOT_ATTRACTIVE,
                final_buy_action=BuyAction.NOT_ATTRACTIVE,
                recommendation_id=None,
            )
        )
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=400
        )
    )
    service = _service(tmp_path)

    groups = service.build_message_groups()

    assert isinstance(groups, list)
    assert len(groups) > 1
    assert len(groups) <= 5
    for group in groups:
        assert sum(len(line) + 1 for line in group) <= 4500 + 200  # ヘッダー強制追記分の余裕
    all_lines = [line for group in groups for line in group]
    for label in CATEGORY_DISPLAY_LABELS:
        assert f"【{label}】" in all_lines


def test_does_not_write_to_watchlist_or_evaluation_records(tmp_path: Path) -> None:
    """読み取り専用機能としての安全性(19節)。"""
    service = _service(tmp_path)
    assert not hasattr(service, "upsert")
    assert not hasattr(service, "delete")


# --- 要件1: 区分見出し切り替わり時の区切り空行(2026-08、ウォッチリスト表示改善) ---
# _MessagePackerを直接テストする(WatchlistViewService.build_message_groups()
# 経由の統合テストは上記の既存テスト群で別途カバーされている)。


def test_first_category_header_has_no_leading_blank_line() -> None:
    """必須テスト1: 最初の区分見出し前に空行が入らない。"""
    packer = _MessagePacker()
    packer.add_category("買い候補", ["A（1111）"])
    assert packer.messages[0][0] == "【買い候補】"


def test_blank_line_inserted_before_second_category_header() -> None:
    """必須テスト2: 2区分目以降の見出し前に空行が1行入る。"""
    packer = _MessagePacker()
    packer.add_category("買い候補", ["A（1111）"])
    packer.add_category("買い間近", ["B（2222）"])
    assert packer.messages[0] == [
        "【買い候補】",
        "A（1111）",
        "",
        "【買い間近】",
        "B（2222）",
    ]


def test_no_blank_line_between_items_within_same_category() -> None:
    """必須テスト3: 同一区分内の銘柄同士には空行が入らない。"""
    packer = _MessagePacker()
    packer.add_category("買い候補", ["A（1111）", "B（2222）", "C（3333）"])
    assert packer.messages[0] == ["【買い候補】", "A（1111）", "B（2222）", "C（3333）"]


def test_blank_line_is_exactly_one_between_consecutive_categories() -> None:
    """必須テスト4: 複数の区分が連続しても空行は常に1行だけ(0件区分を挟んでも
    同様、必須テスト6も兼ねる)。"""
    packer = _MessagePacker()
    packer.add_category("買い候補", ["A（1111）"])
    packer.add_category("買い間近", [])
    packer.add_category("買い待ち", ["C（3333）"])
    assert packer.messages[0] == [
        "【買い候補】",
        "A（1111）",
        "",
        "【買い間近】",
        "対象なし",
        "",
        "【買い待ち】",
        "C（3333）",
    ]


def test_no_trailing_blank_line_after_last_category() -> None:
    """必須テスト5: 最終行の後ろに不要な空行が入らない。"""
    packer = _MessagePacker()
    packer.add_category("買い候補", ["A（1111）"])
    packer.add_category("買い間近", ["B（2222）"])
    assert packer.messages[0][-1] == "B（2222）"


def test_new_message_does_not_start_with_blank_line_when_header_overflows() -> None:
    """必須テスト7: メッセージ分割が発生する場合、新規メッセージの先頭は
    見出しから始まり、区切り空行が先頭に来ないこと。

    境界ケース: 「見出し単体ならぎりぎり収まるが、区切り空行を足すと
    4500文字を超える」状態を意図的に作り、この場合も新規メッセージへ
    正しく切り替わり(予算超過も空行の取り残しも起きない)ことを確認する。
    """
    packer = _MessagePacker()
    packer.add_category("買い候補", [])
    header2 = "【買い間近】"
    # 「区切り空行(1文字扱い)+見出し2+区切り文字」がちょうど1文字だけ
    # 予算を超えるよう、現在のメッセージ長を調整する(見出し2単体なら
    # ぎりぎり収まる長さ)。
    filler_len = MESSAGE_CHAR_BUDGET - packer._current_len - len(header2) - 2
    packer._append("あ" * filler_len)
    assert packer._fits(header2)  # 見出し単体ならまだ収まる状態であることの前提確認

    packer.add_category("買い間近", ["B（2222）"])

    assert len(packer.messages) == 2
    # 前のメッセージの末尾はfillerのままで、空行が取り残されていない。
    assert packer.messages[0][-1] == "あ" * filler_len
    # 新規メッセージは見出しから始まり、空行が先頭に来ない。
    assert packer.messages[1] == ["【買い間近】", "B（2222）"]
    # 新規メッセージも予算超過していない。
    assert sum(len(line) + 1 for line in packer.messages[1]) <= MESSAGE_CHAR_BUDGET


def test_category_order_preserved_with_blank_line_separators() -> None:
    """必須テスト8: 空行挿入後も既存の区分順が変わらない。"""
    grouped: dict[str, list[str]] = {label: [] for label in CATEGORY_DISPLAY_LABELS}
    messages = _pack_category_groups(grouped)
    headers_in_order = [
        line[1:-1] for line in messages[0] if line.startswith("【") and line.endswith("】")
    ]
    assert tuple(headers_in_order) == CATEGORY_DISPLAY_LABELS

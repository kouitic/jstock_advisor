"""ウォッチリスト一覧表示(LINE UI第二弾、読み取り専用、2026-08)。

WatchlistItem/BuyCandidateEvaluationRecord/Recommendationを一切書き換えない
読み取り専用サービス。直近購入判定は「直近NORMAL完了batchにおける当該銘柄の
判定」と定義する(全履歴からの最新1件ではない)。

ウォッチリスト表示改善(2026-08、Phase 2-B文章仕様最終案): 7区分
(CATEGORY_DISPLAY_LABELS、対象確認機能と同じ分類・同じ表示順)を固定順で
常に全て表示する(0件でも「対象なし」)。区分ラベルは各銘柄行ではなく区分
見出しに1回だけ表示する。1メッセージ4500文字の予算を守りつつ、LINE Reply
APIの上限である最大5メッセージへ分割する。7区分の見出しは、文字数上限に
達した場合でも必ず全て表示する(見出しを削除するのではなく、その区分内の
銘柄行のみを「対象確認からご確認いただけます」で省略する)。
"""

from __future__ import annotations

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import ScoreWeights
from jstock_advisor.domain.entities.enums import Priority
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
from jstock_advisor.services.latest_batch_records_provider import (
    STILL_PROPAGATING_MESSAGE,
    LatestBatchStillPropagating,
    fetch_latest_normal_batch_records,
)
from jstock_advisor.services.watchlist_display_name import StockDisplayNameResolver
from jstock_advisor.services.watchlist_judgment_summary_formatter import (
    category_label,
    format_watchlist_line_body,
)

_PRIORITY_SORT_ORDER: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 1,
    Priority.LOW: 2,
}

# 判定記録が無い(直近batchに未反映)銘柄の暫定区分。「データ不足」区分と
# 同じラベルへ合流させる(判定できない、という状態が実質的に同じであるため)。
_NO_RECORD_LABEL = "データ不足"

MESSAGE_CHAR_BUDGET = 4500
MAX_MESSAGES = 5
_NO_TARGET_LINE = "対象なし"
_OVERFLOW_GUIDANCE = "🎯対象確認からご確認いただけます"


class WatchlistViewService:
    def __init__(
        self,
        watchlist_repository: WatchlistRepository | None = None,
        evaluation_record_repository: BuyCandidateEvaluationRecordRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        latest_batch_pointer_repository: LatestBuyCandidateBatchPointerRepository | None = None,
        display_name_resolver: StockDisplayNameResolver | None = None,
        fallback_score_weights: ScoreWeights | None = None,
    ) -> None:
        self._watchlist = watchlist_repository or WatchlistRepository()
        self._evaluation_records = (
            evaluation_record_repository or BuyCandidateEvaluationRecordRepository()
        )
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._pointer = (
            latest_batch_pointer_repository or LatestBuyCandidateBatchPointerRepository()
        )
        self._display_name_resolver = display_name_resolver
        # judgment時点のScoreWeightsをRecommendation.config_values_usedから
        # 復元できない場合(スナップショット追加前の既存レコード)のみ使う
        # フォールバック(2026-08-25コードレビュー対応、
        # watchlist_judgment_summary_formatter._resolve_weights参照)。
        self._fallback_score_weights = fallback_score_weights or load_config().scoring.weights

    def build_message_groups(self) -> list[list[str]] | str:
        """戻り値: LINEメッセージ単位でグルーピングされた行のリスト(各要素が
        1メッセージ分の行リスト、最大MAX_MESSAGES件)、またはGSI反映待ちを
        示す単一の安全側メッセージ(str)。呼び出し側はstrの場合そのまま
        LINE本文として使うこと(不完全な一覧を完全な一覧として表示しないため)。
        """
        items = sorted(
            self._watchlist.list_all(),
            key=lambda item: (_PRIORITY_SORT_ORDER.get(item.priority, 1), item.stock_code),
        )

        batch_records = fetch_latest_normal_batch_records(self._pointer, self._evaluation_records)
        if isinstance(batch_records, LatestBatchStillPropagating):
            return STILL_PROPAGATING_MESSAGE
        records_by_stock_code = batch_records.records_by_stock_code if batch_records else {}

        grouped: dict[str, list[str]] = {label: [] for label in CATEGORY_DISPLAY_LABELS}
        for item in items:
            record = records_by_stock_code.get(item.stock_code)
            recommendation = (
                self._recommendations.get(record.recommendation_id)
                if record is not None and record.recommendation_id is not None
                else None
            )
            display_name = (
                self._display_name_resolver.resolve(item.stock_code, fallback_name=item.stock_name)
                if self._display_name_resolver is not None
                else item.stock_name or item.stock_code
            )
            label = category_label(record.purchase_category) if record is not None else (
                _NO_RECORD_LABEL
            )
            grouped[label].append(
                format_watchlist_line_body(
                    display_name,
                    item.stock_code,
                    record,
                    recommendation,
                    self._fallback_score_weights,
                )
            )
        return _pack_category_groups(grouped)


class _MessagePacker:
    """4500文字×最大5メッセージへ、7区分の見出しを必ず全て残したまま詰める。

    区分見出しはハード要件(常に全て表示)のため、通常の文字数予算では
    収まらない場合でも強制的に追記する(その分だけ予算をわずかに超える
    ことを許容する。見出し自体は数文字〜十数文字であり、実運用上の
    超過幅は無視できる)。銘柄行は予算内に収まる分だけ表示し、収まらない
    残りは「他N件は🎯対象確認からご確認いただけます」の1行に要約する。

    レビュー対応(2026-08、ウォッチリスト表示改善): 2つ目以降の区分見出しの
    直前には、区分の境界を視認しやすくするため空行を1行挿入する(_started/
    add_category()参照)。ただし見出しが新規メッセージへ持ち越される場合
    (_start_new_message()発火時)は挿入しない(各メッセージの先頭に不要な
    空行を作らないため)。
    """

    def __init__(self) -> None:
        self.messages: list[list[str]] = [[]]
        self._current_len = 0
        self._started = False

    def _fits(self, line: str) -> bool:
        return self._current_len + len(line) + 1 <= MESSAGE_CHAR_BUDGET

    def _append(self, line: str) -> None:
        self.messages[-1].append(line)
        self._current_len += len(line) + 1

    def _start_new_message(self) -> bool:
        if len(self.messages) >= MAX_MESSAGES:
            return False
        self.messages.append([])
        self._current_len = 0
        return True

    def add_category(self, label: str, item_lines: list[str]) -> None:
        header = f"【{label}】"
        is_first_category = not self._started
        self._started = True

        # 区切り空行が必要かどうかは、同一メッセージ内で前の区分から続く
        # 場合のみ。ただし空行自体も1行分の予算を消費するため、
        # 「空行+見出し」が同一メッセージへ収まるかを併せて判定する
        # (見出し単体ではぎりぎり収まるが空行を足すと超過する境界ケースで、
        # 新規メッセージの先頭に空行だけが取り残されることを防ぐ)。
        needs_separator = not is_first_category and self._current_len > 0
        separator_cost = 1 if needs_separator else 0  # 空行という1行分(内容は空文字列)

        if self._current_len + separator_cost + len(header) + 1 > MESSAGE_CHAR_BUDGET:
            if not self._start_new_message():
                # 上限到達: 見出しは必ず表示するハード要件のため、最後の
                # メッセージへ予算超過を許容してでも追記する(区切り空行は
                # 挿入しない、既存の予算超過許容方針のみを踏襲)。銘柄行は
                # 一切表示せず案内のみ残す。
                self._append(header)
                if item_lines:
                    self._append(f"{len(item_lines)}件は{_OVERFLOW_GUIDANCE}")
                else:
                    self._append(_NO_TARGET_LINE)
                return
            # 新規メッセージの先頭は見出しから始める(空行を入れない)。
            needs_separator = False
        if needs_separator:
            self._append("")

        self._append(header)

        if not item_lines:
            # 0件は必ず「対象なし」を表示する(見出しと同様のハード要件、
            # 数文字のため予算超過は無視できる)。
            self._append(_NO_TARGET_LINE)
            return

        for shown, line in enumerate(item_lines):
            if not self._fits(line):
                if not self._start_new_message():
                    remaining = len(item_lines) - shown
                    self._append(f"他{remaining}件は{_OVERFLOW_GUIDANCE}")
                    return
                self._append(f"{header}(続き)")
            self._append(line)


def _pack_category_groups(grouped: dict[str, list[str]]) -> list[list[str]]:
    packer = _MessagePacker()
    for label in CATEGORY_DISPLAY_LABELS:
        packer.add_category(label, grouped[label])
    return packer.messages

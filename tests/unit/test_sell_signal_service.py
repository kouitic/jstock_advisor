"""sell_signal_service.pyの決算日表示ロジックのテスト(コードレビュー対応:
明治ホールディングス(2269)事例)。

_build_next_review_conditions()はnext_earnings_dateをそのまま表示するため、
過去日を渡すと「次回決算発表(過去日付)後に本判定を再評価する」という誤った
文言が表示されてしまう(明治HD事例の症状そのもの)。build_stock_snapshot()の
一元化により、この関数へは常に検証済みの値(過去日はNone)のみが渡される
ことをここで保証する。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.services.sell_signal_service import _build_next_review_conditions


def test_next_review_conditions_omits_earnings_line_when_date_is_none() -> None:
    """next_earnings_date=None(過去日として検証除外された場合を含む)のとき、
    決算発表に関する文言は一切追加されない(明治HD回帰: 過去日を表示しない)。"""
    conditions = _build_next_review_conditions([], None)
    assert not any("決算発表" in c for c in conditions)


def test_next_review_conditions_includes_earnings_line_when_confirmed() -> None:
    """検証済みの未来日/当日が渡された場合は、その日付をそのまま表示する。"""
    future = dt.date(2026, 11, 15)
    conditions = _build_next_review_conditions([], future)
    assert f"次回決算発表({future})後に本判定を再評価する" in conditions

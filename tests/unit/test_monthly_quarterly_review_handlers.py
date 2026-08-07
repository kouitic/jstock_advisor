"""monthly_review_handler.py/quarterly_review_handler.pyがLINE通知を送信しない
ことの回帰テスト(振り返り機能改修、決定事項18)。"""

from __future__ import annotations

from jstock_advisor.lambda_handlers import monthly_review_handler, quarterly_review_handler


def test_monthly_review_handler_never_sends_line() -> None:
    # build_line_client_from_envというシンボル自体がハンドラから参照されて
    # いないことを確認する(importすらしていないことの回帰確認)。
    assert "build_line_client_from_env" not in dir(monthly_review_handler)

    result = monthly_review_handler.handler({}, None)
    assert result["skipped"] is True
    assert "is_monthly_review_day" in result


def test_quarterly_review_handler_never_sends_line() -> None:
    assert "build_line_client_from_env" not in dir(quarterly_review_handler)

    result = quarterly_review_handler.handler({}, None)
    assert result["skipped"] is True
    assert "is_quarterly_review_day" in result

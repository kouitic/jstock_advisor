"""Issue #810: 診断用の長文の「判定理由」「データ取得日時」の表示を assert する。

対象は `render_notification_preview()` の出力。

## 何を守るテストか(価値の範囲)

`services/line_notification_service.py` の `_format_message()` 配下の長文フォーマットは、
`reasons`(判定理由)を7か所、`data_sources`(データ取得日時)を4か所で表示する。これらの表示行を
壊しても、従来は落ちるテストが1件も無かった(#366 PR-1 の変異の実測。Q1〜Q7)。

**この経路は診断専用である。** `render_notification_preview()` の呼び出し元は
`services/before_after_report_service.py`(設定変更の前後比較。人が手で実行する診断)だけで、
実際にLINEへ送信される本文(50/70文字の短文。`recommendation_adapter.py`)には、ここで固定する
11行は出ない。したがって、本テストの価値は **診断出力(before/afterレポート)の表示を守ること**に
限る。**実送信の通知の表示を守るものではない**(実送信の reasons の表示は、売却・重大リスクの
短文と銘柄分析の返信のテストが別に assert している。#810 の Phase A の変異3種で確認済み)。

将来この診断経路が削除・変更された場合は、本テストも合わせて更新または削除すること。

## 方法

`tests.factories.build_recommendation` で、識別できる固有のマーカー値(判定理由・取得日時)を
持つ Recommendation を作り、各表示箇所へ到達する `recommendation_type` / `buy_action` ごとに
`render_notification_preview()` の出力を見る。11か所を1つの表(parametrize)にして、
どの表示行がどの変異で落ちるかを対応づける。判定・通知の仕様は変えない(表示行の存在と中身のみ)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import BuyAction, RecommendationType
from jstock_advisor.services.line_notification_service import render_notification_preview
from tests.factories import build_recommendation

# 固有のマーカー値。既定の理由(factory の「factoryの既定判定理由」)と区別できる値にする。
_REASON_A = "診断マーカー理由A"
_REASON_B = "診断マーカー理由B"
_REASONS = [_REASON_A, _REASON_B]

# データ取得日時は、複数の data_sources のうち最も古いものを表示する(#576)。
# 古い方を最初・新しい方を最後に置かず、順序に依存しないことも確かめるため逆順で渡す。
_FETCHED_OLDER = dt.datetime(2026, 3, 2, 1, 15, tzinfo=dt.UTC)  # JST 2026-03-02 10:15
_FETCHED_NEWER = dt.datetime(2026, 3, 5, 8, 40, tzinfo=dt.UTC)
_DATA_SOURCES = [
    DataSourceReference(provider="fake-newer", fetched_at=_FETCHED_NEWER),
    DataSourceReference(provider="fake-older", fetched_at=_FETCHED_OLDER),
]
_FETCHED_TEXT = "2026-03-02 10:15 JST"
# 保有判断(SELL_CONSIDERATION 系)の表示だけ、区切りが全角のコロン(既存の表示のまま)。
_FETCHED_LINE_HALF = f"データ取得日時: {_FETCHED_TEXT}"
_FETCHED_LINE_FULL = f"データ取得日時：{_FETCHED_TEXT}"

# 表示箇所ごとの入力: (到達する分岐を決める field, 表示の見出し, reasons の期待される表示)。
# id の Qn は #810 の起票時の変異の番号(Q8 は Q1〜Q7 の外にあった、決算直前の抑制通知の理由)。
_REASON_CASES = [
    pytest.param(
        {"buy_action": BuyAction.BUY},
        "主な評価理由: ",
        f"主な評価理由: {_REASON_A} / {_REASON_B}",
        id="Q1_buy_candidate_main_reasons",
    ),
    pytest.param(
        {"buy_action": BuyAction.WATCH_FOR_PRICE},
        "企業評価: ",
        f"企業評価: {_REASON_A} / {_REASON_B}",
        id="Q2_watch_for_price_company_evaluation",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.REVIEW_BEFORE_EARNINGS},
        "理由:",
        f"理由:\n・{_REASON_A}\n・{_REASON_B}",
        id="Q8_earnings_suppressed_reasons",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW},
        "検出内容:",
        f"検出内容:\n・{_REASON_A}\n・{_REASON_B}",
        id="Q5_portfolio_concentration_detected",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.PARTIAL_PROFIT_TAKE},
        "利確を検討する理由: ",
        f"利確を検討する理由: {_REASON_A} / {_REASON_B}",
        id="Q3_profit_taking_reasons",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.SELL},
        "悪化懸念(投資前提が悪化した理由): ",
        f"悪化懸念(投資前提が悪化した理由): {_REASON_A} / {_REASON_B}",
        id="Q4_sell_deterioration_concerns",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.SELL_CONSIDERATION},
        "主な減点要因：",
        f"主な減点要因：\n・{_REASON_A}\n・{_REASON_B}",
        id="Q6_holding_decision_deduction_factors",
    ),
]

_DATA_SOURCE_CASES = [
    pytest.param({"buy_action": BuyAction.BUY}, _FETCHED_LINE_HALF, id="Q7a_buy_candidate"),
    pytest.param(
        {"recommendation_type": RecommendationType.PARTIAL_PROFIT_TAKE},
        _FETCHED_LINE_HALF,
        id="Q7b_profit_taking",
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.SELL}, _FETCHED_LINE_HALF, id="Q7c_sell"
    ),
    pytest.param(
        {"recommendation_type": RecommendationType.SELL_CONSIDERATION},
        _FETCHED_LINE_FULL,
        id="Q7d_holding_decision",
    ),
]


@pytest.mark.parametrize(("branch", "heading", "expected"), _REASON_CASES)
def test_preview_shows_reasons(
    branch: dict[str, object], heading: str, expected: str
) -> None:
    recommendation = build_recommendation(reasons=list(_REASONS), **branch)

    text = render_notification_preview(recommendation)

    assert expected in text


@pytest.mark.parametrize(("branch", "heading", "expected"), _REASON_CASES)
def test_preview_omits_the_reasons_line_when_there_are_no_reasons(
    branch: dict[str, object], heading: str, expected: str
) -> None:
    """reasons が空のときは、その行(見出し)を出さない(空の見出しだけが残らない)。"""
    recommendation = build_recommendation(reasons=[], **branch)

    text = render_notification_preview(recommendation)

    assert _REASON_A not in text
    assert not any(line.startswith(heading) for line in text.splitlines())


@pytest.mark.parametrize(("branch", "expected_line"), _DATA_SOURCE_CASES)
def test_preview_shows_the_oldest_data_fetch_time(
    branch: dict[str, object], expected_line: str
) -> None:
    recommendation = build_recommendation(data_sources=list(_DATA_SOURCES), **branch)

    text = render_notification_preview(recommendation)

    assert expected_line in text.splitlines()


@pytest.mark.parametrize(("branch", "expected_line"), _DATA_SOURCE_CASES)
def test_preview_omits_the_fetch_time_line_when_there_are_no_data_sources(
    branch: dict[str, object], expected_line: str
) -> None:
    recommendation = build_recommendation(data_sources=[], **branch)

    text = render_notification_preview(recommendation)

    assert "データ取得日時" not in text

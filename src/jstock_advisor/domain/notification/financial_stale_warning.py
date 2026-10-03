"""財務データが最新の決算を反映していない可能性があること(財務鮮度 STALE)の、
実送信LINE短文への伝達に使う文言と判定(Issue #474)。

## 何のためのmoduleか

売却・利確判定は、判定に使った財務データが報告サイクル上の最新でない(STALE)場合、
Recommendation.key_risks へ警告の1行(`FINANCIAL_STALE_USER_WARNING`)を入れる
(`profit_taking_service.py` / `sell_signal_service.py` /
`holding_decision_notification_builder.py`)。
これが財務鮮度を示す**既存の唯一の伝達経路**で、Recommendationへ新しい field は追加しない
(USER決定 2026-10-03、#122 issuecomment-5963453330)。

実際にLINEへ送信される短文(50/70文字ルール。`message_formatter.format_notification_text()`)へ
この事実を出すため、`recommendation_adapter.py` が key_risks を**完全一致**で判定し、
短縮ラベル(`FINANCIAL_STALE_SHORT_LABEL`)を `NotificationTextInput` へ渡す。

## なぜ文言をここへ置くか(依存方向。#474 D-3 / MANAGER 判断 = 案 D)

警告文言の元の定義は `services/financial_freshness_integration.py` にある。
`recommendation_adapter.py` 自身が「domain 層から service 層への逆依存を作らない」ことを
設計方針としているため、adapter が services の定数を import しない。lock を増やさない
(F-38 / D8 の主要 source を変更しない)ため、services 側は変更せず、**同じ文言を本 module に
置き、両者が一致することを一致テストで固定する**(片方だけ変えるとテストが落ちる)。

## 完全一致(substring / prefix 禁止)

判定は `key_risks` の**要素**が `FINANCIAL_STALE_WARNING_TEXT` と完全に等しいことだけを見る。
前方一致・部分一致・末尾への付加・前後の空白は一致としない。

## #701(valuation_caveats)との二重表示を作らない契約

短文へ出す財務鮮度の源は `key_risks` の完全一致**だけ**である。
`Recommendation.valuation_caveats`(#701。現在は短文へ接続されていない。診断 preview の
長文のみが読む)を将来 SHORT_TEXT へ接続する場合は、`FINANCIAL_STALE_WARNING_TEXT` と
一致する要素を**除外**して、同じ財務鮮度の情報を二重に表示しないこと。
`tests/unit/test_issue_474_*.py` の二重表示テストが、key_risks と valuation_caveats の
両方に同じ警告が入っても、短文に出るラベルがちょうど1回であることを固定している。
"""

from __future__ import annotations

from collections.abc import Iterable

# services/financial_freshness_integration.py の FINANCIAL_STALE_USER_WARNING と同じ値で
# なければならない(一致テストが固定する)。変更する場合は両方を同時に変えること。
FINANCIAL_STALE_WARNING_TEXT = "最新の決算が財務データへ反映されていない可能性がある"

# 実送信短文に出す短縮ラベル(USER決定 2026-10-03: 「決算未反映」など、意味を失わない短い表現)。
# 「財務データが最新決算を反映していない可能性がある」と利用者が理解できる表現であること。
FINANCIAL_STALE_SHORT_LABEL = "決算未反映"


def has_financial_stale_warning(key_risks: Iterable[str]) -> bool:
    """`key_risks` に財務鮮度の警告が**完全一致**で含まれるか。

    `x in key_risks` は、key_risks が誤って str で渡されたときに部分一致になるため使わず、
    要素ごとの等値比較にする(str を渡しても各文字との比較になり、一致しない)。
    """
    return any(item == FINANCIAL_STALE_WARNING_TEXT for item in key_risks)

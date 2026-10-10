"""保有判断の再通知の『暫定の方針』(Issue #890 PR-3。USER 決定: 案 A = 暫定の値で接続する)。

PR-1(domain/signals/holding_decision_renotification.py)の判定部品は、まだ決まっていない 6 つの選択
(D-1〜D-6)を**既定値なしの引数**で受け取る。通知の判断へ接続する以上、呼び出す側は具体値を渡す
必要がある。その値を**この 1 か所だけ**に置く。

★ これは『暫定』である ★
  値は設計 rev1 の推奨値で、USER の確定ではない。**D-1〜D-6 は、本番を ACTIVE にする承認の前に、
  USER が各項目で確定する**(ACTIVE 承認の条件)。確定したら、この定数だけを変える(変更が 1 か所で
  済むことをテストで固定している)。保有判断の通知は、検証モード(SHADOW)の間は経路が動かないため、
  この方針は本番の挙動に影響しない。

  D-1 周期の再送を保有判断にも残す           KEEP(共通の『JST 暦日差 ≧ N 日』を残す)
  D-2 判定の変化の範囲                       ANY_CHANGE(変化すべて)
  D-3 決算後の方式                           FIRST_EVALUATION_AFTER_EARNINGS(1 決算 1 回。案 A)
  D-4 スコアの比較値                         BASE_SCORE(hard gate の頭打ちを避ける)
  D-5 キーワード一致のみの hard gate        NOT_COUNTED(USER 指定: 一致だけで『確認済み』にしない)
  D-6 売却目安価格の参照                     TARGET_PRICE_ONLY(適正価格由来のみ)
"""

from __future__ import annotations

from jstock_advisor.domain.signals.holding_decision_renotification import (
    DecisionChangeScope,
    EarningsMode,
    KeywordOnlyHandling,
    PeriodicPolicy,
    RenotificationPolicy,
    ScoreBasis,
    SellPriceReference,
)

PROVISIONAL_RENOTIFICATION_POLICY = RenotificationPolicy(
    periodic=PeriodicPolicy.KEEP,
    decision_change_scope=DecisionChangeScope.ANY_CHANGE,
    earnings_mode=EarningsMode.FIRST_EVALUATION_AFTER_EARNINGS,
    score_basis=ScoreBasis.BASE_SCORE,
    keyword_only=KeywordOnlyHandling.NOT_COUNTED,
    sell_price_reference=SellPriceReference.TARGET_PRICE_ONLY,
)

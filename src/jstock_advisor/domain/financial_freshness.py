"""財務データの鮮度判定(Issue #52 Phase B3-A)。

## この module が答える問い

「取得できている財務データが、**報告サイクル上あるべき最新のもの**か」

具体的には次の2つを区別する。これが Issue #52 Phase B3 の主題である。

```
決算発表前なので旧期のデータが残っている   -> 正常(FRESH)
決算発表後なのに旧期のデータしか無い       -> 異常(STALE)
```

## `now - fiscal_period_end` の単純差分では判定できない

期末からの経過日数だけでは、上の2つを区別できない。3月期末の会社が
5月時点で3月期のデータしか持っていないのは正常だが、8月時点で同じなら異常である。
差分の大小はどちらの場合も同じように増えていく。

そこで**期末を起点に「報告期限」を求め、その期限を過ぎたかどうか**で判定する。

## 決算発表日そのものは使えない

無料 provider(yfinance)は決算発表日を提供せず、`source_published_at` も
設定しない。EDINET は提出日を持つが、対象が有価証券報告書・半期報告書に限られ、
四半期の決算短信を含まず、財務サマリの取得元でもない。

したがって**公開時刻の存在を前提にしない**。代わりに、既に取得できている
期末日と決算期末月から報告サイクルを推定する。

## 上場会社に期待する更新周期は四半期である

```
EXPECTED_FINANCIAL_UPDATE_CYCLE = QUARTERLY
```

日本の上場会社は四半期ごとに財務を開示する。したがって
**次に来るはずの期末は原則3か月後**である。

provider の実態として、四半期データは超大型株を除きほとんど取得できず、
`RecentPeriodsSource.ANNUAL_FALLBACK` になる銘柄が多数を占める。しかしこれは

```
ANNUAL_FALLBACK_SEMANTICS = PROVIDER_DATA_LIMITATION_ONLY
```

であり、**その会社が年次でしか開示しないという意味ではない**。
年次フォールバックを理由に「次の期末は12か月後」としてはならない。
そうすると、四半期ごとに更新されるはずのデータが1年近く古いまま
FRESH と判定されてしまう。

`fiscal_year_end_month` は**年次周期を意味しない**。四半期の暦をどこに
揃えるかを決める **anchor** として使う。

## 推定できないときは UNKNOWN にする

推定の根拠が足りないまま STALE へ倒すと、正常な銘柄へ警告と減点が付く。
架空の期末日を作ることは禁止する。

ただし **`ANNUAL_FALLBACK` であること自体を理由に UNKNOWN へ落とさない。**
決算期末月から四半期の暦を安全に解決できるなら、3か月後を使う。

## 責務の境界(Issue #59 と重複実装しない)

```
FAILURE   取得しようとして失敗した        -> #59 の provider 契約が所有する
MISSING   取得は成功したが値が無い        -> provider / service 層が所有する
FRESH / STALE / UNKNOWN                   -> 本 module が所有する
```

本 module は**正常に取得できた財務データだけ**を入力に取る。
`FinancialFreshnessVerdict` に FAILURE / MISSING を追加しない。
上位 service が既存の failure / missing 状態と合成する。

## Phase B3-A の範囲

**本 module はどの判定経路からも呼ばれない。** 純粋な domain contract のみを
提供する。BUY / holdings / SELL / 利確 / screening / confidence / 通知への
接続は Phase B3-B で行う。したがって本 module を merge しても
Production の挙動は変わらない。

## 閾値を持たない

`reporting_lag_days` は**呼び出し側から明示的に受け取る**。config を読まない。

既存の `fiscal_period_reporting_lag_days`(earnings_window)とは別契約である。
既存値は「決算予定日を起点に**過去方向へ**、許容できる期末の下限を見る」ものだが、
本 module は「期末を起点に**未来方向へ**、報告期限を求める」。
起点も方向も適用条件も異なるため、同じ設定値を共有しない。
共有すると、片方の校正が他方の判定を無言で動かす。
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.entities.enums import RecentPeriodsSource

# 四半期サイクルの月数。四半期の期末は3か月ごとに来るという前提の唯一の置き場所。
_QUARTER_CYCLE_MONTHS: Final = 3
# 四半期周期を「確認できた」とみなすために必要な実績期末の最小件数。
# 1点では間隔を検証できず、機械的な3か月加算になってしまうため2点を要求する。
_MIN_QUARTER_ENDS_FOR_CYCLE: Final = 2


class FinancialFreshnessVerdict(StrEnum):
    """財務データの鮮度判定結果。

    FRESH    報告サイクル上、現在のデータが最新で正しい(発表前なので旧期が正常)
    STALE    報告期限を過ぎているのに、期待される期のデータへ更新されていない
    UNKNOWN  鮮度を判定するための根拠が足りない

    FAILURE / MISSING は本 enum に含めない(module docstring の責務境界を参照)。
    """

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ExpectedPeriodBasis(StrEnum):
    """次に来るはずの期末日を、何を根拠に求めたか。

    監査で「なぜその判定になったか」を追えるようにするために持つ。
    UNKNOWN の理由が「四半期履歴が足りない」のか「決算期末月が無い」のかを
    区別できないと、provider 側の改善余地を見誤る。

    QUARTERLY_HISTORY               実績の期末が四半期周期として整合していた
    FISCAL_YEAR_ANCHORED_QUARTERLY  実績履歴では解決できず、決算期末月を
                                    anchor として四半期の暦を解決した
    UNRESOLVED                      根拠が足りず解決しなかった

    **どちらの解決経路でも、次の期末は3か月後である。**
    決算期末月を使うのは暦を揃えるためであって、年次周期を意味しない。
    """

    QUARTERLY_HISTORY = "QUARTERLY_HISTORY"
    FISCAL_YEAR_ANCHORED_QUARTERLY = "FISCAL_YEAR_ANCHORED_QUARTERLY"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class ExpectedNextPeriod:
    """次に来るはずの期末日の解決結果。

    period_end が None のとき、basis は必ず UNRESOLVED になる。
    """

    period_end: dt.date | None
    basis: ExpectedPeriodBasis


@dataclass(frozen=True)
class FinancialFreshnessResult:
    """財務鮮度の判定結果と、その根拠。

    判定だけでなく根拠も返すのは、Phase B3-C で監査へ記録する際に
    「なぜ UNKNOWN だったか」を後から説明できるようにするため。
    """

    verdict: FinancialFreshnessVerdict
    expected_next_period_end: dt.date | None
    expected_report_deadline: dt.date | None
    basis: ExpectedPeriodBasis
    reason: str


# 判定理由の文言はここへ集約する。呼び出し側へ分散させない。
_REASON_FRESH: Final = (
    "次の決算({expected})の報告期限({deadline})前のため、現在の財務データは最新です"
)
_REASON_STALE: Final = (
    "次の決算({expected})の報告期限({deadline})を過ぎていますが、"
    "財務データが更新されていません"
)
_REASON_UNKNOWN_NO_PERIOD: Final = "財務データの期間末を確認できないため鮮度を判定できません"
_REASON_UNKNOWN_FUTURE_PERIOD: Final = (
    "財務データの期間末が未来日({period_end})のため鮮度を判定できません"
)
_REASON_UNKNOWN_NOT_MONTH_END: Final = (
    "財務データの期間末({period_end})が月末ではないため鮮度を判定できません"
)
_REASON_UNKNOWN_NO_CYCLE: Final = "決算サイクルを確認できないため鮮度を判定できません"
_REASON_UNKNOWN_INVALID_LAG: Final = "報告期限の猶予日数が不正なため鮮度を判定できません"


def _is_month_end(value: dt.date) -> bool:
    """その日付が属する月の最終日か。

    日本の決算期末は月末である。月末でない期末日は provider 側の異常か、
    本 module が想定していない決算形態を示す。いずれの場合も推定の根拠に
    しない(UNKNOWN へ倒す)。
    """
    return value.day == calendar.monthrange(value.year, value.month)[1]


def _add_months(value: dt.date, months: int) -> dt.date:
    """月末を月末のまま保って月を加算する。

    単純な「月に+3」ではうるう年・月末で破綻する。次の2点を守る。

    ```
    入力が月末     -> 出力も月末     2024-02-29 +3 -> 2024-05-31
    入力が月末でない -> 日を維持し、桁溢れは月末へ丸める
    ```

    前者を守らないと、2月末を起点にした四半期列(2/29 -> 5/31)が
    「間隔が不整合」と誤判定され、正常な銘柄が UNKNOWN になる。
    """
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if _is_month_end(value) else min(value.day, last_day)
    return dt.date(year, month, day)


def _has_verified_quarterly_cycle(quarter_ends: tuple[dt.date, ...]) -> bool:
    """実績の期末日が四半期周期として整合しているか。

    連続する2点が「ちょうど1四半期分」離れていることを全ペアで確認する。
    経過日数(89〜92日)での近似判定はしない。変則決算・決算期変更・欠落を
    許容してしまい、根拠の弱い推定になるため。
    """
    if len(quarter_ends) < _MIN_QUARTER_ENDS_FOR_CYCLE:
        return False
    return all(
        _add_months(previous, _QUARTER_CYCLE_MONTHS) == following
        for previous, following in zip(quarter_ends, quarter_ends[1:], strict=False)
    )


def resolve_expected_next_period_end(
    latest_financial_period_end: dt.date,
    quarter_ends: tuple[dt.date, ...],
    recent_periods_source: RecentPeriodsSource,
    fiscal_year_end_month: int | None,
    evaluation_date: dt.date,
) -> ExpectedNextPeriod:
    """次に来るはずの期末日を求める。根拠が足りなければ解決しない。

    解決順序:

    ```
    1  実績が四半期由来で、期末が2点以上あり、周期が整合しており、
       かつ**履歴の末尾が latest_financial_period_end と一致している**
         -> 直近の期末に1四半期を加える(basis = QUARTERLY_HISTORY)
    2  1が成立せず、決算期末月を anchor として直近期末が四半期の暦に乗る
         -> 直近の期末に**1四半期**を加える
            (basis = FISCAL_YEAR_ANCHORED_QUARTERLY)
    3  いずれも成立しない
         -> UNRESOLVED(推定しない)
    ```

    **2 でも加えるのは3か月である。1年ではない。**
    決算期末月は暦を揃える anchor であり、更新周期そのものではない。

    `recent_periods_source` を見るのは、`quarter_ends` に値があっても
    それが年次フォールバック由来のことがあるため。年次の期末を「四半期の実績
    履歴」として周期検証に使うと、実在しない期末日を作って STALE を誤検出する。

    ただし **`ANNUAL_FALLBACK` であることを理由に推定を諦めない。** 履歴による
    周期検証ができないだけで、決算期末月から暦を解決できるなら 2 を使う。

    1 の末尾一致が必要な理由(review 指摘)。四半期履歴が
    `latest_financial_period_end` より古いことがある。その場合に履歴の末尾から
    1四半期を進めると、**既に持っている期を「次に来るはず」として扱ってしまう**。

    ```
    latest = 2024-09-30 / 履歴 = (2024-03-31, 2024-06-30)
      履歴だけ見ると周期は整合している
      末尾 2024-06-30 + 1四半期 = 2024-09-30 = latest と同一
      -> 期限を過ぎると「2024-09-30 へまだ更新されていない」と判定してしまう
    ```

    次の期末は必ず**現在の最新期末**から進める。古い履歴の末尾から進めない。

    Args:
        latest_financial_period_end: 解決済みの直近期末日。評価日以前であること。
        quarter_ends: 実績の期末日。順不同・重複・未来日を含んでよい。
        recent_periods_source: `quarter_ends` の由来。
        fiscal_year_end_month: 正式な決算期末月(1-12)。不明なら None。
        evaluation_date: 判定基準となる JST 暦日。
    """
    if not _is_month_end(latest_financial_period_end):
        return ExpectedNextPeriod(period_end=None, basis=ExpectedPeriodBasis.UNRESOLVED)

    if recent_periods_source is RecentPeriodsSource.QUARTERLY:
        # 未来日は根拠にしない(provider 異常・時刻ずれの可能性)。
        # 重複は間隔検証を壊すため除去する。月末でない値が混ざっていたら
        # 想定外の期末形態として四半期推定を諦める。
        usable = tuple(sorted({q for q in quarter_ends if q <= evaluation_date}))
        if (
            usable
            and all(_is_month_end(q) for q in usable)
            and _has_verified_quarterly_cycle(usable)
            # 四半期履歴が「現在の最新期末」まで到達していることを必須とする。
            #
            # これが無いと、履歴が最新期末より古い場合に
            # 「履歴の末尾 + 1四半期」を次の期末としてしまう。その値が
            # 既に持っている latest_financial_period_end と一致すると、
            # **既に取得済みの期を「まだ更新されていない」と判定する**
            # 論理矛盾が起きる(期限を過ぎれば STALE になる)。
            #
            # 比較対象は sanitize 後の末尾であり、引数の生の最後の要素ではない
            # (順不同・重複・未来日を含みうるため)。
            and usable[-1] == latest_financial_period_end
        ):
            return ExpectedNextPeriod(
                period_end=_add_months(usable[-1], _QUARTER_CYCLE_MONTHS),
                basis=ExpectedPeriodBasis.QUARTERLY_HISTORY,
            )

    # 実績履歴では解決できない場合でも、決算期末月を anchor にすれば
    # 四半期の暦を決められる。3月決算なら 3 / 6 / 9 / 12 月末が期末になる。
    #
    # 直近期末の月が anchor から3の倍数だけ離れていれば、その暦に乗っている。
    # 乗っていない場合は変則決算・決算期変更・provider 異常の可能性があり、
    # 推定の根拠にしない。
    if (
        fiscal_year_end_month is not None
        and 1 <= fiscal_year_end_month <= 12
        and (latest_financial_period_end.month - fiscal_year_end_month)
        % _QUARTER_CYCLE_MONTHS
        == 0
    ):
        return ExpectedNextPeriod(
            period_end=_add_months(latest_financial_period_end, _QUARTER_CYCLE_MONTHS),
            basis=ExpectedPeriodBasis.FISCAL_YEAR_ANCHORED_QUARTERLY,
        )

    return ExpectedNextPeriod(period_end=None, basis=ExpectedPeriodBasis.UNRESOLVED)


def evaluate_financial_freshness(
    latest_financial_period_end: dt.date | None,
    quarter_ends: tuple[dt.date, ...],
    recent_periods_source: RecentPeriodsSource,
    fiscal_year_end_month: int | None,
    evaluation_date: dt.date,
    reporting_lag_days: int,
) -> FinancialFreshnessResult:
    """財務データが報告サイクル上の最新かを判定する。

    ```
    expected_report_deadline = expected_next_period_end + reporting_lag_days

    evaluation_date <  deadline  -> FRESH  (まだ発表前。旧期が正常)
    evaluation_date >= deadline  -> STALE  (期限を過ぎたのに未更新)
    根拠不足                     -> UNKNOWN
    ```

    **期限当日は STALE 側に含める**(境界は 1 つに固定する)。期限当日にはもう
    発表されているべきだからであり、当日を FRESH にすると期限の意味が薄れる。

    `reporting_lag_days` は**暦日**として扱う。営業日へ勝手に読み替えない。
    値そのものは呼び出し側の責務であり、本 module は config を読まない。

    取得時刻(`fetched_at`)は判定に**使わない**。無料 provider は取得の都度
    現在時刻を入れるため、常に「新しい」ことになってしまう。これが Issue #52 の
    根本原因であり、ここで再び持ち込まない。

    Args:
        latest_financial_period_end: 解決済みの直近期末日。取得できなければ None。
        quarter_ends: 実績の期末日。順不同・重複・未来日を含んでよい。
        recent_periods_source: `quarter_ends` の由来。
        fiscal_year_end_month: 正式な決算期末月(1-12)。不明なら None。
        evaluation_date: 判定基準となる JST 暦日。
        reporting_lag_days: 期末から報告期限までの猶予暦日数。0 以上。
    """
    if reporting_lag_days < 0:
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.UNKNOWN,
            expected_next_period_end=None,
            expected_report_deadline=None,
            basis=ExpectedPeriodBasis.UNRESOLVED,
            reason=_REASON_UNKNOWN_INVALID_LAG,
        )

    if latest_financial_period_end is None:
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.UNKNOWN,
            expected_next_period_end=None,
            expected_report_deadline=None,
            basis=ExpectedPeriodBasis.UNRESOLVED,
            reason=_REASON_UNKNOWN_NO_PERIOD,
        )

    if latest_financial_period_end > evaluation_date:
        # 評価日より後の期末は、その時点で存在し得ない。provider 異常として扱い、
        # 「更新済み」の証拠に使わない。
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.UNKNOWN,
            expected_next_period_end=None,
            expected_report_deadline=None,
            basis=ExpectedPeriodBasis.UNRESOLVED,
            reason=_REASON_UNKNOWN_FUTURE_PERIOD.format(period_end=latest_financial_period_end),
        )

    if not _is_month_end(latest_financial_period_end):
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.UNKNOWN,
            expected_next_period_end=None,
            expected_report_deadline=None,
            basis=ExpectedPeriodBasis.UNRESOLVED,
            reason=_REASON_UNKNOWN_NOT_MONTH_END.format(period_end=latest_financial_period_end),
        )

    expected = resolve_expected_next_period_end(
        latest_financial_period_end=latest_financial_period_end,
        quarter_ends=quarter_ends,
        recent_periods_source=recent_periods_source,
        fiscal_year_end_month=fiscal_year_end_month,
        evaluation_date=evaluation_date,
    )
    if expected.period_end is None:
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.UNKNOWN,
            expected_next_period_end=None,
            expected_report_deadline=None,
            basis=ExpectedPeriodBasis.UNRESOLVED,
            reason=_REASON_UNKNOWN_NO_CYCLE,
        )

    deadline = expected.period_end + dt.timedelta(days=reporting_lag_days)
    if evaluation_date < deadline:
        return FinancialFreshnessResult(
            verdict=FinancialFreshnessVerdict.FRESH,
            expected_next_period_end=expected.period_end,
            expected_report_deadline=deadline,
            basis=expected.basis,
            reason=_REASON_FRESH.format(expected=expected.period_end, deadline=deadline),
        )
    return FinancialFreshnessResult(
        verdict=FinancialFreshnessVerdict.STALE,
        expected_next_period_end=expected.period_end,
        expected_report_deadline=deadline,
        basis=expected.basis,
        reason=_REASON_STALE.format(expected=expected.period_end, deadline=deadline),
    )

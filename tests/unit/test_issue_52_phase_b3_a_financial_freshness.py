"""Issue #52 Phase B3-A: 財務鮮度 domain contract の固定。

## このテストが守る契約

```
決算発表前で旧期データ        -> FRESH
報告期限を過ぎて旧期のまま    -> STALE
推定の根拠が足りない          -> UNKNOWN(架空の期末日を作らない)
```

## 時刻を固定する

`datetime.now()` / `date.today()` を基準値として使わない。すべての評価日・
期末日をリテラルで固定する。実行時刻によって結果が変わるテストは、
Issue #143 / #52 Phase B2 で実際に事故を起こしているため書かない。

## Phase B3-A は挙動を変えない

本 module が Production の判定経路から呼ばれていないことも固定する
(`test_no_production_call_site`)。B3-A の merge で挙動が変わらないことを
テストで担保するため。
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import RecentPeriodsSource
from jstock_advisor.domain.financial_freshness import (
    ExpectedPeriodBasis,
    FinancialFreshnessVerdict,
    evaluate_financial_freshness,
    resolve_expected_next_period_end,
)

# --- 固定値 -----------------------------------------------------------------
# 3月決算の会社を基本形とする(日本で最も多いため、読んで意図が分かりやすい)。
_FY_END_MONTH_MARCH = 3
_LAG_DAYS = 60

# 3月決算の四半期期末(2月末を含み、うるう年の扱いを自然に踏むよう選ぶ)。
_Q_2023_09 = dt.date(2023, 9, 30)
_Q_2023_12 = dt.date(2023, 12, 31)
_Q_2024_03 = dt.date(2024, 3, 31)
_Q_2024_06 = dt.date(2024, 6, 30)

_QUARTERLY_HISTORY = (_Q_2023_09, _Q_2023_12, _Q_2024_03)
# 直近 2024-03-31 の次は 2024-06-30、その報告期限は +60日 = 2024-08-29。
_EXPECTED_NEXT_QUARTER = _Q_2024_06
_QUARTER_DEADLINE = dt.date(2024, 8, 29)

# 決算期末月 anchor による四半期解決。実績履歴が無くても
# 3月決算なら 2024-03-31 の次は **2024-06-30**(1年後ではない)。
# 期限は +60日 = 2024-08-29。
_ANCHOR_PERIOD_END = _Q_2024_03
_EXPECTED_NEXT_ANCHORED = _Q_2024_06
_ANCHOR_DEADLINE = dt.date(2024, 8, 29)


def _evaluate(
    *,
    latest: dt.date | None,
    quarter_ends: tuple[dt.date, ...] = (),
    source: RecentPeriodsSource = RecentPeriodsSource.UNAVAILABLE,
    fy_end_month: int | None = None,
    evaluation_date: dt.date,
    lag_days: int = _LAG_DAYS,
):
    return evaluate_financial_freshness(
        latest_financial_period_end=latest,
        quarter_ends=quarter_ends,
        recent_periods_source=source,
        fiscal_year_end_month=fy_end_month,
        evaluation_date=evaluation_date,
        reporting_lag_days=lag_days,
    )


# --- 1-2. 四半期サイクル ------------------------------------------------------


def test_quarterly_cycle_before_deadline_is_fresh() -> None:
    """発表前に旧期データが残っているのは正常。"""
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=_QUARTER_DEADLINE - dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.FRESH
    assert result.expected_next_period_end == _EXPECTED_NEXT_QUARTER
    assert result.expected_report_deadline == _QUARTER_DEADLINE
    assert result.basis is ExpectedPeriodBasis.QUARTERLY_HISTORY


def test_quarterly_cycle_after_deadline_is_stale() -> None:
    """期限を過ぎても更新されていなければ異常。"""
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=_QUARTER_DEADLINE + dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.STALE
    assert result.expected_report_deadline == _QUARTER_DEADLINE


# --- 3-4. 年次サイクル --------------------------------------------------------


def test_fiscal_year_anchored_expects_next_quarter_not_next_year() -> None:
    """annual fallback でも次の期末は**3か月後**であること。

    以前は +12か月(ANNUAL_CYCLE)としていた。しかし ANNUAL_FALLBACK は
    「その会社が年次でしか開示しない」ではなく「provider が四半期を取れない」
    という取得上の制約にすぎない。上場会社には四半期更新を期待する。

    +12か月のままだと、四半期ごとに更新されるはずのデータが1年近く古くても
    FRESH と判定されてしまう。
    """
    result = _evaluate(
        latest=_ANCHOR_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=_ANCHOR_DEADLINE - dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.FRESH
    assert result.expected_next_period_end == _EXPECTED_NEXT_ANCHORED
    assert result.expected_next_period_end != dt.date(2025, 3, 31)  # 旧契約の値
    assert result.basis is ExpectedPeriodBasis.FISCAL_YEAR_ANCHORED_QUARTERLY


def test_fiscal_year_anchored_after_deadline_is_stale() -> None:
    result = _evaluate(
        latest=_ANCHOR_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=_ANCHOR_DEADLINE + dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.STALE


@pytest.mark.parametrize(
    ("latest", "fy_end_month", "expected_next"),
    [
        # 3月決算。期末は 3 / 6 / 9 / 12 月末
        (dt.date(2026, 3, 31), 3, dt.date(2026, 6, 30)),
        (dt.date(2026, 6, 30), 3, dt.date(2026, 9, 30)),
        (dt.date(2026, 9, 30), 3, dt.date(2026, 12, 31)),
        (dt.date(2026, 12, 31), 3, dt.date(2027, 3, 31)),
        # 12月決算。期末は 12 / 3 / 6 / 9 月末
        (dt.date(2025, 12, 31), 12, dt.date(2026, 3, 31)),
        (dt.date(2026, 3, 31), 12, dt.date(2026, 6, 30)),
    ],
)
def test_fiscal_year_anchor_resolves_quarter_calendar(
    latest: dt.date, fy_end_month: int, expected_next: dt.date
) -> None:
    """決算期末月は四半期の暦を揃える anchor であり、周期そのものではない。"""
    resolved = resolve_expected_next_period_end(
        latest_financial_period_end=latest,
        quarter_ends=(),
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fiscal_year_end_month=fy_end_month,
        evaluation_date=dt.date(2027, 12, 31),
    )
    assert resolved.period_end == expected_next
    assert resolved.basis is ExpectedPeriodBasis.FISCAL_YEAR_ANCHORED_QUARTERLY


def test_annual_fallback_alone_does_not_force_unknown() -> None:
    """ANNUAL_FALLBACK であること自体を理由に推定を諦めない。"""
    resolved = resolve_expected_next_period_end(
        latest_financial_period_end=_ANCHOR_PERIOD_END,
        quarter_ends=(),
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fiscal_year_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2024, 4, 1),
    )
    assert resolved.period_end is not None


def test_period_month_not_on_quarter_calendar_is_unknown() -> None:
    """決算期末月から3の倍数だけ離れていない期末は暦に乗っていない。

    変則決算・決算期変更・provider 異常のいずれかであり、推定の根拠にしない。
    """
    result = _evaluate(
        latest=dt.date(2026, 5, 31),  # 3月決算の暦(3/6/9/12)に乗らない
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2027, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN
    assert result.basis is ExpectedPeriodBasis.UNRESOLVED


def test_human_decided_fifty_day_lag_boundary() -> None:
    """猶予50日の境界。期限当日は STALE、前日は FRESH。

    50 は人間が確定した値だが、domain へは埋め込まず引数で受け取る。
    期待値は実装の計算とは独立に、リテラルで固定する。
    """
    # latest 2026-03-31 -> expected 2026-06-30 -> deadline 2026-06-30 + 50日
    deadline = dt.date(2026, 8, 19)
    fresh = _evaluate(
        latest=dt.date(2026, 3, 31),
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2026, 8, 18),
        lag_days=50,
    )
    stale = _evaluate(
        latest=dt.date(2026, 3, 31),
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=deadline,
        lag_days=50,
    )
    assert fresh.verdict is FinancialFreshnessVerdict.FRESH
    assert fresh.expected_report_deadline == deadline
    assert stale.verdict is FinancialFreshnessVerdict.STALE


def test_reporting_lag_is_not_hardcoded_in_domain() -> None:
    """確定値 50 を domain へ埋め込んでいないこと(供給は B3-B1 の責務)。"""
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "jstock_advisor"
        / "domain"
        / "financial_freshness.py"
    ).read_text(encoding="utf-8")
    assert "= 50" not in source
    assert "reporting_lag_days: int = " not in source


# --- 5-11. UNKNOWN へ倒すべき条件 ---------------------------------------------


def test_single_quarter_end_does_not_infer_cycle() -> None:
    """期末が1点しかないときに機械的な3か月加算をしない。"""
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=(_Q_2024_03,),
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN
    assert result.expected_next_period_end is None
    assert result.basis is ExpectedPeriodBasis.UNRESOLVED


def test_inconsistent_quarter_interval_is_unknown() -> None:
    """四半期間隔が不整合なら周期を確認できたとみなさない。"""
    result = _evaluate(
        latest=_Q_2024_03,
        # 2023-09-30 -> 2024-03-31 は 6 か月。四半期として整合しない。
        quarter_ends=(_Q_2023_09, _Q_2024_03),
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_annual_fallback_is_not_treated_as_quarterly() -> None:
    """年次フォールバック由来の期末を「四半期の実績履歴」として扱わない。

    履歴として使うと周期検証が成立してしまい、実在しない期末日を作る。
    このケースは決算期末月も無いため anchor でも解決できず UNKNOWN になる
    (ANNUAL_FALLBACK だから UNKNOWN なのではない点は
    test_annual_fallback_alone_does_not_force_unknown で固定している)。
    """
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_missing_fiscal_year_end_month_is_unknown() -> None:
    """年次推定が必要な場面で決算期末月が無ければ推定しない。"""
    result = _evaluate(
        latest=_ANCHOR_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=None,
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_fiscal_year_end_month_contradiction_is_unknown() -> None:
    """決算期末月の暦に直近期末が乗らない(決算期変更の可能性)。

    3月期末は 12月決算の暦(12/3/6/9)に乗るため、この組み合わせでは
    anchor で解決できてしまう。ここでは暦に乗らない月を使って
    矛盾を表現する。
    """
    result = _evaluate(
        latest=dt.date(2024, 4, 30),  # 4月期末
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=12,  # 12月決算の暦(12/3/6/9)に 4月は乗らない
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_future_period_end_is_unknown() -> None:
    """評価日より後の期末は、その時点で存在し得ない。"""
    result = _evaluate(
        latest=dt.date(2025, 3, 31),
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_non_month_end_period_is_unknown() -> None:
    """月末でない期末日は想定していない決算形態として扱う。"""
    result = _evaluate(
        latest=dt.date(2024, 3, 15),
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_irregular_fiscal_year_is_unknown() -> None:
    """12か月でない会計年度(移行期)を年次サイクルとして扱わない。

    期末月が決算期末月と一致しないため、矛盾として UNKNOWN になる。
    """
    result = _evaluate(
        latest=dt.date(2024, 10, 31),  # 12月決算の暦(12/3/6/9)に 10月は乗らない
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=12,
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_missing_period_end_is_unknown() -> None:
    result = _evaluate(latest=None, evaluation_date=dt.date(2024, 12, 31))
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


# --- 四半期履歴が最新期末まで到達していること(review 指摘の回帰) ---------------


def test_quarter_history_older_than_latest_period_is_unknown() -> None:
    """履歴が最新期末より古いとき、履歴の末尾から次期を推定しない。

    履歴 (2024-03-31, 2024-06-30) はそれ自体では四半期周期として整合する。
    しかし最新期末は既に 2024-09-30 であり、履歴の末尾 + 1四半期は
    2024-09-30 = 最新期末と同一になる。

    これを許すと **既に取得済みの期を「まだ更新されていない」と判定する**
    という論理矛盾が起き、期限を過ぎた時点で STALE になってしまう。
    """
    result = _evaluate(
        latest=dt.date(2024, 9, 30),
        quarter_ends=(dt.date(2024, 3, 31), dt.date(2024, 6, 30)),
        source=RecentPeriodsSource.QUARTERLY,
        fy_end_month=None,
        evaluation_date=dt.date(2025, 1, 1),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN
    assert result.expected_next_period_end is None
    assert result.basis is ExpectedPeriodBasis.UNRESOLVED


def test_quarter_history_terminating_at_latest_period_resolves() -> None:
    """履歴の末尾が最新期末と一致していれば従来どおり解決する(positive control)。"""
    result = _evaluate(
        latest=dt.date(2024, 6, 30),
        quarter_ends=(dt.date(2024, 3, 31), dt.date(2024, 6, 30)),
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 7, 1),
    )
    assert result.expected_next_period_end == dt.date(2024, 9, 30)
    assert result.basis is ExpectedPeriodBasis.QUARTERLY_HISTORY


def test_history_alignment_is_checked_after_sanitization() -> None:
    """順不同・重複・未来日を含む入力でも、整形後の末尾と最新期末を比較する。

    生の入力の最後の要素と比較すると、並び順や未来日の混入で判定が変わる。
    """
    result = _evaluate(
        latest=dt.date(2024, 6, 30),
        quarter_ends=(
            dt.date(2024, 6, 30),
            dt.date(2024, 3, 31),
            dt.date(2024, 3, 31),  # 重複
            dt.date(2025, 3, 31),  # 未来日(評価日より後)
        ),
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 7, 1),
    )
    assert result.expected_next_period_end == dt.date(2024, 9, 30)
    assert result.basis is ExpectedPeriodBasis.QUARTERLY_HISTORY


def test_history_mismatch_still_allows_fiscal_year_anchored_inference() -> None:
    """履歴で解決できなくても、決算期末月の暦で解決できるなら使う。

    履歴が古くて周期検証は使えないが、決算期末月を anchor にすれば
    暦は決まる。ここで一律 UNKNOWN にすると使える根拠を捨てることになる。
    次の期末は**3か月後**であり1年後ではない。
    """
    result = _evaluate(
        latest=dt.date(2024, 3, 31),
        quarter_ends=(dt.date(2023, 6, 30), dt.date(2023, 9, 30)),
        source=RecentPeriodsSource.QUARTERLY,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=dt.date(2024, 4, 1),
    )
    assert result.expected_next_period_end == dt.date(2024, 6, 30)
    assert result.basis is ExpectedPeriodBasis.FISCAL_YEAR_ANCHORED_QUARTERLY


# --- 12-14. 暦計算の境界 ------------------------------------------------------


@pytest.mark.parametrize(
    ("history", "latest", "expected_next"),
    [
        # 月末日数が異なる月をまたぐ(9/30 -> 12/31)
        ((dt.date(2023, 6, 30), dt.date(2023, 9, 30)), dt.date(2023, 9, 30), dt.date(2023, 12, 31)),
        # うるう年の2月末をまたぐ(11/30 -> 2/29 -> 5/31)
        (
            (dt.date(2023, 11, 30), dt.date(2024, 2, 29)),
            dt.date(2024, 2, 29),
            dt.date(2024, 5, 31),
        ),
        # 平年の2月末(11/30 -> 2/28 -> 5/31)
        (
            (dt.date(2022, 11, 30), dt.date(2023, 2, 28)),
            dt.date(2023, 2, 28),
            dt.date(2023, 5, 31),
        ),
        # 年をまたぐ(12/31 -> 3/31)
        (
            (dt.date(2023, 9, 30), dt.date(2023, 12, 31)),
            dt.date(2023, 12, 31),
            dt.date(2024, 3, 31),
        ),
    ],
)
def test_month_end_arithmetic_across_boundaries(
    history: tuple[dt.date, ...], latest: dt.date, expected_next: dt.date
) -> None:
    """月末は月末のまま進める。日数の異なる月・うるう年・年跨ぎで破綻しない。

    ここを日数加算で実装すると、2月末を含む四半期列が「間隔が不整合」と
    誤判定され、正常な銘柄が UNKNOWN へ落ちる。
    """
    resolved = resolve_expected_next_period_end(
        latest_financial_period_end=latest,
        quarter_ends=history,
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
        fiscal_year_end_month=None,
        evaluation_date=dt.date(2026, 1, 1),
    )
    assert resolved.period_end == expected_next
    assert resolved.basis is ExpectedPeriodBasis.QUARTERLY_HISTORY


def test_leap_day_month_end_preserved_in_anchored_fallback() -> None:
    """うるう年2月末を anchor 経路でも月末のまま3か月進める。

    2024-02-29 -> 2024-05-31。日数加算だと 5/29 になり月末が崩れる。
    """
    resolved = resolve_expected_next_period_end(
        latest_financial_period_end=dt.date(2024, 2, 29),
        quarter_ends=(),
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fiscal_year_end_month=2,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert resolved.period_end == dt.date(2024, 5, 31)
    assert resolved.basis is ExpectedPeriodBasis.FISCAL_YEAR_ANCHORED_QUARTERLY


# --- 15. 期限当日の境界 -------------------------------------------------------


def test_deadline_day_is_stale_not_fresh() -> None:
    """期限当日は STALE 側。境界を1つに固定する。"""
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=_QUARTER_DEADLINE,
    )
    assert result.verdict is FinancialFreshnessVerdict.STALE


def test_day_before_deadline_is_fresh() -> None:
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=_QUARTER_DEADLINE - dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.FRESH


# --- 16. reporting_lag_days は外部引数 ----------------------------------------


def test_reporting_lag_days_is_an_explicit_argument() -> None:
    """猶予日数を変えると期限が動く。module は config を読まない。"""
    short = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 7, 15),
        lag_days=10,
    )
    long = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 7, 15),
        lag_days=90,
    )
    assert short.verdict is FinancialFreshnessVerdict.STALE
    assert long.verdict is FinancialFreshnessVerdict.FRESH


def test_negative_reporting_lag_is_unknown() -> None:
    result = _evaluate(
        latest=_Q_2024_03,
        quarter_ends=_QUARTERLY_HISTORY,
        source=RecentPeriodsSource.QUARTERLY,
        evaluation_date=dt.date(2024, 12, 31),
        lag_days=-1,
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_module_does_not_read_config() -> None:
    """domain が config を読み始めていないことを固定する。"""
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "jstock_advisor"
        / "domain"
        / "financial_freshness.py"
    ).read_text(encoding="utf-8")
    assert "load_config" not in source
    assert "jstock_advisor.config" not in source


# --- 17. 取得時刻を鮮度判定に使わない ------------------------------------------


def test_fetched_at_is_not_part_of_the_contract() -> None:
    """取得時刻は引数に存在しない。

    `fetched_at` は provider が取得の都度 now を入れるため、鮮度の根拠にならない。
    これを持ち込むことが Issue #52 の根本原因だった。引数として受け取らない
    ことで、構造的に再発させない。
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "jstock_advisor"
        / "domain"
        / "financial_freshness.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            assert "fetched_at" not in arg_names, node.name
            assert "financial_observed_at" not in arg_names, node.name


# --- 18. verdict は3状態のみ --------------------------------------------------


def test_verdict_has_exactly_fresh_stale_unknown() -> None:
    """FAILURE / MISSING を持たない(Issue #59 と責務を重複させない)。"""
    assert {v.value for v in FinancialFreshnessVerdict} == {"FRESH", "STALE", "UNKNOWN"}


# --- 20. 接続先は Phase ごとに限定する ----------------------------------------


def test_production_call_sites_are_limited_to_the_current_phase() -> None:
    """接続先を Phase 単位で固定する。

    B3-A の時点では call site 0 だった(pure domain contract のみ)。
    B3-B1 で BUY へ、B3-B2 で SELL / 利確へ接続した。**接続のしかたは経路ごとに
    異なる**(BUY は共通 confidence score を持たないため警告のみ、SELL / 利確は
    既存の `compute_confidence` へ減点を接続する)ため、どこへ繋がっているかを
    ここで一元的に固定し、次の Phase で無自覚に広がらないようにする。

    接続のしかたそのものは各 Phase のテストが固定する。
    B3-B1(BUY)  tests/unit/test_issue_52_phase_b3_b1_buy_financial_freshness.py
    B3-B2(SELL / 利確)
                 tests/unit/test_issue_52_phase_b3_b2_sell_profit_financial_freshness.py
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
    importers = sorted(
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if path.name != "financial_freshness.py"
        and "financial_freshness" in path.read_text(encoding="utf-8")
    )
    assert importers == [
        # BUY(B3-B1): 警告のみ。共通 confidence score を持たない
        "services/buy_signal_service.py",
        # SELL / 利確(B3-B2)が共有する接続部分
        "services/financial_freshness_integration.py",
        "services/profit_taking_service.py",
        "services/sell_signal_service.py",
    ], f"unexpected call sites: {importers}"

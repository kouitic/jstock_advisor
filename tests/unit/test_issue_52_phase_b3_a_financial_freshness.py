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

# 年次サイクル。2024-03-31 の次は 2025-03-31、期限は +60日 = 2025-05-30。
_ANNUAL_PERIOD_END = _Q_2024_03
_EXPECTED_NEXT_ANNUAL = dt.date(2025, 3, 31)
_ANNUAL_DEADLINE = dt.date(2025, 5, 30)


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


def test_annual_cycle_before_deadline_is_fresh() -> None:
    result = _evaluate(
        latest=_ANNUAL_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=_ANNUAL_DEADLINE - dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.FRESH
    assert result.expected_next_period_end == _EXPECTED_NEXT_ANNUAL
    assert result.basis is ExpectedPeriodBasis.ANNUAL_CYCLE


def test_annual_cycle_after_deadline_is_stale() -> None:
    result = _evaluate(
        latest=_ANNUAL_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=_FY_END_MONTH_MARCH,
        evaluation_date=_ANNUAL_DEADLINE + dt.timedelta(days=1),
    )
    assert result.verdict is FinancialFreshnessVerdict.STALE


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
    """年次フォールバック由来の期末を四半期として扱わない。

    扱ってしまうと実在しない期末日を作り、STALE を誤検出する。
    決算期末月も無いためここでは推定できず UNKNOWN になる。
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
        latest=_ANNUAL_PERIOD_END,
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=None,
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_fiscal_year_end_month_contradiction_is_unknown() -> None:
    """決算期末月と直近期末の月が矛盾する(決算期変更の可能性)。"""
    result = _evaluate(
        latest=_ANNUAL_PERIOD_END,  # 3月期末
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=12,  # 12月決算だと主張している
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
        latest=dt.date(2024, 9, 30),  # 12月決算の会社が9月末で区切った移行期
        source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fy_end_month=12,
        evaluation_date=dt.date(2025, 12, 31),
    )
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


def test_missing_period_end_is_unknown() -> None:
    result = _evaluate(latest=None, evaluation_date=dt.date(2024, 12, 31))
    assert result.verdict is FinancialFreshnessVerdict.UNKNOWN


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


def test_leap_day_annual_cycle() -> None:
    """2月決算のうるう年。2024-02-29 の1年後は 2025-02-28(月末を維持)。"""
    resolved = resolve_expected_next_period_end(
        latest_financial_period_end=dt.date(2024, 2, 29),
        quarter_ends=(),
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
        fiscal_year_end_month=2,
        evaluation_date=dt.date(2024, 12, 31),
    )
    assert resolved.period_end == dt.date(2025, 2, 28)


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


# --- 20. Phase B3-A は挙動を変えない ------------------------------------------


def test_no_production_call_site() -> None:
    """判定経路から呼ばれていないことを固定する。

    B3-A は pure domain contract のみで、merge しても Production の挙動は
    変わらない。接続は Phase B3-B で行う。ここが破れたら、それは
    B3-A の範囲を超えた変更である。
    """
    src_root = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
    importers = [
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if path.name != "financial_freshness.py"
        and "financial_freshness" in path.read_text(encoding="utf-8")
    ]
    assert importers == [], f"unexpected call sites: {importers}"

"""Issue #66 F-L4: 長期保有優待の接近判定が UTC 暦日で比較していた。

`profit_taking_service._is_long_term_benefit_imminent()` は条件達成日と
`now.date()`（= **UTC 暦日**）を比較していた。保有銘柄分析の定期実行は
**08:00 JST = 前日 23:00 UTC** であるため、UTC 暦日は ★ **毎回 JST の前日**になり、
条件達成日までの残日数が常に 1 日ずれていた。

★ 同 service は同じ評価の中で `evaluation_date_jst(now)` を既に使っており
（期間末解決・関連性判定）、コメントも「各所で個別に
`now.date()` / `evaluation_date_jst(now)` を再計算しない」と定めていた。
★ **その集約から本 helper だけが取り残されていた**、というのが本 finding である。

★ 「毎日ずれている」＝「毎日誤判定」ではない。境界に当たる保有がある日にだけ
  緩和要因の成否が変わり、そこから最終 Action が変わりうる。
  本ファイルは ★ **ずれること**と ★ **ずれが Action に届きうること**の両方を固定する。

★ 実在の銘柄コード・銘柄名・所有者名は使用しない（架空値のみ）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType, BenefitUtilityCategory
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    _apply_mitigating_factors,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit
from jstock_advisor.services.profit_taking_service import _is_long_term_benefit_imminent

_CONFIG = load_config()
_STOCK_CODE = "0000"  # ★ 実在しない銘柄コード
_FIRST_PURCHASE = dt.date(2024, 3, 15)
# 24 か月後 = 2026-03-15 が条件達成日（`_add_months` の算出と一致させる）
_QUALIFY_DATE = dt.date(2026, 3, 15)
_CONDITION_MONTHS = 24


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, _STOCK_CODE),
        stock_code=_STOCK_CODE,
        stock_name="銘柄 X",
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("1200000"),
        first_purchase_date=_FIRST_PURCHASE,
        last_purchase_date=_FIRST_PURCHASE,
        account_type=AccountType.SPECIFIC,
        created_at=dt.datetime(2024, 3, 15, tzinfo=dt.UTC),
        updated_at=dt.datetime(2024, 3, 15, tzinfo=dt.UTC),
    )


def _benefit(condition_months: int | None = _CONDITION_MONTHS) -> ShareholderBenefit:
    return ShareholderBenefit(
        stock_code=_STOCK_CODE,
        min_shares_required=100,
        benefits=[
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_PRODUCT,
                description="長期保有優待（テスト用の架空値）",
                min_shares_for_tier=100,
                long_term_holding_condition_months=condition_months,
            )
        ],
        frequency_per_year=1,
        source=DataSourceReference(
            provider="test", fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        ),
    )


def _imminent_on(evaluation_date: dt.date) -> bool:
    return _is_long_term_benefit_imminent(_holding(), _benefit(), evaluation_date, _CONFIG)


def _within_days() -> int:
    mitigating = _CONFIG.profit_taking.mitigating_factors.long_term_holding_benefit_imminent
    assert mitigating.within_business_days is not None, "テストの前提: この要因は有効"
    return mitigating.within_business_days


# --- A: 評価日そのものによる境界 ---------------------------------------------------------


def test_a1_the_day_before_qualifying_is_imminent() -> None:
    """条件充足の**前日**は「近い」と判定されること。"""
    assert _imminent_on(_QUALIFY_DATE - dt.timedelta(days=1)) is True


def test_a2_the_qualifying_day_itself_is_imminent() -> None:
    """条件充足の**当日**（残り 0 日）も「近い」と判定されること。"""
    assert _imminent_on(_QUALIFY_DATE) is True


def test_a3_the_day_after_qualifying_is_not_imminent() -> None:
    """★ 条件充足の**翌日**は「近い」ではないこと（既に達成済みのため）。

    ★ ここが UTC/JST のずれで最も分かりやすく壊れる点である。
      UTC 暦日は JST の前日になるため、翌日に評価しても「当日」として
      True を返し続けてしまう。
    """
    assert _imminent_on(_QUALIFY_DATE + dt.timedelta(days=1)) is False


def test_a4_far_before_the_window_is_not_imminent() -> None:
    """接近ウィンドウの**外側**（十分前）は「近い」ではないこと。"""
    outside = _QUALIFY_DATE - dt.timedelta(days=_within_days() * 2 + 1)
    assert _imminent_on(outside) is False


def test_a5_the_window_boundary_is_inclusive() -> None:
    """接近ウィンドウの**内側の端**（ちょうど上限）は「近い」であること。"""
    boundary = _QUALIFY_DATE - dt.timedelta(days=_within_days() * 2)
    assert _imminent_on(boundary) is True


def test_a6_no_condition_months_is_never_imminent() -> None:
    """長期条件を持たない優待は常に False（月数算定は変えていない）。"""
    holding = _holding()
    benefit = _benefit(condition_months=None)
    assert _is_long_term_benefit_imminent(holding, benefit, _QUALIFY_DATE, _CONFIG) is False


# --- B: ★ JST 評価日で判定されること（本 finding の中心） --------------------------------


@pytest.mark.parametrize(
    ("now", "label"),
    [
        (dt.datetime(2026, 3, 15, 23, 0, tzinfo=dt.UTC), "23:00 UTC = 翌 08:00 JST（定期実行）"),
        (dt.datetime(2026, 3, 15, 15, 0, tzinfo=dt.UTC), "15:00 UTC = 翌 00:00 JST"),
        (dt.datetime(2026, 3, 15, 23, 59, tzinfo=dt.UTC), "23:59 UTC = 翌 08:59 JST"),
    ],
)
def test_b1_utc_evening_is_already_the_next_jst_day(now: dt.datetime, label: str) -> None:
    """★★ 本 finding の中心。

    UTC の夕方〜深夜は **JST では翌日**である。`evaluation_date_jst()` を通せば
    翌日（= 条件充足の翌日）となり ★ **False** になるが、`now.date()`（UTC 暦日）を
    使うと当日のままで ★ **True** を返してしまう。

    ★ 特に **23:00 UTC は 08:00 JST の定期実行そのもの**であり、
      ★ この 1 ケースが**毎営業日**通る経路である。
    """
    jst_date = evaluation_date_jst(now)
    assert jst_date == _QUALIFY_DATE + dt.timedelta(days=1), label
    # JST 評価日で判定すれば「翌日 = もう近くない」
    assert _imminent_on(jst_date) is False
    # ★ 修正前の実装（UTC 暦日）だと当日扱いになり True になっていた
    assert _imminent_on(now.date()) is True


def test_b2_jst_0900_is_the_same_calendar_day_in_both(  # noqa: D103
) -> None:
    """09:00 JST（= 00:00 UTC 同日）は UTC 暦日と JST 暦日が**一致**する。

    ★ 「常にずれる」わけではないことを固定する（ずれるのは UTC 15:00 以降）。
    """
    now = dt.datetime(2026, 3, 16, 0, 0, tzinfo=dt.UTC)  # = 09:00 JST 同日
    assert evaluation_date_jst(now) == now.date()
    assert _imminent_on(evaluation_date_jst(now)) is _imminent_on(now.date())


def test_b3_the_service_passes_a_date_not_a_datetime() -> None:
    """★ helper が受け取るのは `dt.date`（評価日）であって `dt.datetime` ではないこと。

    ★ signature を戻す（`now: dt.datetime` に戻す）と、この test が落ちる。
    """
    import inspect

    params = inspect.signature(_is_long_term_benefit_imminent).parameters
    assert "evaluation_date" in params, "評価日を受け取る引数名であること"
    assert params["evaluation_date"].annotation == "dt.date"
    assert "now" not in params, "★ now(datetime) を直接受け取ってはならない"


# --- C: ★ ずれが最終 Action へ届きうること / floor で吸収されること ----------------------


def _downgrade_levels() -> int:
    factor = _CONFIG.profit_taking.mitigating_factors.long_term_holding_benefit_imminent
    return factor.downgrade_levels


def _apply(level: int, imminent: bool) -> int:
    new_level, _applied = _apply_mitigating_factors(
        level,  # type: ignore[arg-type] - _Level は IntEnum 互換の内部型
        MitigatingFactorInputs(long_term_holding_benefit_imminent=imminent),
        _CONFIG.profit_taking.mitigating_factors,
    )
    return int(new_level)


def test_c1_the_mitigation_changes_the_level_when_there_is_room() -> None:
    """★ 緩和が**残る**ケース: 判定レベルに余裕があると、1 日のずれが Action を変える。

    ★ = 「接近している/していない」の差が、そのまま降格の有無になる。
    """
    downgrade = _downgrade_levels()
    assert downgrade >= 1, "テストの前提: この要因は降格量を持つ"
    level = downgrade + 1  # 降格しても 0 にならない高さ

    assert _apply(level, imminent=True) == level - downgrade
    assert _apply(level, imminent=False) == level
    assert _apply(level, imminent=True) != _apply(level, imminent=False)


def test_c2_the_floor_absorbs_the_difference_at_the_bottom() -> None:
    """★ **floor で吸収される**ケース: 既に最下位なら、1 日ずれても結果は同じ。

    `_apply_mitigating_factors` は `max(0, level - downgrade)` で下限を持つ。
    ★ したがって「毎日ずれている」ことが ★ **毎日 Action を変えるわけではない**。
      本 finding の影響範囲を過大に読まないための固定である。
    """
    assert _apply(0, imminent=True) == 0
    assert _apply(0, imminent=False) == 0
    assert _apply(0, imminent=True) == _apply(0, imminent=False)


def test_c3_the_mitigation_is_recorded_even_when_absorbed() -> None:
    """★ floor で吸収されても、該当した緩和要因は**記録される**こと。

    「効かなかった」と「該当しなかった」を混同しないため（既存挙動の確認）。
    """
    _new_level, applied = _apply_mitigating_factors(
        0,  # type: ignore[arg-type]
        MitigatingFactorInputs(long_term_holding_benefit_imminent=True),
        _CONFIG.profit_taking.mitigating_factors,
    )
    assert any("長期保有優待" in reason for reason in applied)

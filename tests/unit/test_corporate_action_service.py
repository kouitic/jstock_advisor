import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import CorporateActionType
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.services.corporate_action_service import (
    CorporateActionService,
    MismatchedAdjustmentBasisDateError,
    NonIntegerShareAdjustmentError,
)

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


class _FakeCorporateActionProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.stock_code == stock_code]


def _split_event(stock_code: str, effective_date: dt.date, ratio: str) -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code=stock_code,
        event_type=CorporateActionType.SPLIT,
        announced_date=effective_date,
        effective_date=effective_date,
        ratio=Decimal(ratio),
        source=_SOURCE,
    )


def _event(
    stock_code: str,
    event_type: CorporateActionType,
    effective_date: dt.date,
    ratio: str | None,
) -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code=stock_code,
        event_type=event_type,
        announced_date=effective_date,
        effective_date=effective_date,
        ratio=Decimal(ratio) if ratio is not None else None,
        source=_SOURCE,
    )


def test_cumulative_split_factor_is_one_when_no_split_in_range() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    factor = service.cumulative_split_factor("5401", dt.date(2026, 1, 1), dt.date(2026, 7, 27))
    assert factor == Decimal("1")


def test_adjust_price_for_2for1_split() -> None:
    events = [_split_event("5401", dt.date(2026, 3, 1), "2")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    result = service.adjust_price(
        raw=Decimal("3500"),
        stock_code="5401",
        value_date=dt.date(2026, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    assert result.adjustment_factor == Decimal("2")
    assert result.adjusted_value == Decimal("1750")
    assert result.raw_value == Decimal("3500")


def test_adjust_eps_for_2for1_split() -> None:
    events = [_split_event("5401", dt.date(2026, 3, 1), "2")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    result = service.adjust_per_share_metric(
        raw=Decimal("100"),
        stock_code="5401",
        value_date=dt.date(2026, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    assert result.adjusted_value == Decimal("50")


def test_adjust_dps_for_5for1_split_matches_nippon_steel_case() -> None:
    # 5401日本製鉄: 2025-10-01に1:5分割
    events = [_split_event("5401", dt.date(2025, 10, 1), "5")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    # 分割前基準の年間配当32円は、分割後基準では6.4円相当
    result = service.adjust_per_share_metric(
        raw=Decimal("32"),
        stock_code="5401",
        value_date=dt.date(2024, 3, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    assert result.adjusted_value == Decimal("6.4")


def test_adjust_shares_across_two_splits_compounds_factors() -> None:
    events = [
        _split_event("7203", dt.date(2024, 6, 1), "2"),
        _split_event("7203", dt.date(2025, 6, 1), "3"),
    ]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    result = service.adjust_shares(
        raw=100,
        stock_code="7203",
        value_date=dt.date(2024, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    # 2倍 x 3倍 = 6倍
    assert result.adjustment_factor == Decimal("6")
    assert result.adjusted_value == 600


def test_adjust_shares_raises_when_result_not_integer() -> None:
    # 1.5倍(3対2分割)は、保有株数が奇数だと分割後株数が整数にならない
    events = [_split_event("9999", dt.date(2026, 3, 1), "1.5")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    with pytest.raises(NonIntegerShareAdjustmentError):
        service.adjust_shares(
            raw=7,
            stock_code="9999",
            value_date=dt.date(2026, 1, 1),
            basis_date=dt.date(2026, 7, 27),
            source=_SOURCE,
        )


def test_double_adjustment_prevented_when_already_at_basis_date() -> None:
    """既にbasis_date時点の値を、同じbasis_dateへ再度調整しても変化しない(二重適用防止)。"""
    events = [_split_event("5401", dt.date(2025, 10, 1), "5")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    once = service.adjust_price(
        raw=Decimal("3500"),
        stock_code="5401",
        value_date=dt.date(2024, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    twice = service.adjust_price(
        raw=once.adjusted_value,
        stock_code="5401",
        value_date=dt.date(2026, 7, 27),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    assert twice.adjustment_factor == Decimal("1")
    assert twice.adjusted_value == once.adjusted_value


def test_require_matching_basis_dates_raises_on_mismatch() -> None:
    events = [_split_event("5401", dt.date(2025, 10, 1), "5")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    a = service.adjust_price(
        raw=Decimal("100"),
        stock_code="5401",
        value_date=dt.date(2024, 1, 1),
        basis_date=dt.date(2026, 1, 1),
        source=_SOURCE,
    )
    b = service.adjust_price(
        raw=Decimal("100"),
        stock_code="5401",
        value_date=dt.date(2024, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    with pytest.raises(MismatchedAdjustmentBasisDateError):
        service.require_matching_basis_dates(a, b)


def test_require_matching_basis_dates_passes_when_same() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    a = service.adjust_price(
        raw=Decimal("100"),
        stock_code="5401",
        value_date=dt.date(2026, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    b = service.adjust_price(
        raw=Decimal("200"),
        stock_code="5401",
        value_date=dt.date(2026, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    service.require_matching_basis_dates(a, b)  # 例外を送出しなければ成功


def test_cumulative_split_factor_reverse_direction_is_correctly_inverted() -> None:
    # from_date > to_date(post-split基準からpre-split基準へ逆方向に調整する)場合、
    # raw_value/factorが常に正しく機能するよう、係数の向きを反転する必要がある。
    events = [_split_event("5401", dt.date(2025, 10, 1), "5")]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    # 順方向: pre-split(2024)からpost-split(2026)へ。3500円 -> 700円
    forward_price = service.adjust_price(
        raw=Decimal("3500"),
        stock_code="5401",
        value_date=dt.date(2024, 1, 1),
        basis_date=dt.date(2026, 7, 27),
        source=_SOURCE,
    )
    assert forward_price.adjusted_value == Decimal("700")

    # 逆方向: post-split(2026)からpre-split(2024)基準へ。700円 -> 3500円相当に戻る
    reverse_price = service.adjust_price(
        raw=Decimal("700"),
        stock_code="5401",
        value_date=dt.date(2026, 7, 27),
        basis_date=dt.date(2024, 1, 1),
        source=_SOURCE,
    )
    assert reverse_price.adjusted_value == Decimal("3500")


def test_adjust_total_metric_never_adjusted_by_splits() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    result = service.adjust_total_metric(
        raw=Decimal("1000000000"), source=_SOURCE, basis_date=dt.date(2026, 7, 27)
    )
    assert result.adjustment_factor == Decimal("1")
    assert result.adjusted_value == result.raw_value


# ===== コードレビュー修正1: 1株当たり指標の調整対象イベント判定の一元化 =====
# クロスバリデーション側が独自にratio有無だけで「配当基準を変更するイベント」を
# 分類していたため、cumulative_split_factor()が対象とする種別(SPLIT/
# REVERSE_SPLIT/FREE_ALLOTMENT)と定義がズレる可能性があった。判定をここへ
# 一元化するpublic APIを追加する。


def test_is_per_share_adjustment_event_true_for_split_reverse_split_free_allotment() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    for event_type in (
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.FREE_ALLOTMENT,
    ):
        event = _event("5401", event_type, dt.date(2026, 3, 1), "2")
        assert service.is_per_share_adjustment_event(event) is True


def test_is_per_share_adjustment_event_false_for_merger_even_with_ratio() -> None:
    """MERGER等、ratioを持っていてもSPLIT/REVERSE_SPLIT/FREE_ALLOTMENT以外の
    イベント種別は1株当たり指標の調整対象ではない。"""
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    event = _event("5401", CorporateActionType.MERGER, dt.date(2026, 3, 1), "2")
    assert service.is_per_share_adjustment_event(event) is False


def test_is_per_share_adjustment_event_false_without_ratio_or_effective_date() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    no_ratio = _event("5401", CorporateActionType.SPLIT, dt.date(2026, 3, 1), None)
    assert service.is_per_share_adjustment_event(no_ratio) is False


def test_get_ratio_adjustment_events_filters_out_non_ratio_event_types() -> None:
    service = CorporateActionService(_FakeCorporateActionProvider([]), now=_NOW)
    split = _event("5401", CorporateActionType.SPLIT, dt.date(2026, 3, 1), "2")
    reverse_split = _event("5401", CorporateActionType.REVERSE_SPLIT, dt.date(2026, 4, 1), "0.5")
    free_allotment = _event("5401", CorporateActionType.FREE_ALLOTMENT, dt.date(2026, 5, 1), "1.1")
    merger = _event("5401", CorporateActionType.MERGER, dt.date(2026, 6, 1), "3")
    ticker_change = _event("5401", CorporateActionType.TICKER_CHANGE, dt.date(2026, 7, 1), None)

    result = service.get_ratio_adjustment_events(
        [split, reverse_split, free_allotment, merger, ticker_change]
    )

    assert result == [split, reverse_split, free_allotment]


def test_cumulative_split_factor_ignores_merger_ratio() -> None:
    """cumulative_split_factor()もget_ratio_adjustment_events()経由で判定するため、
    MERGERイベントのratioは分割係数へ混入しない(既存の分割係数計算ロジックの回帰確認)。"""
    events = [
        _split_event("5401", dt.date(2026, 3, 1), "2"),
        _event("5401", CorporateActionType.MERGER, dt.date(2026, 4, 1), "3"),
    ]
    service = CorporateActionService(_FakeCorporateActionProvider(events), now=_NOW)
    factor = service.cumulative_split_factor("5401", dt.date(2026, 1, 1), dt.date(2026, 7, 27))
    assert factor == Decimal("2")  # MERGERの3倍は無視され、SPLITの2倍のみ反映

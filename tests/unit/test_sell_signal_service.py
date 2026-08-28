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
import inspect

from jstock_advisor.services import sell_signal_service as sell_signal_service_module
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


def test_sell_signal_service_does_not_reference_earnings_release_gating() -> None:
    """SELL/URGENT_REVIEW系は決算発表確認待ち(AWAITING_CONFIRMATION/DELAYED)による
    抑制の対象外(デプロイ前対応§8: 投資前提悪化の確定的シグナルは決算タイミングで
    抑制しない)。sell_signal_service.pyがresolve_earnings_release_confirmation等を
    一切参照していないことを確認し、将来の変更で誤って組み込まれないための
    構造的な回帰ガードとする。
    """
    source = inspect.getsource(sell_signal_service_module)
    assert "resolve_earnings_release_confirmation" not in source
    assert "resolve_earnings_decision_relevance" not in source
    assert "EarningsReleaseConfirmationState" not in source
    assert "EarningsDecisionRelevance" not in source


# --- Issue #30 Phase 1: is_progressive_or_doe_policyの3状態化(bool | None) ---
# SELL側の反対材料評価(_evaluate_counter_factors)の既存互換semanticsを固定する:
# True=従来どおり、False=従来どおり、None=従来の「取得不能時False」と同一結果。
# 評価coverage(dividend_policy_maintained)もFalse/Noneで「評価できず」のまま
# (案b: is not None は不採用。coverage semanticsは変更しない)。


def _counter_factor_snapshot(policy: bool | None):
    from decimal import Decimal
    from types import SimpleNamespace

    from jstock_advisor.domain.entities.common import DataSourceReference
    from jstock_advisor.interfaces.types import DividendInfo

    now = dt.datetime(2026, 8, 28, tzinfo=dt.UTC)
    dividend = DividendInfo(
        stock_code="9433",
        fiscal_year="2026",
        is_progressive_or_doe_policy=policy,
        consecutive_dividend_increase_years=None,
        source=DataSourceReference(provider="test", fetched_at=now),
    )
    return SimpleNamespace(
        quarterly_operating_income_periods=[
            SimpleNamespace(value=Decimal("100")),
            SimpleNamespace(value=Decimal("90")),
        ],
        disclosures=[],
        dividend=dividend,
        financial=SimpleNamespace(
            sector="Technology", industry="Consumer Electronics", equity_ratio_pct=50.0
        ),
        cashflow_decomposition=None,
        momentum=SimpleNamespace(ma20=None),
    )


def test_counter_factor_policy_true_behaves_as_before() -> None:
    factors, _ = sell_signal_service_module._evaluate_counter_factors(  # noqa: SLF001
        _counter_factor_snapshot(True), triggered_count=2
    )
    assert "累進的配当方針・配当下限方針が維持されている" in factors


def test_counter_factor_policy_false_behaves_as_before() -> None:
    factors, evaluated = sell_signal_service_module._evaluate_counter_factors(  # noqa: SLF001
        _counter_factor_snapshot(False), triggered_count=2
    )
    assert "累進的配当方針・配当下限方針が維持されている" not in factors
    assert evaluated is False  # 方針False+下限方針不明は従来どおり「評価できず」


def test_counter_factor_policy_none_identical_to_false() -> None:
    """None(UNKNOWN)はFalseと完全に同一の結果(SELL側判定を一切変えない)。"""
    false_result = sell_signal_service_module._evaluate_counter_factors(  # noqa: SLF001
        _counter_factor_snapshot(False), triggered_count=2
    )
    none_result = sell_signal_service_module._evaluate_counter_factors(  # noqa: SLF001
        _counter_factor_snapshot(None), triggered_count=2
    )
    assert none_result == false_result
    assert "累進的配当方針・配当下限方針が維持されている" not in none_result[0]

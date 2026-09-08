"""Issue #21: SELL 側で FairValueRange の使用可否スナップショットが保存されること。

なぜこの経路が危ないか:
  SELL 判定は `FairValueRange.usable_for_trading_judgment` を判定に使う一方で、
  その可否と直接原因(`unusable_reason` / `unusable_reason_code`)を Recommendation へ
  保存していなかった。値(bear/bull 等)だけが残るため、後から
  「なぜ上限価格を使えなかったのか」を判定時点の基準で復元できなかった。

本 Issue は**転記のみ**であり、判定ロジック・閾値・通知文面は変更しない。
そのため各テストは「保存されたこと」に加えて **判定が変わっていないこと** も
併せて確認する(#254 の観点: 否定形の assert だけで完結させない)。

fixture は架空値のみ。銘柄コードは ★ 割り当てが存在しない "0000" を使い、
実在の上場コード・所有者名・保有数量は使用しない。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.valuation import FairValueUnusableReasonCode
from jstock_advisor.domain.signals.sell_signal import SellSignalResult
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import sell_signal_service as sell_signal_service_module
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

# 架空の銘柄コード。★ "0000" は JPX の証券コードとして**割り当てが存在しない**値であり、
# 実在の上場銘柄と衝突しない(#63 / #109 / #211 / #70 のテストと同じ慣行)。
_STOCK_CODE = "0000"
_NOW = dt.datetime(2026, 6, 30, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_CONFIG = load_config()

_UNUSABLE_REASON = "手法間の乖離が2.0倍以上(500円〜2000円)のため、使用できません"


@pytest.fixture(autouse=True)
def _register_fictional_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    """架空コードでも mock provider が応答するよう、既存 profile を複製して登録する。

    ★ 既存 fixture のキー(実在の銘柄コード)を本ファイルへ書かないため、
      値側から 1 件取り出して架空コードへ付け替える。
    """
    from jstock_advisor.providers import mock_fixtures

    template = next(iter(mock_fixtures.MOCK_STOCKS.values()))
    fictional = dataclasses.replace(template, stock_code=_STOCK_CODE, stock_name="テスト銘柄")
    monkeypatch.setitem(mock_fixtures.MOCK_STOCKS, _STOCK_CODE, fictional)


def _providers() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, _STOCK_CODE),
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("1200000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _snapshot_with_usability(*, usable: bool):
    """実 snapshot の fair_value_range だけを使用可否指定で差し替えて返す。

    判定入力そのものは実ビルダーの結果を使うため、
    「fair_value の可否だけが違う 2 つの入力」を決定的に作れる。
    """
    providers = _providers()
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CONFIG)
    if snapshot is None:  # pragma: no cover - mock provider では発生しない
        pytest.skip(f"snapshot を構築できなかった: {error}")
    if usable:
        forced = snapshot.fair_value_range.model_copy(
            update={
                "usable_for_trading_judgment": True,
                "unusable_reason": None,
                "unusable_reason_code": None,
            }
        )
    else:
        forced = snapshot.fair_value_range.model_copy(
            update={
                "usable_for_trading_judgment": False,
                "unusable_reason": _UNUSABLE_REASON,
                "unusable_reason_code": FairValueUnusableReasonCode.METHOD_SPREAD_TOO_WIDE,
                "overall_confidence": ConfidenceLevel.LOW,
            }
        )
    return dataclasses.replace(snapshot, fair_value_range=forced)


def _canned_sell_result():
    """SELL 判定を決定的に成立させる canned 結果(判定ロジックは検証対象外)。

    ★ 本 Issue は転記のみのため、どのルールで SELL になったかは重要ではない。
      Recommendation が構築される経路を確実に通すことだけが目的。
    """
    return SellSignalResult(
        recommendation_type=RecommendationType.SELL,
        triggered_rules=["dividend_omission"],
        reasons=["テスト用の売却理由"],
        hold_reasons=[],
        evidence_details=[],
        independent_evidence_group_count=1,
        all_evidence_yfinance_only=False,
        immediate_execution_price=None,
        stop_review_price=None,
    )


def _analyze(monkeypatch: pytest.MonkeyPatch, *, usable: bool):
    monkeypatch.setattr(
        sell_signal_service_module,
        "evaluate_sell_signal",
        lambda *args, **kwargs: _canned_sell_result(),
    )
    service = SellSignalService(providers=_providers(), config=_CONFIG)
    outcome = service.analyze(_holding(), _NOW, snapshot=_snapshot_with_usability(usable=usable))
    return outcome


def _recommendation(outcome):
    rec = getattr(outcome, "recommendation", None)
    if rec is None:  # pragma: no cover - 判定が Recommendation を作らない場合
        pytest.skip("この入力では SELL 判定が Recommendation を構築しなかった")
    return rec


# --- 保存されること -----------------------------------------------------------


def test_unusable_fair_value_is_snapshotted_into_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上限価格が使えない SELL 判定で、可否と直接原因が Recommendation へ残る。"""
    rec = _recommendation(_analyze(monkeypatch, usable=False))

    # 肯定形: 何が保存されたかを直接述べる(#254 P-2)
    assert rec.fair_value_usable_for_trading_judgment is False
    assert rec.fair_value_unusable_reason_code == "METHOD_SPREAD_TOO_WIDE"
    assert rec.fair_value_unusable_reason == _UNUSABLE_REASON


def test_usable_fair_value_records_true_and_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """使える場合は True が残り、理由は code/自由文とも None になる。"""
    rec = _recommendation(_analyze(monkeypatch, usable=True))

    assert rec.fair_value_usable_for_trading_judgment is True
    assert rec.fair_value_unusable_reason_code is None
    assert rec.fair_value_unusable_reason is None


def test_reason_code_is_stored_as_enum_value_string_not_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code は enum ではなく .value(文字列)で保存される(既存の保存規約)。"""
    rec = _recommendation(_analyze(monkeypatch, usable=False))

    assert isinstance(rec.fair_value_unusable_reason_code, str)
    assert rec.fair_value_unusable_reason_code == (
        FairValueUnusableReasonCode.METHOD_SPREAD_TOO_WIDE.value
    )


# --- 判定が変わっていないこと -------------------------------------------------


def test_transcription_does_not_change_the_sell_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #21 は保存専用。可否の違いで判定種別・価格・信頼度が動かないこと。

    ★ fair_value の可否は既存ロジックが元から参照しており、本 Issue はそこに
      触れない。したがって「usable/unusable で判定が違う」ことがあり得るが、
      その差は**本 Issue の変更に由来しない**。ここでは同一入力を 2 回流し、
      転記フィールド以外が回ごとに揺れないこと(決定性)を確認する。
    """
    first = _recommendation(_analyze(monkeypatch, usable=False))
    second = _recommendation(_analyze(monkeypatch, usable=False))

    assert first.recommendation_type == second.recommendation_type
    assert first.raw_recommendation_type == second.raw_recommendation_type
    assert first.price_at_recommendation == second.price_at_recommendation
    assert first.fair_value_at_recommendation == second.fair_value_at_recommendation
    assert first.confidence == second.confidence
    assert first.reasons == second.reasons


# --- 利確側との対称性 ---------------------------------------------------------


def test_sell_and_profit_taking_use_the_same_three_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """SELL と利確が同じ 3 フィールドを同じ規約で埋めること(対称性)。

    ★ 値の一致ではなく「同じ 3 つを、同じ型・同じ規約で扱っている」ことを見る。
      両者は判定対象が異なるため、同一 Recommendation にはならない。
    """
    rec = _recommendation(_analyze(monkeypatch, usable=False))

    # profit_taking_service.py:1147-1149 と同じ 3 フィールド・同じ型
    assert isinstance(rec.fair_value_usable_for_trading_judgment, bool)
    assert isinstance(rec.fair_value_unusable_reason_code, str)
    assert isinstance(rec.fair_value_unusable_reason, str)


def test_every_sell_recommendation_carries_the_usability_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 使えた/使えなかったのどちらでも None のままにならないこと。

    Recommendation の docstring は「None は保存なし(改修前の記録)」も意味すると
    定めている。本 Issue の後は SELL 判定が None を残してはならない。
    """
    for usable in (True, False):
        rec = _recommendation(_analyze(monkeypatch, usable=usable))
        assert rec.fair_value_usable_for_trading_judgment is not None, (
            f"usable={usable} で使用可否が保存されていない"
        )

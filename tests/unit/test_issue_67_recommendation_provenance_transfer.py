"""Issue #67 F-I1 / F-I4 / F-I5: 旧 SELL・保有判断が判定入力の付随情報を転記しない。

なぜこの 3 件が 1 つのファイルにまとまるのか:
  いずれも「**同じ snapshot を消費しているのに、その事実を Recommendation へ
  書き写していない**」という同型の欠陥であり、修正箇所も同じ 2 module の同じ
  constructor である。別々に直すと同じ場所を 3 回触ることになる。

  F-I1  `financial_input_provenance`（判定に使った財務データの出所）
  F-I4  `earnings_date_status` / `earnings_date_raw`（決算日の確度と生値）
  F-I5  `benefit_record_date_recurring_label` / `benefit_record_date_source_type`

★ **転記のみ**である。取得し直さない・再解決しない・過去レコードを埋め直さない。
  そのため各テストは「保存されたこと」だけでなく ★ **判定と通知文面が変わって
  いないこと**も併せて固定する（#254 の観点: 否定形の assert だけで完結させない）。

★ 「全 5 経路で全フィールドが一致すること」を強制するテストは ★ **置かない**。
  経路ごとに意図的な非対称があり（集中度は財務入力を消費しない等）、
  一致を強制すると ★ **その意図を壊す**。代わりに下の `_APPLICABILITY`
  （適用表）で経路ごとの必須 / 条件付き / 非該当を宣言し、
  ★ **「非該当には理由がある」ことを機械的に検査する**。

fixture は架空値のみ。銘柄コードは ★ 割り当てが存在しない "0000" を使い、
実在の上場コード・所有者名・保有数量は使用しない。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    AccountType,
    BenefitUtilityCategory,
    EarningsDateStatus,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    NotificationType,
    PriceRangeEvaluationState,
    RecommendationType,
    RecordDateUnknownReason,
    SourceType,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.sell_signal import SellSignalResult
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import line_notification_service as line_module
from jstock_advisor.services import sell_signal_service as sell_signal_service_module
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

# ★ "0000" は JPX の証券コードとして割り当てが存在しない値であり、実在銘柄と衝突しない。
_STOCK_CODE = "0000"
_NOW = dt.datetime(2026, 6, 30, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_CONFIG = load_config()

# 本ファイルで転記を検査する 5 フィールド。
_TRANSFERRED_FIELDS = (
    "financial_input_provenance",
    "earnings_date_status",
    "earnings_date_raw",
    "benefit_record_date_recurring_label",
    "benefit_record_date_source_type",
)


# --- 適用表（設計案 #49 issuecomment-5626653624 の「適用表」） -----------------------
#
# ★ 置き場所をテスト側にした理由: docs へ置くと実装との乖離を誰も検出できない。
#   ここに置けば「表に無い経路・理由の無い非該当」は ★ **CI が落として教えてくれる**。
#
# 分類の意味
#   必須      その経路は同じ入力を消費しており、★ 転記しなければ欠陥である
#   条件付き  入力が存在する場合のみ転記する（入力が無ければ None が正しい）
#   非該当    ★ その経路はその入力を消費していない。★ **理由を必ず書く**

_ROUTES = ("BUY", "SELL_LEGACY", "PROFIT_TAKING", "HOLDING_DECISION", "CONCENTRATION")

_NOT_CONSUMED_FINANCIAL = (
    "集中度判定はポートフォリオの構成比のみを入力とし、銘柄の財務 snapshot を"
    "消費しない（holdings_watchlist_handler が構成比から直接組み立てる）。"
    "消費していない入力の出所を書くと、出所の意味が壊れる。"
)

_APPLICABILITY: dict[str, dict[str, tuple[str, str | None]]] = {
    "financial_input_provenance": {
        "BUY": ("必須", None),
        "SELL_LEGACY": ("必須", None),
        "PROFIT_TAKING": ("必須", None),
        "HOLDING_DECISION": ("必須", None),
        "CONCENTRATION": ("非該当", _NOT_CONSUMED_FINANCIAL),
    },
    "earnings_date_status": {
        "BUY": ("必須", None),
        "SELL_LEGACY": ("必須", None),
        "PROFIT_TAKING": ("必須", None),
        "HOLDING_DECISION": ("必須", None),
        "CONCENTRATION": ("非該当", _NOT_CONSUMED_FINANCIAL),
    },
    "earnings_date_raw": {
        # ★ raw は「検証前の生値」であり、status と対で意味を持つ。
        #   status が UNAVAILABLE でも raw が残ることがある（取得はできたが検証を通らない）。
        "BUY": ("条件付き", "入力に raw が無ければ None のままにする"),
        "SELL_LEGACY": ("条件付き", "入力に raw が無ければ None のままにする"),
        "PROFIT_TAKING": ("条件付き", "入力に raw が無ければ None のままにする"),
        "HOLDING_DECISION": ("条件付き", "入力に raw が無ければ None のままにする"),
        "CONCENTRATION": ("非該当", _NOT_CONSUMED_FINANCIAL),
    },
    "benefit_record_date_recurring_label": {
        "BUY": ("条件付き", "優待が無い銘柄・確定日がある銘柄では None が正しい"),
        "SELL_LEGACY": ("条件付き", "同上"),
        "PROFIT_TAKING": ("条件付き", "同上"),
        "HOLDING_DECISION": ("条件付き", "同上"),
        "CONCENTRATION": ("非該当", _NOT_CONSUMED_FINANCIAL),
    },
    "benefit_record_date_source_type": {
        "BUY": ("条件付き", "確定日または登録済み周期がある場合のみ付与する"),
        "SELL_LEGACY": ("条件付き", "同上"),
        "PROFIT_TAKING": ("条件付き", "同上"),
        "HOLDING_DECISION": ("条件付き", "同上"),
        "CONCENTRATION": ("非該当", _NOT_CONSUMED_FINANCIAL),
    },
}

# ★ 本ファイルが実装を検査する経路（BUY / 利確は既に転記済みで、本 Issue の対象外）。
_ROUTES_UNDER_TEST = ("SELL_LEGACY", "HOLDING_DECISION")


# --- 適用表そのものの検査（★ 「全フィールド一致」ではない） -------------------------


@pytest.mark.parametrize("field", _TRANSFERRED_FIELDS)
def test_applicability_table_covers_every_route(field: str) -> None:
    """適用表が 5 経路すべてを宣言していること（経路を足したら表も足す）。"""
    assert tuple(_APPLICABILITY[field]) == _ROUTES


@pytest.mark.parametrize("field", _TRANSFERRED_FIELDS)
def test_every_not_applicable_entry_states_a_reason(field: str) -> None:
    """★ 非該当には必ず理由が書かれていること。

    ★ 理由の無い「非該当」は、実装漏れを「仕様です」と言い換えたものと
      区別できない。#60 A-4 で実際に起きた形である。
    """
    for route, (classification, reason) in _APPLICABILITY[field].items():
        if classification == "非該当":
            assert reason, f"{field} / {route} の非該当に理由がない"


def test_classifications_are_from_the_defined_vocabulary() -> None:
    """分類語彙を 3 つに固定する（「たぶん不要」等の曖昧な語を混ぜない）。"""
    seen = {c for row in _APPLICABILITY.values() for c, _ in row.values()}
    assert seen <= {"必須", "条件付き", "非該当"}


# --- 共通 fixture -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_fictional_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    """架空コードでも mock provider が応答するよう、既存 profile を複製して登録する。"""
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


def _base_snapshot():
    snapshot, error = build_stock_snapshot(_providers(), _STOCK_CODE, _NOW, _CONFIG)
    if snapshot is None:  # pragma: no cover - mock provider では発生しない
        pytest.skip(f"snapshot を構築できなかった: {error}")
    return snapshot


def _benefit(
    *,
    record_dates: list[dt.date] | None = None,
    recurrence_months: list[int] | None = None,
    unknown_reason: RecordDateUnknownReason | None = None,
    source_type: SourceType = SourceType.COMPANY_IR,
) -> ShareholderBenefit:
    return ShareholderBenefit(
        stock_code=_STOCK_CODE,
        min_shares_required=100,
        benefits=[
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_PRODUCT,
                description="テスト用の架空の優待",
                min_shares_for_tier=100,
            )
        ],
        frequency_per_year=1,
        benefit_record_dates=record_dates or [],
        benefit_record_date_recurrence_months=recurrence_months or [],
        benefit_record_date_unknown_reason=unknown_reason,
        source=DataSourceReference(
            provider="test",
            fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            source_type=source_type,
        ),
    )


def _canned_sell_result() -> SellSignalResult:
    """SELL 判定を決定的に成立させる canned 結果（判定ロジックは検証対象外）。"""
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


def _holding_decision_result() -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id="issue-67-transfer-test",
        holding_id=build_holding_id(DEFAULT_OWNER, _STOCK_CODE),
        stock_code=_STOCK_CODE,
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


_NOT_EVALUATED_EXIT_PRICE_RANGE = ExitPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOW,
    model_version="exit_price_range_v1",
)


def _sell_recommendation(monkeypatch: pytest.MonkeyPatch, snapshot):
    monkeypatch.setattr(
        sell_signal_service_module,
        "evaluate_sell_signal",
        lambda *args, **kwargs: _canned_sell_result(),
    )
    service = SellSignalService(providers=_providers(), config=_CONFIG)
    outcome = service.analyze(_holding(), _NOW, snapshot=snapshot)
    rec = getattr(outcome, "recommendation", None)
    if rec is None:  # pragma: no cover - canned 結果では SELL になる
        pytest.skip("この入力では SELL 判定が Recommendation を構築しなかった")
    return rec


def _holding_recommendation(snapshot):
    return build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )


def _both_routes(monkeypatch: pytest.MonkeyPatch, snapshot) -> dict[str, object]:
    """同じ snapshot を 2 経路へ通し、経路名 -> Recommendation で返す。"""
    return {
        "SELL_LEGACY": _sell_recommendation(monkeypatch, snapshot),
        "HOLDING_DECISION": _holding_recommendation(snapshot),
    }


# --- F-I1: financial_input_provenance -----------------------------------------------


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i1_provenance_is_transferred_from_the_consumed_snapshot(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 消費した snapshot の provenance が、そのまま Recommendation に残ること。"""
    snapshot = _base_snapshot()
    assert snapshot.financial_input_provenance is not None, (
        "テストの前提: mock provider の snapshot は provenance を持つ"
    )

    rec = _both_routes(monkeypatch, snapshot)[route]

    # 肯定形: 何が保存されたかを直接述べる
    assert rec.financial_input_provenance == snapshot.financial_input_provenance


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i1_missing_input_stays_none_and_is_not_refetched(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 入力自体が未取得のケースと、転記漏れを区別する。

    ★ 入力が None のときに Recommendation が非 None になったら、それは
      「取り直している」ということであり、本 Issue の設計に反する。
    """
    snapshot = dataclasses.replace(_base_snapshot(), financial_input_provenance=None)

    rec = _both_routes(monkeypatch, snapshot)[route]

    assert rec.financial_input_provenance is None


# --- F-I4: earnings_date_status / raw ------------------------------------------------

_D = dt.date(2026, 8, 14)
_PAST = dt.date(2026, 1, 15)

# ★ 決算日の 4 入力。
#   ★ 「解釈不能」は enum 上の独立した値ではなく、★ **status=UNAVAILABLE かつ raw が残る**
#     という組で表れる（取得はできたが検証を通らなかった）。
#     ★ したがって「欠落」とは raw の有無で区別する。この区別が消えないことを固定する。
_EARNINGS_INPUTS = [
    pytest.param(EarningsDateStatus.CONFIRMED, _D, _D, id="確定"),
    pytest.param(EarningsDateStatus.STALE_PAST_DATE, _PAST, None, id="推定不可-過去日"),
    pytest.param(EarningsDateStatus.UNAVAILABLE, None, None, id="欠落"),
    pytest.param(EarningsDateStatus.UNAVAILABLE, _D, None, id="解釈不能-生値のみ残る"),
]


@pytest.mark.parametrize(("status", "raw", "resolved"), _EARNINGS_INPUTS)
@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i4_status_and_raw_travel_together_with_the_date(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    status: EarningsDateStatus,
    raw: dt.date | None,
    resolved: dt.date | None,
) -> None:
    """★ 日付・status・raw の 3 つ組が、入力のまま保存されること。

    ★ 「日付だけ保存して確度を落とす」のが本 finding の欠陥である。
    """
    snapshot = dataclasses.replace(
        _base_snapshot(),
        earnings_date_status=status,
        earnings_date_raw=raw,
        next_earnings_date=resolved,
    )

    rec = _both_routes(monkeypatch, snapshot)[route]

    assert rec.next_earnings_date == resolved
    assert rec.earnings_date_status == status
    assert rec.earnings_date_raw == raw


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i4_unavailable_with_raw_is_distinguishable_from_plain_missing(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 「解釈不能（生値あり）」と「欠落（生値なし）」が保存後も区別できること。

    ★ どちらも status は UNAVAILABLE なので、★ **raw を落とすと区別が消える**。
      この 2 つを同じものとして扱うと、データ提供元の不具合を追えなくなる。
    """
    base = _base_snapshot()
    unparsable = dataclasses.replace(
        base,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
        earnings_date_raw=_D,
        next_earnings_date=None,
    )
    missing = dataclasses.replace(
        base,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
        earnings_date_raw=None,
        next_earnings_date=None,
    )

    rec_unparsable = _both_routes(monkeypatch, unparsable)[route]
    rec_missing = _both_routes(monkeypatch, missing)[route]

    assert rec_unparsable.earnings_date_raw == _D
    assert rec_missing.earnings_date_raw is None
    assert rec_unparsable.earnings_date_raw != rec_missing.earnings_date_raw


# --- F-I5: 権利確定日の label / source_type ------------------------------------------

_BENEFIT_INPUTS = [
    pytest.param(
        _benefit(record_dates=[dt.date(2026, 9, 30)]),
        None,
        SourceType.COMPANY_IR,
        id="明示日付",
    ),
    pytest.param(
        _benefit(recurrence_months=[3, 9]),
        "毎年3月末・9月末(登録済みの権利確定周期に基づく)",
        SourceType.COMPANY_IR,
        id="反復月日",
    ),
    pytest.param(None, None, None, id="情報なし"),
]


@pytest.mark.parametrize(("benefit", "expected_label", "expected_source"), _BENEFIT_INPUTS)
@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i5_record_date_label_and_source_match_buy_and_profit_taking(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    benefit: ShareholderBenefit | None,
    expected_label: str | None,
    expected_source: SourceType | None,
) -> None:
    """★ BUY・利確と同じ意味で label / source_type が保存されること。

    ★ 明示日付があるときに label が None なのは正しい（推定が不要なため）。
      「None = 保存漏れ」ではないので、3 入力を並べて意味を固定する。
    """
    snapshot = dataclasses.replace(_base_snapshot(), benefit=benefit)

    rec = _both_routes(monkeypatch, snapshot)[route]

    assert rec.benefit_record_date_recurring_label == expected_label
    assert rec.benefit_record_date_source_type == expected_source


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_f_i5_is_not_re_resolved_against_the_current_date(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 保存時に現在日付で解決し直さないこと。

    ★ 同じ入力なら、評価時刻を動かしても label は変わらない。
      現在日付で再解決していれば、ここで差が出る。
    """
    snapshot = dataclasses.replace(_base_snapshot(), benefit=_benefit(recurrence_months=[3]))

    first = _both_routes(monkeypatch, snapshot)[route]
    later = _both_routes(monkeypatch, dataclasses.replace(snapshot, data_fetched_at=_NOW))[route]

    assert first.benefit_record_date_recurring_label is not None
    assert first.benefit_record_date_recurring_label == later.benefit_record_date_recurring_label


# --- ★ 転記が判定・通知を変えないこと -------------------------------------------------


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_transfer_does_not_change_the_notification_text(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 5 フィールドを埋めても通知文面が **1 文字も変わらない**こと。

    ★ これは #135（ログ・通知への露出）の観点でもある。`earnings_date_raw` は
      外部データの生文字列であり、★ **保存はするが出力はしない**を固定する。
    """
    snapshot = dataclasses.replace(
        _base_snapshot(),
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
        earnings_date_raw=_D,
        next_earnings_date=None,
        benefit=_benefit(recurrence_months=[3, 9]),
    )
    rec = _both_routes(monkeypatch, snapshot)[route]
    stripped = rec.model_copy(update=dict.fromkeys(_TRANSFERRED_FIELDS, None))

    # ★ 両経路とも SELL_SIGNAL へ送られる（_RECOMMENDATION_TYPE_TO_NOTIFICATION）。
    #   formatter の振り分けは recommendation_type 側で行われる。
    rendered = line_module._format_message(rec, NotificationType.SELL_SIGNAL)

    assert rendered == line_module._format_message(stripped, NotificationType.SELL_SIGNAL)
    assert _D.isoformat() not in rendered


@pytest.mark.parametrize("route", _ROUTES_UNDER_TEST)
def test_transfer_does_not_change_the_recommendation_type(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """★ 判定そのものが変わらないこと（転記のみであることの確認）。

    ★ あわせて、両経路は ★ **REVIEW_AFTER_EARNINGS を生成しない**ことを固定する。
      ★ 理由: `line_notification_service._earnings_waiting_state_key()` は
        `earnings_date_raw` / `earnings_date_status` を ★ **再送判定キー**に使う。
        その経路を通るなら、本 Issue の転記は ★ **再送の挙動を変えうる**。
        両経路は `apply_earnings_window()` を呼ばず REVIEW_AFTER_EARNINGS を
        作らないため、★ **キーの計算対象にならない**。ここが崩れたら落ちる。
    """
    snapshot = dataclasses.replace(
        _base_snapshot(),
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        earnings_date_raw=_D,
        next_earnings_date=_D,
    )
    rec = _both_routes(monkeypatch, snapshot)[route]

    expected = (
        RecommendationType.SELL_CONSIDERATION
        if route == "HOLDING_DECISION"
        else RecommendationType.SELL
    )
    assert rec.recommendation_type == expected
    assert rec.recommendation_type != RecommendationType.REVIEW_AFTER_EARNINGS

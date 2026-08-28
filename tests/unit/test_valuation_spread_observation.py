"""spread観測導出(Issue #20 Phase B1)のテスト。

保存済みRecommendationの判定時点スナップショットだけからBUY_RAW/BUY_DECISION/
SELL_RAWを決定的に導出できること、復元できない場合は推測せず
OBSERVATION_UNAVAILABLEとなること、「データなし」と「有効方式0件」を
混同しないことを固定する。数値は9416既知ケース相当を含むが、production側の
taxonomy・導出ロジックには一切ハードコードしない(fixtureの期待値のみ)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    ValuationExclusionReason,
)
from jstock_advisor.domain.valuation.valuation_spread_observation import (
    ObservationStatus,
    ValuationSpreadContext,
    derive_buy_spread_observations,
    derive_sell_spread_observation,
    derive_spread_observations,
)

_NOW = dt.datetime(2026, 8, 24, 23, 1, tzinfo=dt.UTC)


def _make_recommendation(**overrides: object) -> Recommendation:
    base: dict[str, object] = {
        "recommendation_id": "obs-rec-1",
        "stock_code": "9416",
        "stock_name": "テスト銘柄",
        "recommended_at": _NOW,
        "recommendation_type": RecommendationType.BUY,
        "price_at_recommendation": Decimal("1200"),
        "confidence": ConfidenceLevel.MEDIUM,
        "rule_version": "v1-test",
    }
    base.update(overrides)
    return Recommendation(**base)  # type: ignore[arg-type]


def _method(
    method: str,
    fair_value: Decimal | None,
    *,
    applicable: bool = True,
    exclusion_detail: ValuationExclusionReason | None = None,
    exclusion_reason: str | None = None,
) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=method,
        fair_value=fair_value,
        confidence=ConfidenceLevel.MEDIUM,
        applicable=applicable,
        exclusion_detail=exclusion_detail,
        exclusion_reason=exclusion_reason,
    )


# 9416既知ケース相当のfixture(2026-08-24本番判定の値。fixtureの期待値としてのみ使用)
_V9416 = {
    "target_yield": Decimal("1450"),
    "per": Decimal("1499.6"),
    "pbr": Decimal("1468.5"),
    "historical_range": Decimal("949"),
    "dcf": Decimal("478.9"),
}


def _buy_9416_recommendation() -> Recommendation:
    """9416相当: DCF(478.9)がgeneric外れ値フィルタで除外され、残り4方式で
    decision範囲949〜1499.6となったBUY判定記録を再現する。
    valuation_methodsは外れ値フィルタ適用「前」のスナップショット(実装仕様)の
    ため、DCFはapplicable=True・値保持のまま。除外事実は
    buy_score_input_facts["valuation_outlier_exclusions"]に保存される。"""
    return _make_recommendation(
        valuation_methods=tuple(
            _method(name, value) for name, value in sorted(_V9416.items())
        ),
        buy_score_input_facts={
            "valuation_outlier_exclusions": [
                {
                    "method": "dcf",
                    "code": "EXTREME_LOW_RELATIVE_TO_MEDIAN",
                    "message": "算出値が他方式の中央値の40%未満のため除外",
                    "actual_value": "478.9",
                    "reference_value": "587.4",
                }
            ]
        },
        decision_valuation_min=Decimal("949"),
        decision_valuation_max=Decimal("1499.6"),
    )


# --- BUY -----------------------------------------------------------------


def test_buy_9416_raw_and_decision_observations() -> None:
    """9416相当: BUY_RAWは全5方式(min=dcf 478.9/max=per 1499.6)、
    BUY_DECISIONは外れ値除外後4方式(min=historical_range 949/max=per 1499.6)。"""
    raw, decision = derive_buy_spread_observations(_buy_9416_recommendation())

    assert raw.status is ObservationStatus.AVAILABLE
    assert raw.context is ValuationSpreadContext.BUY_RAW
    assert (raw.min_method, raw.min_value) == ("dcf", Decimal("478.9"))
    assert (raw.max_method, raw.max_value) == ("per", Decimal("1499.6"))
    assert raw.methods_count == 5
    assert raw.spread_ratio == pytest.approx(float(Decimal("1499.6") / Decimal("478.9")))
    assert raw.excluded == ()  # RAW段階では何も除外されていない

    assert decision.status is ObservationStatus.AVAILABLE
    assert decision.context is ValuationSpreadContext.BUY_DECISION
    assert (decision.min_method, decision.min_value) == ("historical_range", Decimal("949"))
    assert (decision.max_method, decision.max_value) == ("per", Decimal("1499.6"))
    assert decision.methods_count == 4
    assert decision.spread_ratio == pytest.approx(float(Decimal("1499.6") / Decimal("949")))
    assert len(decision.excluded) == 1
    excluded = decision.excluded[0]
    assert excluded.method == "dcf"
    assert excluded.code == "EXTREME_LOW_RELATIVE_TO_MEDIAN"
    assert excluded.actual_value == Decimal("478.9")
    assert excluded.reference_value == Decimal("587.4")


def test_buy_dcf_divergence_exclusion_in_raw_not_in_decision() -> None:
    """DCF上方乖離除外(applicable=False+exclusion_detail、値保持)は
    BUY_RAWに含まれ、BUY_DECISIONから除外される。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1100")),
            _method(
                "dcf",
                Decimal("2000"),
                applicable=False,
                exclusion_detail=ValuationExclusionReason(
                    code="DCF_UPWARD_DIVERGENCE",
                    message="簡易DCFが他方式の中央値を30%超上回るため除外",
                    actual_value=Decimal("2000"),
                    reference_value=Decimal("1365"),
                ),
            ),
        ),
        buy_score_input_facts={"valuation_outlier_exclusions": []},
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1100"),
    )
    raw, decision = derive_buy_spread_observations(rec)

    assert raw.status is ObservationStatus.AVAILABLE
    assert (raw.max_method, raw.max_value) == ("dcf", Decimal("2000"))
    assert raw.methods_count == 3
    assert decision.status is ObservationStatus.AVAILABLE
    assert decision.methods_count == 2
    assert (decision.max_method, decision.max_value) == ("per", Decimal("1100"))
    assert [e.method for e in decision.excluded] == ["dcf"]
    assert decision.excluded[0].code == "DCF_UPWARD_DIVERGENCE"


def test_buy_multiple_exclusions_combined() -> None:
    """DCF乖離除外+generic外れ値除外の複合ケース。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1100")),
            _method("pbr", Decimal("1050")),
            _method("historical_range", Decimal("300")),
            _method(
                "dcf",
                Decimal("2000"),
                applicable=False,
                exclusion_detail=ValuationExclusionReason(
                    code="DCF_UPWARD_DIVERGENCE",
                    message="除外",
                    actual_value=Decimal("2000"),
                ),
            ),
        ),
        buy_score_input_facts={
            "valuation_outlier_exclusions": [
                {
                    "method": "historical_range",
                    "code": "EXTREME_LOW_RELATIVE_TO_MEDIAN",
                    "actual_value": "300",
                    "reference_value": "420",
                }
            ]
        },
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1100"),
    )
    raw, decision = derive_buy_spread_observations(rec)

    assert raw.methods_count == 5  # 2000(dcf)と300(historical)を含む
    assert (raw.min_value, raw.max_value) == (Decimal("300"), Decimal("2000"))
    assert decision.methods_count == 3
    assert (decision.min_value, decision.max_value) == (Decimal("1000"), Decimal("1100"))
    assert sorted(e.method for e in decision.excluded) == ["dcf", "historical_range"]


def test_buy_inapplicable_method_without_detail_is_not_in_raw() -> None:
    """算出不能・業種モデル未実装等(applicable=False・exclusion_detailなし)は
    集計候補ですらないため、RAWにも含めない(値の捏造をしない)。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1200")),
            _method("dcf", None, applicable=False, exclusion_reason="営業CFが負のため算出不可"),
        ),
        buy_score_input_facts={"valuation_outlier_exclusions": []},
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1200"),
    )
    raw, decision = derive_buy_spread_observations(rec)
    assert raw.methods_count == 2
    assert decision.methods_count == 2
    assert decision.excluded == ()


def test_buy_tie_resolves_by_method_name_ascending() -> None:
    """同値タイはmethod名昇順の先頭(決定的)。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1000")),
            _method("pbr", Decimal("1000")),
        ),
        buy_score_input_facts={"valuation_outlier_exclusions": []},
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1000"),
    )
    raw, decision = derive_buy_spread_observations(rec)
    assert raw.min_method == "pbr"  # method名昇順: "pbr" < "per" < "target_yield"
    assert raw.min_method == raw.max_method
    assert decision.spread_ratio == pytest.approx(1.0)


def test_buy_decision_mismatch_with_saved_range_is_unavailable() -> None:
    """導出decision集合が保存済みdecision_valuation_min/maxと一致しない場合は
    推測せずUNAVAILABLE(外れ値スナップショット未保存世代の検知)。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1100")),
        ),
        buy_score_input_facts={},  # 外れ値スナップショットなし
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1050"),  # 導出(1100)と不一致
    )
    raw, decision = derive_buy_spread_observations(rec)
    assert raw.status is ObservationStatus.AVAILABLE
    assert decision.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert decision.unavailable_reason is not None
    assert "一致しません" in decision.unavailable_reason


def test_buy_decision_without_saved_range_is_unverifiable() -> None:
    """decision_valuation_min/max未保存(旧世代)では照合できないため、
    decision観測は推測せずUNAVAILABLE。RAWは独立に導出可能。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1100")),
        ),
    )
    raw, decision = derive_buy_spread_observations(rec)
    assert raw.status is ObservationStatus.AVAILABLE
    assert decision.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert "照合ができません" in (decision.unavailable_reason or "")


def test_buy_raw_unavailable_when_excluded_value_lost() -> None:
    """除外方式の元値(fair_value/actual_valueとも)が失われている場合、
    BUY_RAWは推測せずUNAVAILABLE。BUY_DECISIONは照合できれば導出可能。"""
    rec = _make_recommendation(
        valuation_methods=(
            _method("target_yield", Decimal("1000")),
            _method("per", Decimal("1100")),
            _method(
                "dcf",
                None,
                applicable=False,
                exclusion_detail=ValuationExclusionReason(
                    code="DCF_UPWARD_DIVERGENCE", message="除外", actual_value=None
                ),
            ),
        ),
        buy_score_input_facts={"valuation_outlier_exclusions": []},
        decision_valuation_min=Decimal("1000"),
        decision_valuation_max=Decimal("1100"),
    )
    raw, decision = derive_buy_spread_observations(rec)
    assert raw.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert "復元できません" in (raw.unavailable_reason or "")
    assert decision.status is ObservationStatus.AVAILABLE


# --- SELL ----------------------------------------------------------------


def _sell_methods(values: dict[str, Decimal | None]) -> list[dict[str, object]]:
    return [
        {
            "method": name,
            "fair_value": str(value) if value is not None else None,
            "confidence": "MEDIUM",
            "exclusion_reason": None,
        }
        for name, value in sorted(values.items())
    ]


def test_sell_raw_matches_saved_bear_bull_and_spread_ratio() -> None:
    """SELL_RAW端点が保存済みfair_value_bear/bullと一致し、spread_ratioが
    保存値(#21本番36件型: 2.0超)と整合する。"""
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods(_V9416),
        fair_value_bear=Decimal("478.9"),
        fair_value_bull=Decimal("1499.6"),
        fair_value_spread_ratio=float(Decimal("1499.6") / Decimal("478.9")),
        fair_value_usable_for_trading_judgment=False,
        fair_value_unusable_reason_code="METHOD_SPREAD_TOO_WIDE",
    )
    observation = derive_sell_spread_observation(rec)

    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.context is ValuationSpreadContext.SELL_RAW
    assert (observation.min_method, observation.min_value) == ("dcf", Decimal("478.9"))
    assert (observation.max_method, observation.max_value) == ("per", Decimal("1499.6"))
    assert observation.methods_count == 5
    assert observation.spread_ratio == pytest.approx(rec.fair_value_spread_ratio)
    assert observation.excluded == ()  # SELLに除外機構は存在しない
    # #21 reason codeとの意味論整合: METHOD_SPREAD_TOO_WIDE ⇒ ratio >= 2.0
    assert observation.spread_ratio is not None and observation.spread_ratio >= 2.0


def test_sell_no_valid_methods_is_available_with_count_zero() -> None:
    """#21のNO_VALID_METHODS相当(全方式値なし)は「データなし」ではなく
    count=0のAVAILABLE観測(0件とUNAVAILABLEを混同しない)。"""
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods(
            {"target_yield": None, "per": None, "pbr": None}
        ),
        fair_value_bear=None,
        fair_value_bull=None,
        fair_value_usable_for_trading_judgment=False,
        fair_value_unusable_reason_code="NO_VALID_METHODS",
    )
    observation = derive_sell_spread_observation(rec)
    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.methods_count == 0
    assert observation.min_method is None
    assert observation.spread_ratio is None


def test_sell_mismatch_with_saved_range_is_unavailable() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods({"target_yield": Decimal("1000"), "per": Decimal("1100")}),
        fair_value_bear=Decimal("900"),  # 導出(1000)と不一致
        fair_value_bull=Decimal("1100"),
    )
    observation = derive_sell_spread_observation(rec)
    assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert "一致しません" in (observation.unavailable_reason or "")


# --- unavailable / 旧レコード -------------------------------------------


def test_old_record_without_snapshots_is_unavailable_for_all_contexts() -> None:
    """per-methodスナップショットを持たない旧Recommendationは3contextすべて
    OBSERVATION_UNAVAILABLE(現在config・現在株価からの再構築はしない)。"""
    rec = _make_recommendation()
    raw, decision, sell = derive_spread_observations(rec)
    assert raw.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert decision.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert sell.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    for observation in (raw, decision, sell):
        assert observation.methods_count == 0
        assert observation.min_value is None
        assert observation.unavailable_reason is not None


# --- integration: 実サービスが生成したRecommendationとの照合 --------------


def test_integration_buy_service_recommendation_matches_saved_decision_range() -> None:
    """BuySignalService(実mock providerパイプライン)が生成したRecommendation
    について、導出BUY_DECISION端点が保存済みdecision_valuation_min/maxと
    一致し(導出内クロスチェックの通過=AVAILABLE)、BUY_RAWも導出できる。"""
    from jstock_advisor.services.buy_signal_service import BuySignalService
    from jstock_advisor.services.provider_factory import build_mock_provider_bundle

    config = load_config()
    now = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
    service = BuySignalService(
        providers=build_mock_provider_bundle(now),
        config=config,
        business_calendar=BusinessCalendar.from_config(config.holiday_calendar),
    )
    outcome = service.analyze("2914", now, RecommendationType.BUY)
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.valuation_methods  # 保存スナップショットが存在する前提の確認

    raw, decision = derive_buy_spread_observations(rec)

    assert raw.status is ObservationStatus.AVAILABLE
    assert decision.status is ObservationStatus.AVAILABLE
    assert decision.min_value == rec.decision_valuation_min
    assert decision.max_value == rec.decision_valuation_max
    if rec.valuation_dispersion_ratio is not None and decision.spread_ratio is not None:
        # 保存側はfloat→Decimal変換を経ているため厳密一致ではなく近似で照合する
        assert decision.spread_ratio == pytest.approx(
            float(rec.valuation_dispersion_ratio), rel=1e-6
        )


def test_integration_sell_service_recommendation_matches_saved_fair_value_range() -> None:
    """ProfitTakingService(実mock providerパイプライン)が生成した
    Recommendationについて、導出SELL_RAWが保存済み
    fair_value_bear/bull/spread_ratioと一致する。"""
    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
    from jstock_advisor.services.profit_taking_service import ProfitTakingService
    from jstock_advisor.services.provider_factory import build_mock_provider_bundle

    config = load_config()
    now = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
    holding = Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "2914"),
        stock_code="2914",
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("300000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=now,
        updated_at=now,
    )
    service = ProfitTakingService(providers=build_mock_provider_bundle(now), config=config)
    outcome = service.analyze(holding, now)
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.fair_value_methods  # 保存スナップショットが存在する前提の確認

    observation = derive_sell_spread_observation(rec)

    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.min_value == rec.fair_value_bear
    assert observation.max_value == rec.fair_value_bull
    if rec.fair_value_spread_ratio is not None and observation.spread_ratio is not None:
        assert observation.spread_ratio == pytest.approx(rec.fair_value_spread_ratio, rel=1e-9)

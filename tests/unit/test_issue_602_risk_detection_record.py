"""Issue #602 H2: 買い判定が RAER の入力(リスクの印)の『検出の記録』を写す。

記録先 = buy_score_input_facts。

固定するもの(設計 = #602 issuecomment-6139279351 rev2 / サブちゃんの PASS …6139380647)
  (1) 記録の形(版・検出済みの語・語ごとの観測の有無)と、語ごとの観測の条件
  (2) ★ 既に計算済みの値を写すだけ ★: 買い判定・安全余裕率・通知の内容・adjustment_codes が、
      記録を出さない実装(旧挙動)と完全に一致する(差は新しいキー 1 つだけ)
  (3) 失敗を本流へ伝播させない(隔離)。記録できなくても買い判定は変わらない
  (4) 生産側と読み側(domain/valuation/raer_inputs.py)の literal・語彙が一致する(同型 sweep)
  (5) 新しい field を足さない・版を上げない・raer 系 module を import しない(#606 の到達性)
"""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import RecommendationType, StockType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation import raer_inputs
from jstock_advisor.domain.valuation.raer import RiskFlag
from jstock_advisor.domain.valuation.raer_inputs import RiskFlagSource, risk_flag_view
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import (
    FACTS_SCHEMA_VERSION,
    RAER_RISK_DETECTION_KEY,
    RAER_RISK_DETECTION_VERSION,
    BuySignalService,
    _raer_risk_detection_facts,
)
from tests.unit.test_buy_signal_service import (
    _CALENDAR,
    _CONFIG,
    _NIHON_SHINYAKU,
    _NOW,
    _TACHI_S,
    _build_snapshot,
    _providers,
)

_SERVICE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "jstock_advisor"
    / "services"
    / "buy_signal_service.py"
)


def _analyze(
    monkeypatch: pytest.MonkeyPatch,
    fixture: Any = _NIHON_SHINYAKU,
    *,
    override: dict[str, Any] | None = None,
    helper: Any = None,
) -> Recommendation:
    snapshot = _build_snapshot(fixture)
    if override:
        snapshot = dataclasses.replace(snapshot, **override)
    monkeypatch.setattr(service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None))
    if helper is not None:
        monkeypatch.setattr(service_module, "_raer_risk_detection_facts", helper)
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    outcome = service.analyze(fixture.stock_code, _NOW, RecommendationType.BUY)
    return outcome.recommendation


def _record(recommendation: Recommendation) -> dict[str, Any]:
    facts = recommendation.buy_score_input_facts
    assert facts is not None
    record = facts[RAER_RISK_DETECTION_KEY]
    assert isinstance(record, dict)
    return record


# =================================================================================================
# (1) 記録の形と観測の条件
# =================================================================================================


def test_the_record_has_the_version_the_detected_words_and_the_observation_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(_analyze(monkeypatch))
    assert set(record) == {"version", "flags", "observed"}
    assert record["version"] == RAER_RISK_DETECTION_VERSION == 1
    assert isinstance(record["flags"], list)
    assert set(record["observed"]) == {"earnings_date", "liquidity", "industry", "eps"}
    assert all(isinstance(value, bool) for value in record["observed"].values())


def test_the_recorded_flags_are_exactly_the_detected_words_in_a_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = _analyze(monkeypatch)
    detected = [adjustment.code for adjustment in recommendation.margin_adjustments]
    expected = [word for word in service_module._RAER_RISK_WORDS if word in detected]
    assert _record(recommendation)["flags"] == expected
    # この銘柄(決算 2 営業日後)は決算 3 営業日以内の語が検出されている(空振りでない)
    assert "earnings_within_3_business_days" in expected


def test_a_stock_of_the_auto_parts_sector_records_the_industry_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(_analyze(monkeypatch, _TACHI_S))
    assert "major_customer_dependency" in record["flags"]
    assert "cyclical_industry" in record["flags"]
    assert record["observed"]["industry"] is True


@pytest.mark.parametrize(
    ("override", "key"),
    [
        ({"business_days_to_earnings": None}, "earnings_date"),
        ({"avg_trading_value": None}, "liquidity"),
    ],
    ids=["no-earnings-date", "no-trading-value"],
)
def test_a_missing_input_is_recorded_as_not_observed_and_never_as_a_flag(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, Any], key: str
) -> None:
    record = _record(_analyze(monkeypatch, override=override))
    assert record["observed"][key] is False
    other_keys = {"earnings_date", "liquidity", "industry", "eps"} - {key}
    for other in other_keys:
        assert isinstance(record["observed"][other], bool)
    words = {
        "earnings_date": {"earnings_within_3_business_days", "earnings_within_7_business_days"},
        "liquidity": {"small_cap_or_low_liquidity"},
    }[key]
    assert not (set(record["flags"]) & words)


def test_the_industry_observation_means_the_sector_string_was_obtained_not_a_stock_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHOULD-4: industry = 業種の文字列が取れた(buy_industry_sector が UNKNOWN でない)。

    銘柄分類(景気型)では観測済みにしない。
    """
    unknown = _analyze(
        monkeypatch,
        override={
            "financial": _build_snapshot(_NIHON_SHINYAKU).financial.model_copy(
                update={"industry": None, "sector": None}
            ),
            # 銘柄分類(景気型)に該当しても、業種の文字列が取れたことにはならない
            "stock_type_classification": _build_snapshot(
                _NIHON_SHINYAKU
            ).stock_type_classification.model_copy(update={"types": [StockType.CYCLICAL]}),
        },
    )
    record = _record(unknown)
    assert record["observed"]["industry"] is False
    assert "cyclical_industry" in record["flags"]  # 景気型の分類で該当は確定している
    # 業種が不明なら、主要顧客依存・景気業種(業種の分類が前提の語)は『該当しない』と言えない
    view = raer_inputs.risk_flag_view_from_recommendation(unknown)
    assert RiskFlag.CYCLICAL_INDUSTRY in view.flags
    assert RiskFlag.CYCLICAL_INDUSTRY not in view.unobserved  # 該当は観測できた証拠
    assert RiskFlag.MAJOR_CUSTOMER_DEPENDENCY in view.unobserved


_SCENARIOS: list[tuple[str, Any, dict[str, Any] | None]] = [
    ("pharma", _NIHON_SHINYAKU, None),
    ("auto-parts", _TACHI_S, None),
    ("no-earnings-date", _NIHON_SHINYAKU, {"business_days_to_earnings": None}),
    ("no-trading-value", _NIHON_SHINYAKU, {"avg_trading_value": None}),
    ("auto-parts-no-earnings-date", _TACHI_S, {"business_days_to_earnings": None}),
    ("auto-parts-no-trading-value", _TACHI_S, {"avg_trading_value": None}),
    # 業種の文字列が取れず(セクターが UNKNOWN)、銘柄分類(景気型)だけで景気業種に該当する場面。
    # 読み手の『観測できなかった語 − flags』が受ける経路の生産側を固定する(サブちゃんの SHOULD)
    (
        "unknown-sector-cyclical-by-stock-type",
        _NIHON_SHINYAKU,
        {
            "financial": _build_snapshot(_NIHON_SHINYAKU).financial.model_copy(
                update={"industry": None, "sector": None}
            ),
            "stock_type_classification": _build_snapshot(
                _NIHON_SHINYAKU
            ).stock_type_classification.model_copy(update={"types": [StockType.CYCLICAL]}),
        },
    ),
]


@pytest.mark.parametrize(
    ("name", "fixture", "override"), _SCENARIOS, ids=[s[0] for s in _SCENARIOS]
)
def test_a_flag_is_recorded_only_when_it_is_determined_true_by_an_observed_input(
    monkeypatch: pytest.MonkeyPatch, name: str, fixture: Any, override: dict[str, Any] | None
) -> None:
    """★ 唯一の砦(サブちゃんの merge 条件): flags に入る語は、その時点で True と確定した語だけ ★

    読み手は『矛盾した記録(観測できなかったのに該当した語)』を検出せず、flags の語を観測済みとして
    扱う。したがって生産側が、観測できていない入力から語を flags に入れてはならない。
    """
    record = _record(_analyze(monkeypatch, fixture, override=override))
    flags = set(record["flags"])
    observed = record["observed"]
    # 決算 3 / 7 営業日以内の語 = 決算日が分かるときだけ
    if flags & {"earnings_within_3_business_days", "earnings_within_7_business_days"}:
        assert observed["earnings_date"] is True
    # 小型・低流動性の語 = 平均売買代金が分かるときだけ
    if "small_cap_or_low_liquidity" in flags:
        assert observed["liquidity"] is True
    # 主要顧客依存 = 業種の文字列が取れて、自動車部品に分類されたときだけ
    if "major_customer_dependency" in flags:
        assert observed["industry"] is True
    # 一時的な増益のリスク = 景気業種で、EPS が両方取れたときだけ
    if "temporary_earnings_boost_risk" in flags:
        assert "cyclical_industry" in flags
        assert observed["eps"] is True
    # 検出された語は、安全余裕率の調整(買い判定が検出した語)の部分集合
    detected = {
        a.code for a in _analyze(monkeypatch, fixture, override=override).margin_adjustments
    }
    assert flags <= set(service_module._RAER_RISK_WORDS)
    assert flags >= (detected & set(service_module._RAER_RISK_WORDS))


def test_the_eps_observation_needs_both_the_normalized_and_the_forecast_eps() -> None:
    both = _facts(eps_observed=True)["observed"]
    assert isinstance(both, dict) and both["eps"] is True
    neither = _facts(eps_observed=False)["observed"]
    assert isinstance(neither, dict) and neither["eps"] is False


def test_the_industry_is_not_observed_only_when_the_classification_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = _analyze(monkeypatch, override=None, fixture=_NIHON_SHINYAKU)
    assert _record(unknown)["observed"]["industry"] is True  # 分類できる業種


# =================================================================================================
# (1') 記録の組み立て(純粋)の条件
# =================================================================================================


def _facts(**overrides: Any) -> dict[str, object]:
    values: dict[str, Any] = {
        "adjustment_codes": [],
        "business_days_to_earnings": 10,
        "avg_trading_value": Decimal("100000000"),
        "industry_classified": True,
        "eps_observed": True,
    }
    values.update(overrides)
    return _raer_risk_detection_facts(**values)


def test_the_helper_keeps_only_the_six_words_in_the_fixed_order() -> None:
    record = _facts(
        adjustment_codes=[
            "data_quality_warning",
            "small_cap_or_low_liquidity",
            "high_valuation_dispersion",
            "cyclical_industry",
            "volatile_earnings",
            "earnings_within_3_business_days",
        ]
    )
    assert record["flags"] == [
        "cyclical_industry",
        "earnings_within_3_business_days",
        "small_cap_or_low_liquidity",
    ]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"business_days_to_earnings": None}, {"earnings_date": False}),
        ({"business_days_to_earnings": 0}, {"earnings_date": True}),  # 0 営業日は『分かる』
        ({"avg_trading_value": None}, {"liquidity": False}),
        ({"avg_trading_value": Decimal("0")}, {"liquidity": True}),  # 0 円は『分かる』
        ({"industry_classified": False}, {"industry": False}),
        ({"eps_observed": False}, {"eps": False}),
    ],
    ids=["no-date", "zero-days", "no-value", "zero-value", "no-industry", "no-eps"],
)
def test_each_observation_flag_follows_only_its_own_input(
    override: dict[str, Any], expected: dict[str, bool]
) -> None:
    observed = _facts(**override)["observed"]
    assert isinstance(observed, dict)
    baseline = {"earnings_date": True, "liquidity": True, "industry": True, "eps": True}
    assert observed == {**baseline, **expected}


def test_the_helper_is_deterministic_and_does_not_touch_its_input() -> None:
    codes = ["small_cap_or_low_liquidity", "cyclical_industry"]
    before = list(codes)
    first = _facts(adjustment_codes=codes)
    second = _facts(adjustment_codes=codes)
    assert first == second
    assert codes == before


# =================================================================================================
# (2) 既に計算済みの値を写すだけ(買い判定・通知は不変)
# =================================================================================================


@pytest.mark.parametrize("fixture", [_NIHON_SHINYAKU, _TACHI_S], ids=["pharma", "auto-parts"])
def test_the_decision_and_every_other_fact_equal_the_old_behaviour_without_the_record(
    monkeypatch: pytest.MonkeyPatch, fixture: Any
) -> None:
    with_record = _analyze(monkeypatch, fixture)
    without_record = _analyze(monkeypatch, fixture, helper=lambda **_kw: {})
    assert without_record.buy_score_input_facts is not None
    assert without_record.buy_score_input_facts[RAER_RISK_DETECTION_KEY] == {}  # 空振りでない
    assert with_record.buy_score_input_facts is not None
    for field in (
        "buy_action",
        "raw_buy_action",
        "buy_prices",
        "valuation_anchor",
        "confidence",
        "recommendation_type",
        "reasons",
        "counter_factors",
        "key_risks",
        "total_score",
        "company_quality_score",
        "margin_adjustments",
        "required_margin_of_safety_entry",
        "required_margin_of_safety_standard",
        "required_margin_of_safety_strong",
        "business_days_to_earnings",
        "valuation_dispersion_ratio",
    ):
        assert getattr(with_record, field) == getattr(without_record, field), field
    facts_with = {
        key: value
        for key, value in with_record.buy_score_input_facts.items()
        if key != RAER_RISK_DETECTION_KEY
    }
    facts_without = {
        key: value
        for key, value in without_record.buy_score_input_facts.items()
        if key != RAER_RISK_DETECTION_KEY
    }
    assert facts_with == facts_without  # 差は新しいキー 1 つだけ


def test_the_adjustment_codes_are_not_changed_by_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []

    def spy(**kwargs: Any) -> dict[str, object]:
        recorded.append(list(kwargs["adjustment_codes"]))
        return _raer_risk_detection_facts(**kwargs)

    recommendation = _analyze(monkeypatch, helper=spy)
    assert len(recorded) == 1
    assert recorded[0] == [adjustment.code for adjustment in recommendation.margin_adjustments]


# =================================================================================================
# (3) 隔離(記録の失敗は本流へ伝播しない)
# =================================================================================================


def test_a_failure_in_the_record_never_breaks_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: Any) -> dict[str, object]:
        raise RuntimeError("record failed")

    failed = _analyze(monkeypatch, helper=boom)
    normal = _analyze(monkeypatch)
    assert failed.buy_action == normal.buy_action
    assert failed.buy_prices == normal.buy_prices
    assert failed.buy_score_input_facts is not None
    marker = failed.buy_score_input_facts[RAER_RISK_DETECTION_KEY]
    assert marker["shadow_state"] == "COMPUTATION_FAILED"  # type: ignore[index]
    # 読み側は、失敗した記録を使わず、旧い記録(安全余裕率の調整)へ落ちる(補正しない)
    view = risk_flag_view(raer_inputs.facts_from_recommendation(failed))
    assert view.source is not RiskFlagSource.DETECTION_RECORD


# =================================================================================================
# (4) 生産側と読み側の同型 sweep
# =================================================================================================


def test_the_producer_literals_equal_the_reader_literals() -> None:
    assert RAER_RISK_DETECTION_KEY == raer_inputs.RISK_DETECTION_FACTS_KEY
    assert RAER_RISK_DETECTION_VERSION == raer_inputs.RISK_DETECTION_VERSION
    assert set(service_module._RAER_RISK_WORDS) == {flag.value for flag in RiskFlag}
    assert len(service_module._RAER_RISK_WORDS) == 6


def test_the_observed_keys_written_equal_the_keys_the_reader_requires() -> None:
    written = set(_facts()["observed"])  # type: ignore[arg-type]
    assert written == set(raer_inputs._OBSERVED_KEYS)


@pytest.mark.parametrize("fixture", [_NIHON_SHINYAKU, _TACHI_S], ids=["pharma", "auto-parts"])
def test_the_real_recommendation_is_read_by_the_reader_as_a_detection_record(
    monkeypatch: pytest.MonkeyPatch, fixture: Any
) -> None:
    recommendation = _analyze(monkeypatch, fixture)
    view = raer_inputs.risk_flag_view_from_recommendation(recommendation)
    assert view.source is RiskFlagSource.DETECTION_RECORD
    expected = {
        RiskFlag(word)
        for word in service_module._RAER_RISK_WORDS
        if word in [a.code for a in recommendation.margin_adjustments]
    }
    assert view.flags == frozenset(expected)
    observed = _record(recommendation)["observed"]
    if all(observed.values()):
        assert view.unobserved == frozenset()  # 全項目を観測できた銘柄
    else:
        assert view.unobserved  # 観測できなかった項目の語が、未観測として読める
        assert not (view.flags & view.unobserved)


def test_a_missing_input_flows_to_the_reader_as_unobserved_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommendation = _analyze(monkeypatch, override={"avg_trading_value": None})
    view = raer_inputs.risk_flag_view_from_recommendation(recommendation)
    assert view.source is RiskFlagSource.DETECTION_RECORD
    assert view.unobserved == frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})
    assert not view.observed_all()
    assert view.observed_all(frozenset({RiskFlag.MAJOR_CUSTOMER_DEPENDENCY}))


# =================================================================================================
# (5) 構造
# =================================================================================================


def test_the_schema_version_is_not_raised_by_an_optional_key() -> None:
    # 版は『読み手が必須とするキーの集合』を指し、追加の optional key では上げない(SHOULD-1)
    assert FACTS_SCHEMA_VERSION == "v2"


def test_the_recommendation_entity_gains_no_field_for_the_record() -> None:
    assert not [name for name in Recommendation.model_fields if "risk_detection" in name]


def test_the_buy_service_does_not_import_any_raer_module() -> None:
    tree = ast.parse(_SERVICE_PATH.read_text(encoding="utf-8"))
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [m for m in modules if "raer" in m or "expected_return" in m]


def test_the_record_contains_no_number_code_or_amount() -> None:
    record = _facts(adjustment_codes=["cyclical_industry"])
    text = repr(record)
    assert "Decimal" not in text
    assert "4516" not in text

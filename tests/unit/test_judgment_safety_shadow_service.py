"""Issue #160 PR-3(#457): 判断の安全条件のshadow評価・監査記録(サービス)。

不変条件を固定する:
  * OFFなら、評価関数も監査記録も呼ばない(事実の取得もしない)。
  * SHADOWでも、評価・記録の失敗は隔離され、例外が呼び出し元へ出ない。リトライしない。
  * 強い判定(BUY系 / 利確のFULL_PROFIT_TAKE)だけを記録する。findingが0件でも記録する(分母)。
  * 記録は決定的なaudit_idで冪等。VALIDATIONでは保存しない。
  * 保存する内容は allowlist の列挙のみ。禁止項目(holding_id・owner・価格・数量・銘柄名等)
    を含まない。

★ 銘柄コードは実在しない0000系、所有者・保有数量・価格は架空値のみ。
   Productionへは一切アクセスしない。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    EarningsDateStatus,
    ExecutionMode,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.judgment_safety import (
    UNMEASURABLE_G3_INPUTS,
    CorporateActionFacts,
    ProfitTakingMitigationFacts,
    SafetyFacts,
)
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    ShadowMode,
    load_judgment_safety_shadow_config,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services import judgment_safety_shadow_service as shadow_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.judgment_safety_shadow_service import (
    DECISION_TYPE,
    ENGINE_BUY_CANDIDATES,
    ENGINE_HOLDINGS_PROFIT_TAKING,
    FACT_KEYS,
    INPUT_VALUE_KEYS,
    OUTPUT_VALUE_KEYS,
    observe_judgment_safety_shadow,
    shadow_audit_id,
)

_NOW = dt.datetime(2026, 9, 20, 9, 0, tzinfo=dt.UTC)
_SHADOW = JudgmentSafetyShadowConfig(mode=ShadowMode.SHADOW)
_OFF = JudgmentSafetyShadowConfig()
_NORMAL = ExecutionContext.normal()


def _rec(
    *,
    recommendation_type: RecommendationType = RecommendationType.BUY,
    buy_action: BuyAction | None = BuyAction.BUY,
    earnings_date_status: EarningsDateStatus | None = EarningsDateStatus.CONFIRMED,
    recommendation_id: str = "rec-457-1",
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        # 所有者・holding_idを含む値をあえて持たせ、shadowの記録へ漏れないことを確認する。
        owner="owner-a",
        holding_id="owner-a#0000",
        stock_code="0000",
        stock_name="架空銘柄A",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_action=buy_action,
        price_at_recommendation=Decimal("1234.56"),
        average_purchase_price_at_recommendation=Decimal("987.65"),
        shares_at_recommendation=321,
        fair_value_at_recommendation=Decimal("2222.22"),
        confidence=ConfidenceLevel.MEDIUM,
        reasons=["架空の理由文(記録へ入れてはならない)"],
        rule_version="v1-test",
        earnings_date_status=earnings_date_status,
    )


def _full_take() -> Recommendation:
    return _rec(
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        buy_action=None,
        recommendation_id="rec-457-2",
    )


class _Outcome:
    def __init__(self, facts: SafetyFacts | None) -> None:
        self.safety_facts = facts


class _SpyAuditService:
    """record_if_absentだけを持つ記録用のspy。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._seen: set[str] = set()

    def record_if_absent(self, **kwargs: Any) -> object | None:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if kwargs["audit_id"] in self._seen:
            return None
        self._seen.add(kwargs["audit_id"])
        return object()


def _run(
    recommendation: Recommendation,
    facts: SafetyFacts | None,
    *,
    engine: shadow_module.ShadowEngine = ENGINE_BUY_CANDIDATES,
    config: JudgmentSafetyShadowConfig = _SHADOW,
    audit_service: Any = None,
) -> bool:
    return observe_judgment_safety_shadow(
        recommendation,
        _Outcome(facts),
        engine,
        _NOW,
        execution_context=_NORMAL,
        audit_service=audit_service,
        shadow_config=config,
    )


# --- OFFなら何もしない ------------------------------------------------------------


def test_off_never_evaluates_records_or_reads_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF(既定): 評価関数も監査記録も呼ばない。事実の取得(outcome.safety_facts)もしない。"""

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("shadow OFF なのに実行された")

    monkeypatch.setattr(shadow_module, "evaluate_safety_conditions", _must_not_run)
    audit = _SpyAuditService()

    class _ExplodingOutcome:
        @property
        def safety_facts(self) -> SafetyFacts:
            raise AssertionError("shadow OFF なのに事実を読んだ")

    recorded = observe_judgment_safety_shadow(
        _rec(),
        _ExplodingOutcome(),
        ENGINE_BUY_CANDIDATES,
        _NOW,
        execution_context=_NORMAL,
        audit_service=cast(AuditService, audit),
        shadow_config=_OFF,
    )

    assert recorded is False
    assert audit.calls == []


def test_default_config_is_the_shipped_off_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """shadow_configを渡さない既定は、出荷configを読む(mode: "OFF")。何も記録しない。"""
    audit = _SpyAuditService()

    recorded = observe_judgment_safety_shadow(
        _rec(),
        _Outcome(SafetyFacts()),
        ENGINE_BUY_CANDIDATES,
        _NOW,
        execution_context=_NORMAL,
        audit_service=cast(AuditService, audit),
    )

    assert recorded is False
    assert audit.calls == []


def test_unreadable_config_falls_back_to_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """設定が不正で読めない場合は、実際のloaderがOFFへ縮退し、何も記録せず、例外も出さない。"""
    (tmp_path / "judgment_safety_shadow.yaml").write_text("mode: [broken", encoding="utf-8")
    monkeypatch.setattr(
        shadow_module,
        "load_judgment_safety_shadow_config",
        lambda: load_judgment_safety_shadow_config(tmp_path),
    )
    audit = _SpyAuditService()

    recorded = observe_judgment_safety_shadow(
        _rec(),
        _Outcome(SafetyFacts()),
        ENGINE_BUY_CANDIDATES,
        _NOW,
        execution_context=_NORMAL,
        audit_service=cast(AuditService, audit),
    )

    assert recorded is False
    assert audit.calls == []


# --- 強い判定だけを記録する ---------------------------------------------------------


@pytest.mark.parametrize("action", [BuyAction.STRONG_BUY, BuyAction.BUY, BuyAction.SMALL_ENTRY])
def test_buy_family_is_recorded(action: BuyAction) -> None:
    audit = _SpyAuditService()

    assert _run(
        _rec(buy_action=action), SafetyFacts(financials_are_stale=False), audit_service=audit
    )
    assert len(audit.calls) == 1
    assert audit.calls[0]["decision_type"] == DECISION_TYPE


def test_full_profit_take_is_recorded() -> None:
    audit = _SpyAuditService()

    assert _run(
        _full_take(),
        SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(3, True)),
        engine=ENGINE_HOLDINGS_PROFIT_TAKING,
        audit_service=cast(AuditService, audit),
    )
    assert audit.calls[0]["input_values"]["engine"] == "HOLDINGS_PROFIT_TAKING"


@pytest.mark.parametrize(
    ("recommendation_type", "buy_action"),
    [
        (RecommendationType.HOLD, None),
        (RecommendationType.WATCH, None),
        (RecommendationType.PARTIAL_PROFIT_TAKE, None),
        (RecommendationType.SELL, None),
        (RecommendationType.URGENT_REVIEW, None),
        (RecommendationType.BUY, BuyAction.WATCH_FOR_PRICE),
        (RecommendationType.BUY, BuyAction.MANUAL_REVIEW),
        (RecommendationType.BUY, BuyAction.NOT_ATTRACTIVE),
    ],
)
def test_non_strong_judgments_are_not_recorded(
    recommendation_type: RecommendationType, buy_action: BuyAction | None
) -> None:
    """強い判定でなければ、評価も記録もしない(書き込み量を強い判定の件数に限る。SELL系へ拡張しない)。"""
    audit = _SpyAuditService()

    recorded = _run(
        _rec(recommendation_type=recommendation_type, buy_action=buy_action),
        SafetyFacts(),
        audit_service=cast(AuditService, audit),
    )

    assert recorded is False
    assert audit.calls == []


def test_strong_judgment_with_zero_findings_is_still_recorded() -> None:
    """findingが0件でも、強い判定なら記録する(分母。0件を「記録なし」と区別できるようにする)。"""
    audit = _SpyAuditService()
    facts = SafetyFacts(
        financials_are_stale=False,
        corporate_action=CorporateActionFacts("EVALUATED", ()),
    )

    assert _run(_rec(), facts, audit_service=audit)

    output = audit.calls[0]["output_values"]
    assert output["findings"] == []
    assert output["strong"] is True


def test_not_evaluated_is_recorded_separately_from_findings() -> None:
    """入力が無い条件は not_evaluated として別掲する(「該当なし」ではない)。"""
    audit = _SpyAuditService()

    assert _run(_rec(), SafetyFacts(), audit_service=audit)  # 事実が何も供給されていない

    output = audit.calls[0]["output_values"]
    assert output["findings"] == []
    assert set(output["not_evaluated"]) == {"G2", "G4"}


def test_missing_facts_object_is_treated_as_nothing_supplied() -> None:
    """outcome.safety_facts が None でも例外にならず、全条件が not_evaluated になる。"""
    audit = _SpyAuditService()

    assert _run(_rec(), None, audit_service=audit)

    assert set(audit.calls[0]["output_values"]["not_evaluated"]) == {"G2", "G4"}


def test_findings_are_recorded_with_condition_and_reason() -> None:
    audit = _SpyAuditService()
    facts = SafetyFacts(
        financials_are_stale=True,
        corporate_action=CorporateActionFacts("EVALUATED", ("price_discontinuity_unexplained",)),
    )

    assert _run(
        _rec(earnings_date_status=EarningsDateStatus.UNAVAILABLE), facts, audit_service=audit
    )

    findings = audit.calls[0]["output_values"]["findings"]
    assert [(f["condition_id"], f["reason_code"]) for f in findings] == [
        ("G1", "EARNINGS_DATE_UNKNOWN"),
        ("G2", "STALE_FINANCIALS"),
        ("G4", "CORPORATE_ACTION_UNRESOLVED:price_discontinuity_unexplained"),
    ]
    assert all(f["would_suppress"] is True for f in findings)
    assert audit.calls[0]["output_values"]["unmeasurable_g3_inputs"] == list(UNMEASURABLE_G3_INPUTS)


# --- 記録する内容(allowlist・禁止項目)-----------------------------------------------


def _walk(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def test_recorded_payload_is_exactly_the_allowlist() -> None:
    """保存する項目は列挙のみ。スキーマに無いキーを足さない。"""
    audit = _SpyAuditService()
    facts = SafetyFacts(
        financials_are_stale=True,
        profit_taking_mitigation=ProfitTakingMitigationFacts(None, False),
        corporate_action=CorporateActionFacts("EVALUATED", ("purchase_price_basis_mismatch",)),
    )

    assert _run(_rec(), facts, audit_service=audit)

    call = audit.calls[0]
    assert tuple(call["input_values"]) == INPUT_VALUE_KEYS
    assert tuple(call["input_values"]["facts"]) == FACT_KEYS
    assert tuple(call["output_values"]) == OUTPUT_VALUE_KEYS
    assert call["audit_id"] == shadow_audit_id("rec-457-1") == "judgment_safety_shadow:rec-457-1"
    assert call["decision_type"] == "judgment_safety_shadow"
    assert call["stock_code"] == "0000"
    assert call["rule_version"] == "v1-test"
    assert call["data_sources"] == []
    assert call["calculation_formulas"] == {
        "evaluator": "evaluate_safety_conditions",
        "schema_version": "1",
    }
    assert call["input_values"]["schema_version"] == 1
    assert call["input_values"]["shadow_mode"] == "SHADOW"
    assert call["input_values"]["recommendation_type"] == "BUY"
    assert call["input_values"]["buy_action"] == "BUY"
    assert call["input_values"]["earnings_date_status"] == "CONFIRMED"
    assert call["input_values"]["facts"]["profit_taking_mitigation"] == {
        "continuous_dividend_increase_years": None,
        "is_progressive_or_doe_policy": False,
    }


def test_recorded_payload_contains_no_forbidden_field_or_value() -> None:
    """禁止項目(holding_id・owner・価格・数量・銘柄名・理由文・例外文)を、再帰的に走査して含まない。"""
    audit = _SpyAuditService()
    facts = SafetyFacts(
        financials_are_stale=True,
        profit_taking_mitigation=ProfitTakingMitigationFacts(None, None),
        corporate_action=CorporateActionFacts("EVALUATED", ("price_discontinuity_unexplained",)),
    )
    for recommendation, engine in (
        (_rec(), ENGINE_BUY_CANDIDATES),
        (_full_take(), ENGINE_HOLDINGS_PROFIT_TAKING),
    ):
        assert _run(recommendation, facts, engine=engine, audit_service=audit)

    forbidden_keys = {
        "holding_id",
        "owner",
        "shares",
        "average_purchase_price",
        "price",
        "stock_name",
        "reasons",
        "description",
        "suppressed_values",
        "message",
        "error",
    }
    forbidden_values = {
        "owner-a",
        "owner-a#0000",
        "架空銘柄A",
        "架空の理由文(記録へ入れてはならない)",
        "1234.56",
        "987.65",
        "2222.22",
        321,
    }
    for call in audit.calls:
        keys_and_values = list(_walk(call["input_values"])) + list(_walk(call["output_values"]))
        keys_and_values += [call["audit_id"], call["stock_code"]]
        for item in keys_and_values:
            assert item not in forbidden_keys, item
        # 数値・文字列の値に、禁止した値が含まれない(部分一致も含めて確認)。
        for item in keys_and_values:
            for bad in forbidden_values:
                if isinstance(bad, str) and isinstance(item, str):
                    assert bad not in item, (bad, item)
                else:
                    assert item != bad or isinstance(item, bool), (bad, item)
        assert "owner" not in call["audit_id"]


# --- 冪等・VALIDATION ------------------------------------------------------------


def test_same_recommendation_recorded_only_once_via_record_if_absent(tmp_path: Path) -> None:
    """同じrecommendation_idで2回呼んでも、監査ログには1件(冪等)。"""
    repository = AuditLogRepository(tmp_path)
    audit = AuditService(repository, execution_context=_NORMAL)

    first = _run(_rec(), SafetyFacts(), audit_service=audit)
    second = _run(_rec(), SafetyFacts(), audit_service=audit)

    assert (first, second) == (True, False)
    entries = repository.list_by_decision_type(DECISION_TYPE)
    assert [e.audit_id for e in entries] == ["judgment_safety_shadow:rec-457-1"]


def test_validation_mode_does_not_persist_the_record(tmp_path: Path) -> None:
    """VALIDATIONでは監査ログへ保存しない(呼び出し元の既存のifに加え、AuditService自身も抑止する)。"""
    repository = AuditLogRepository(tmp_path)
    validation = ExecutionContext(mode=ExecutionMode.VALIDATION)
    audit = AuditService(repository, execution_context=validation)

    _run(_rec(), SafetyFacts(), audit_service=audit)

    assert repository.list_by_decision_type(DECISION_TYPE) == []


# --- 失敗の隔離 ------------------------------------------------------------------


def test_record_failure_is_isolated_and_not_retried() -> None:
    """記録(AuditService)の失敗は例外にならず、リトライもしない。"""
    audit = _SpyAuditService(raises=PermissionError("AccessDenied(架空)"))

    recorded = _run(_rec(), SafetyFacts(), audit_service=audit)

    assert recorded is False
    assert len(audit.calls) == 1  # リトライしない


def test_evaluation_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """評価関数の例外も本流へ出ない(評価と記録の両方が隔離の内側にある)。"""

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("評価の失敗(架空)")

    monkeypatch.setattr(shadow_module, "evaluate_safety_conditions", _boom)
    audit = _SpyAuditService()

    recorded = _run(_rec(), SafetyFacts(), audit_service=audit)

    assert recorded is False
    assert audit.calls == []


def test_facts_supply_failure_is_isolated() -> None:
    """事実の取得(outcome.safety_facts)の失敗も隔離される(OFFではそもそも読まない)。"""

    class _Broken:
        @property
        def safety_facts(self) -> SafetyFacts:
            raise AttributeError("safety_facts が無い(架空)")

    audit = _SpyAuditService()

    recorded = observe_judgment_safety_shadow(
        _rec(),
        _Broken(),
        ENGINE_BUY_CANDIDATES,
        _NOW,
        execution_context=_NORMAL,
        audit_service=cast(AuditService, audit),
        shadow_config=_SHADOW,
    )

    assert recorded is False
    assert audit.calls == []


def test_failure_log_does_not_contain_stock_code_or_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """失敗の警告ログ(S-20)には、銘柄コード・例外メッセージを出さない(型名のみ)。"""
    audit = _SpyAuditService(raises=PermissionError("secret-detail-架空"))

    with caplog.at_level("WARNING"):
        _run(_rec(), SafetyFacts(), audit_service=audit)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "judgment_safety_shadow" in text
    assert "secret-detail" not in text
    assert "0000" not in text

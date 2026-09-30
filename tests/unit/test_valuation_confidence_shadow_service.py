"""Issue #582: 適正価格信頼度(valuation_confidence)のHIGH tier到達可否shadow計測(サービス)。

不変条件を固定する:
  * OFFなら、candidateの再計算も監査記録も呼ばない(inputsの読み取りもしない)。
  * SHADOWでも、再計算・記録の失敗は隔離され、例外が呼び出し元へ出ない。リトライしない。
  * 記録は決定的なaudit_idで冪等。
  * 保存する内容は allowlist の列挙のみ。禁止項目(holding_id・owner・銘柄名等)を含まない。
  * shadow ON/OFFで既存のRecommendation・buy_action・価格は一切変わらない
    (本module・buy_signal_service.py双方とも、既存のRecommendation構築後にのみ
    read-onlyで動く)。

★ 銘柄コードは実在しない0000系、所有者・保有数量・価格は架空値のみ。
   Productionへは一切アクセスしない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, cast

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    EarningsDateStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.signals.valuation_confidence_shadow_config import (
    ShadowMode,
    ValuationConfidenceShadowConfig,
    load_valuation_confidence_shadow_config,
)
from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels
from jstock_advisor.domain.valuation.margin_of_safety import compute_margin_of_safety
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence
from jstock_advisor.domain.valuation.valuation_methods import (
    compute_valuation_anchor,
    determine_dispersion_band,
)
from jstock_advisor.services import valuation_confidence_shadow_service as shadow_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.valuation_confidence_shadow_service import (
    DECISION_TYPE,
    ValuationConfidenceShadowInputs,
    build_shadow_record,
    observe_valuation_confidence_shadow,
    shadow_audit_id,
)

_NOW = dt.datetime(2026, 9, 28, 9, 0, tzinfo=dt.UTC)
_SHADOW = ValuationConfidenceShadowConfig(mode=ShadowMode.SHADOW)
_OFF = ValuationConfidenceShadowConfig()
_NORMAL_EXECUTION_CONTEXT = (
    object()
)  # 本サービスはisolated build内でしかexecution_contextを使わない
_CONFIG = load_config()


def _methods() -> list[FairValueMethodResult]:
    return [
        FairValueMethodResult(
            method="per", fair_value=Decimal("950"), confidence=ConfidenceLevel.HIGH
        ),
        FairValueMethodResult(
            method="pbr", fair_value=Decimal("1000"), confidence=ConfidenceLevel.HIGH
        ),
        FairValueMethodResult(
            method="target_yield", fair_value=Decimal("1050"), confidence=ConfidenceLevel.HIGH
        ),
    ]


def _valuation_summary() -> FairValueRange:
    methods = _methods()
    return FairValueRange(
        bear=Decimal("950"),
        neutral=Decimal("1000"),
        bull=Decimal("1050"),
        overall_confidence=ConfidenceLevel.HIGH,
        methods_used=methods,
        methods_excluded=[],
        usable_for_trading_judgment=True,
        valuation_dispersion_ratio=1050 / 950,
        methods_used_count=len(methods),
    )


def _rec(
    *,
    recommendation_id: str = "rec-582-1",
    buy_action: BuyAction | None = BuyAction.SMALL_ENTRY,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        # 所有者・holding_idを含む値をあえて持たせ、shadowの記録へ漏れないことを確認する。
        owner="owner-a",
        holding_id="owner-a#0000",
        stock_code="0000",
        stock_name="架空銘柄A",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_action=buy_action,
        raw_buy_action=buy_action,
        price_at_recommendation=Decimal("900"),
        confidence=ConfidenceLevel.MEDIUM,
        reasons=["架空の理由文(記録へ入れてはならない)"],
        rule_version="v1-test",
        earnings_date_status=EarningsDateStatus.CONFIRMED,
    )


def _actual_medium_chain(
    *, industry_model_applied: bool = False
) -> tuple[ConfidenceLevel, Decimal | None, Decimal | None, Decimal | None]:
    """require_industry_model省略(=True、本番の実際の経路)で、baselineの実際の結果を計算する。"""
    vs = _valuation_summary()
    dispersion_band = determine_dispersion_band(
        vs.valuation_dispersion_ratio, _CONFIG.buy_decision.valuation_dispersion
    )
    confidence = determine_valuation_confidence(
        methods_used_count=vs.methods_used_count or 0,
        dispersion_ratio=vs.valuation_dispersion_ratio,
        dispersion_medium_max=_CONFIG.buy_decision.valuation_dispersion.medium_max,
        dispersion_anchor_block=_CONFIG.buy_decision.valuation_dispersion.anchor_block,
        industry_model_applied=industry_model_applied,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    anchor = compute_valuation_anchor(
        vs, confidence.level, dispersion_band, _CONFIG.valuation.fair_value_methods.method_weights
    )
    margin = compute_margin_of_safety(confidence.level, [], _CONFIG.buy_decision.margin_of_safety)
    levels = compute_buy_price_levels(anchor.anchor, margin)
    return (
        confidence.level,
        levels.entry.price if levels.entry else None,
        levels.standard.price if levels.standard else None,
        levels.strong.price if levels.strong else None,
    )


def _candidate_high_chain() -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """require_industry_model=Falseで実際にHIGHへ変わった場合の、価格を独立に算出する

    (build_shadow_record()とは別に、実際の純関数を直接呼んで期待値を作る。
    サブちゃんレビュー対応PR #704 MUST2: candidate側がactual_confidence
    〔MEDIUM〕を取り違えて使っても本関数の期待値とは無関係に実行されるため、
    価格差分が非ゼロであることを固定できる)。
    """
    vs = _valuation_summary()
    dispersion_band = determine_dispersion_band(
        vs.valuation_dispersion_ratio, _CONFIG.buy_decision.valuation_dispersion
    )
    anchor = compute_valuation_anchor(
        vs,
        ConfidenceLevel.HIGH,
        dispersion_band,
        _CONFIG.valuation.fair_value_methods.method_weights,
    )
    margin = compute_margin_of_safety(
        ConfidenceLevel.HIGH, [], _CONFIG.buy_decision.margin_of_safety
    )
    levels = compute_buy_price_levels(anchor.anchor, margin)
    return (
        levels.entry.price if levels.entry else None,
        levels.standard.price if levels.standard else None,
        levels.strong.price if levels.strong else None,
    )


def _inputs(**overrides: Any) -> ValuationConfidenceShadowInputs:
    actual_confidence, actual_entry, actual_standard, actual_strong = _actual_medium_chain()
    defaults: dict[str, Any] = dict(
        valuation_summary=_valuation_summary(),
        dispersion_band="LOW",
        industry_model_applied=False,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
        adjustment_codes=(),
        data_quality_warning=False,
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        current_price=Decimal("900"),
        company_quality_score=70.0,
        business_days_to_earnings=30,
        config=_CONFIG,
        actual_confidence=actual_confidence,
        actual_reasons_not_high=("業種別適正価格モデル未適用",),
        actual_entry_price=actual_entry,
        actual_standard_price=actual_standard,
        actual_strong_price=actual_strong,
        actual_buy_action=BuyAction.SMALL_ENTRY,
        actual_raw_buy_action=BuyAction.SMALL_ENTRY,
    )
    defaults.update(overrides)
    return ValuationConfidenceShadowInputs(**defaults)


class _Outcome:
    def __init__(self, inputs: ValuationConfidenceShadowInputs | None) -> None:
        self.valuation_confidence_shadow_inputs = inputs


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
    inputs: ValuationConfidenceShadowInputs | None,
    *,
    config: ValuationConfidenceShadowConfig = _SHADOW,
    audit_service: Any = None,
) -> bool:
    return observe_valuation_confidence_shadow(
        recommendation,
        _Outcome(inputs),
        _NOW,
        execution_context=cast(Any, _NORMAL_EXECUTION_CONTEXT),
        audit_service=audit_service,
        shadow_config=config,
    )


# --- build_shadow_record: candidateの再計算とdiffの正しさ ---------------------------


def test_build_shadow_record_recomputes_candidate_with_require_industry_model_false() -> None:
    """candidateはrequire_industry_model=Falseで再計算され、HIGHへ到達する。"""
    inputs = _inputs()

    input_values, output_values = build_shadow_record(_rec(), inputs)

    assert input_values["require_industry_model"] is False
    assert output_values["actual_confidence"] == "MEDIUM"
    assert output_values["candidate_confidence"] == "HIGH"
    assert output_values["confidence_changed"] is True
    assert output_values["candidate_reasons_not_high"] == []
    assert output_values["reasons_not_high_diff"]["removed"] == ["業種別適正価格モデル未適用"]


def test_build_shadow_record_medium_to_high_fixture_produces_nonzero_price_diffs() -> None:
    """統合テスト(サブちゃんレビュー対応PR #704 MUST2。USER指定TEST_PLAN
    「実際にMEDIUM→HIGHへ変わるfixtureで6項目を固定」)。

    candidate側のanchor/margin計算でcandidate_confidenceの代わりに
    actual_confidence(MEDIUM)を誤って使う変異を入れても、修正前は
    MEDIUM→HIGHへ変化した既存テスト(confidence自体)しか検証していなかった
    ため検出できなかった(サブちゃん実測: mutation N6/N7 SURVIVED、449件中
    1件も検出せず)。価格差分(entry/standard/strong)を、
    build_shadow_record()とは独立に計算した期待値(_candidate_high_chain)
    と比較して固定することで、この変異を検出できるようにする。
    """
    inputs = _inputs()
    expected_entry, expected_standard, expected_strong = _candidate_high_chain()

    _, output_values = build_shadow_record(_rec(), inputs)

    # MEDIUM(actual)とHIGH(candidate)は安全余裕率テーブルが異なるため、
    # 有効なanchorが存在する限り価格は必ず異なる(0では比較不能でNoneになるため、
    # まずNoneでないことも確認する)。
    assert output_values["entry_price_diff_pct"] is not None
    assert output_values["standard_price_diff_pct"] is not None
    assert output_values["strong_price_diff_pct"] is not None
    assert output_values["entry_price_diff_pct"] != 0.0
    assert output_values["standard_price_diff_pct"] != 0.0
    assert output_values["strong_price_diff_pct"] != 0.0

    # 独立に算出した期待値(HIGH tier基準)と完全一致することを固定する
    # (candidate側が誤ってactual_confidence=MEDIUMを使っていた場合、
    # ここがMEDIUM tier価格になり不一致となって検出される)。
    actual_confidence, actual_entry, actual_standard, actual_strong = _actual_medium_chain()
    assert actual_entry is not None and expected_entry is not None
    assert actual_standard is not None and expected_standard is not None
    assert actual_strong is not None and expected_strong is not None
    expected_entry_diff = float((expected_entry - actual_entry) / actual_entry * 100)
    expected_standard_diff = float((expected_standard - actual_standard) / actual_standard * 100)
    expected_strong_diff = float((expected_strong - actual_strong) / actual_strong * 100)
    assert output_values["entry_price_diff_pct"] == pytest.approx(expected_entry_diff)
    assert output_values["standard_price_diff_pct"] == pytest.approx(expected_standard_diff)
    assert output_values["strong_price_diff_pct"] == pytest.approx(expected_strong_diff)


def test_build_shadow_record_uses_simplified_dcf_still_blocks_candidate_high() -> None:
    """サブちゃんレビュー対応PR #704 SHOULD: inputs.uses_simplified_dcfがcandidate側の
    determine_valuation_confidence()へ正しく渡っていることを固定する
    (誤った定数へ固定する変異が入ると、この理由が消えてHIGHへ到達してしまう)。
    """
    inputs = _inputs(uses_simplified_dcf=True)

    _, output_values = build_shadow_record(_rec(), inputs)

    assert output_values["candidate_confidence"] == "MEDIUM"
    assert output_values["candidate_reasons_not_high"] == [
        "簡易DCF(固定割引率・固定成長率の前提)を使用"
    ]


def test_build_shadow_record_normalized_eps_confidence_flows_to_candidate() -> None:
    """サブちゃんレビュー対応PR #704 SHOULD: inputs.normalized_eps_confidenceが
    candidate側へ正しく渡っていることを固定する。"""
    inputs = _inputs(normalized_eps_confidence=ConfidenceLevel.MEDIUM)

    _, output_values = build_shadow_record(_rec(), inputs)

    assert output_values["candidate_confidence"] == "MEDIUM"
    assert output_values["candidate_reasons_not_high"] == ["平準化EPSの信頼度が十分でない"]


def test_build_shadow_record_adjustment_codes_flow_to_candidate_margin() -> None:
    """サブちゃんレビュー対応PR #704 SHOULD: inputs.adjustment_codesが
    candidate側のcompute_margin_of_safety()へ正しく渡っていることを固定する
    (誤った定数〔空リスト等〕へ固定する変異が入ると、加算が消えて価格差分が
    変わらなくなる)。"""
    without_adjustment = _inputs()
    with_adjustment = _inputs(adjustment_codes=("very_high_valuation_dispersion",))

    _, output_without = build_shadow_record(_rec(), without_adjustment)
    _, output_with = build_shadow_record(_rec(), with_adjustment)

    assert output_with["entry_price_diff_pct"] != output_without["entry_price_diff_pct"]


def test_build_shadow_record_action_transition_format() -> None:
    """サブちゃんレビュー対応PR #704 SHOULD: action_transitionが
    "{actual}->{candidate}" の形式で記録され、actualがNoneの場合は
    "NONE"へ落ちることを固定する(フィールドを落とす変異を検出する)。"""
    none_inputs = _inputs(actual_buy_action=None)
    _, none_output = build_shadow_record(_rec(), none_inputs)
    assert none_output["action_transition"] == f"NONE->{none_output['candidate_buy_action']}"

    set_inputs = _inputs(actual_buy_action=BuyAction.WATCH_FOR_PRICE)
    _, set_output = build_shadow_record(_rec(), set_inputs)
    assert (
        set_output["action_transition"] == f"WATCH_FOR_PRICE->{set_output['candidate_buy_action']}"
    )


def test_build_shadow_record_price_diffs_are_zero_when_prices_are_identical() -> None:
    """actual/candidateの価格が同じ(=confidenceが変わらない)場合、diff%は0.0になる。"""
    actual_confidence, entry, standard, strong = _actual_medium_chain(industry_model_applied=True)
    inputs = _inputs(
        industry_model_applied=True,  # candidateもrequire_industry_model無関係でHIGH据え置き
        actual_confidence=actual_confidence,
        actual_reasons_not_high=(),
        actual_entry_price=entry,
        actual_standard_price=standard,
        actual_strong_price=strong,
    )

    _, output_values = build_shadow_record(_rec(), inputs)

    assert output_values["confidence_changed"] is False
    assert output_values["entry_price_diff_pct"] == 0.0
    assert output_values["standard_price_diff_pct"] == 0.0
    assert output_values["strong_price_diff_pct"] == 0.0


def test_build_shadow_record_price_diff_is_none_when_actual_price_is_none() -> None:
    """actual側が価格を持たない(confidence=LOW等)場合、diff%はNone(比較不能)になる。"""
    inputs = _inputs(actual_entry_price=None, actual_standard_price=None, actual_strong_price=None)

    _, output_values = build_shadow_record(_rec(), inputs)

    assert output_values["entry_price_diff_pct"] is None
    assert output_values["standard_price_diff_pct"] is None
    assert output_values["strong_price_diff_pct"] is None


def test_build_shadow_record_does_not_leak_owner_or_holding_id() -> None:
    """allowlist: 保存禁止項目(owner・holding_id・銘柄名)が記録内容に含まれない。"""
    inputs = _inputs()

    input_values, output_values = build_shadow_record(_rec(), inputs)

    serialized = repr(input_values) + repr(output_values)
    assert "owner-a" not in serialized
    assert "架空銘柄A" not in serialized
    assert "holding_id" not in input_values
    assert "holding_id" not in output_values


# --- OFFなら何もしない ------------------------------------------------------------


def test_off_never_recomputes_or_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF(既定): candidateの再計算も監査記録も呼ばない。inputsの読み取りもしない。"""

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("shadow OFF なのに実行された")

    monkeypatch.setattr(shadow_module, "_run_candidate_chain", _must_not_run)
    audit = _SpyAuditService()

    class _ExplodingOutcome:
        @property
        def valuation_confidence_shadow_inputs(self) -> ValuationConfidenceShadowInputs:
            raise AssertionError("shadow OFF なのにinputsを読んだ")

    recorded = observe_valuation_confidence_shadow(
        _rec(),
        _ExplodingOutcome(),
        _NOW,
        execution_context=cast(Any, _NORMAL_EXECUTION_CONTEXT),
        audit_service=cast(AuditService, audit),
        shadow_config=_OFF,
    )

    assert recorded is False
    assert audit.calls == []


def test_default_config_is_the_shipped_off_config() -> None:
    """shadow_configを渡さない既定は、出荷configを読む(mode: "OFF")。何も記録しない。"""
    audit = _SpyAuditService()

    recorded = observe_valuation_confidence_shadow(
        _rec(),
        _Outcome(_inputs()),
        _NOW,
        execution_context=cast(Any, _NORMAL_EXECUTION_CONTEXT),
        audit_service=cast(AuditService, audit),
    )

    assert recorded is False
    assert audit.calls == []
    assert load_valuation_confidence_shadow_config().enabled is False  # 出荷既定の確認


def test_inputs_none_does_not_record_even_when_shadow_is_on() -> None:
    """SHADOWでも、inputsがNone(推奨が生成されなかった経路)なら何もしない。"""
    audit = _SpyAuditService()

    recorded = _run(_rec(), None, audit_service=cast(AuditService, audit))

    assert recorded is False
    assert audit.calls == []


# --- SHADOWで正しく記録する ----------------------------------------------------------


def test_shadow_records_with_decision_type_and_deterministic_audit_id() -> None:
    audit = _SpyAuditService()
    rec = _rec(recommendation_id="rec-582-42")

    recorded = _run(rec, _inputs(), audit_service=cast(AuditService, audit))

    assert recorded is True
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["decision_type"] == DECISION_TYPE
    assert call["audit_id"] == shadow_audit_id("rec-582-42")
    assert call["stock_code"] == "0000"
    assert call["rule_version"] == "v1-test"
    assert call["timestamp"] == _NOW


def test_shadow_is_idempotent_on_duplicate_delivery() -> None:
    """同じrecommendation_idで2回呼んでも、2回目はrecord_if_absentがNoneを返しFalseになる。"""
    audit = _SpyAuditService()
    rec = _rec()

    first = _run(rec, _inputs(), audit_service=cast(AuditService, audit))
    second = _run(rec, _inputs(), audit_service=cast(AuditService, audit))

    assert first is True
    assert second is False
    assert len(audit.calls) == 2  # 呼び出し自体は2回発生するが、2回目は新規記録にならない


def test_recording_failure_is_isolated_and_returns_false() -> None:
    """記録(AuditService)が例外を送出しても、本流へ伝播せずFalseを返す。"""
    audit = _SpyAuditService(raises=RuntimeError("boom"))

    recorded = _run(_rec(), _inputs(), audit_service=cast(AuditService, audit))

    assert recorded is False


def test_candidate_recomputation_failure_is_isolated_and_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """candidateの再計算自体が例外を送出しても、本流へ伝播せずFalseを返す。"""

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("candidate recompute exploded")

    monkeypatch.setattr(shadow_module, "_run_candidate_chain", _boom)
    audit = _SpyAuditService()

    recorded = _run(_rec(), _inputs(), audit_service=cast(AuditService, audit))

    assert recorded is False
    assert audit.calls == []

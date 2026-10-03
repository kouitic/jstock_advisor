"""Issue #720: 利確gateが実際に候補を抑制したかの因果追跡(観測のみ)。

## 確認する契約

- 純粋関数(`build_profit_taking_gate_trace`)は、遮断側の入力を1つずつ通過側へ
  置換して`evaluate_profit_taking()`を再評価し、USER決定が列挙した5項目
  (gate_name / gate_result / candidate_action / actually_suppressed /
  superseded_by)**だけ**を記録する。実際の`evaluate_profit_taking()`を通して
  値を固定する(fixtureへ結果を直接差し込むだけにしない)。
- service経由(`ProfitTakingService.analyze()`)で、監査記録の`output_values`へ
  `profit_taking_gate_trace`が**値つきで**入る(キー名・gate_result・
  actually_suppressedの取り違え、配線の無効化で落ちる)。
- 記録の失敗は利確判定・戻り値・他のoutput_valuesを変えない(fail-soft。
  `isolated_shadow_computation`)。失敗は空listへ偽装せず「算出できなかった」
  として残る。
- 判定不変: traceの有無で、Recommendation・他のoutput_valuesは完全一致する。
- 外部I/Oを増やさない: 企業行動providerの呼び出しは保有1件あたり1回のまま。

時刻は固定(`_NOW`)。実時計に依存しない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    IndustryClassification,
    RecommendationType,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    ProfitTakingResult,
    evaluate_profit_taking,
)
from jstock_advisor.domain.signals.profit_taking_gate_trace import (
    build_profit_taking_gate_trace,
)
from jstock_advisor.services import profit_taking_service as service_module
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.stock_analysis_view_service import (
    _PROFIT_TAKING_HOLD_AUDIT_KEYS,
)
from tests.unit.test_profit_taking import _FULL_GATE_INPUTS, _fair_value_range
from tests.unit.test_profit_taking_service import (
    _CONFIG,
    _NOW,
    _canned_result,
    _holding,
    _providers,
    _RecordingCorporateActionProvider,
)

_CBJ = _CONFIG.profit_taking.condition_based_judgment
_MIN_DAYS = _CBJ.min_business_days_to_earnings_for_fair_value_action
_TRACE_KEY = "profit_taking_gate_trace"


# ============================================================================
# 純粋関数(実際のevaluate_profit_taking()を通す)
# ============================================================================


def _real_evaluate_factory(price: str):  # type: ignore[no-untyped-def]
    def _evaluate(inputs: ProfitTakingConditionInputs) -> ProfitTakingResult:
        return evaluate_profit_taking(
            current_price=Decimal(price),
            average_purchase_price=Decimal("1000"),
            shares=100,
            total_purchase_amount=Decimal("100000"),
            cumulative_dividend_received=Decimal("0"),
            cumulative_benefit_value_received=Decimal("0"),
            current_total_yield_pct=None,
            forecast_annual_dividend_per_share=None,
            mitigating_inputs=MitigatingFactorInputs(),
            config=_CONFIG.profit_taking,
            condition_inputs=inputs,
        )

    return _evaluate


def _fv_strong_inputs(**gate_overrides: Any) -> ProfitTakingConditionInputs:
    """適正価格ベースの強い判定(HIGH信頼度・手法3・全gate通過)が成立する入力。"""
    return ProfitTakingConditionInputs(
        fair_value_range=_fair_value_range(
            neutral=Decimal("650"),
            bull=Decimal("700"),
            bear=Decimal("600"),
            overall_confidence=ConfidenceLevel.HIGH,
            method_count=3,
        ),
        guidance_revision_disclosed=True,
        fair_value_reflects_latest_earnings=True,
        **dict(_FULL_GATE_INPUTS, **gate_overrides),
    )


def _trace(inputs: ProfitTakingConditionInputs, price: str) -> list[dict[str, object]]:
    evaluate = _real_evaluate_factory(price)
    return build_profit_taking_gate_trace(inputs, evaluate, evaluate(inputs), _MIN_DAYS)


def test_no_blocked_input_produces_an_empty_trace() -> None:
    assert _trace(_fv_strong_inputs(), "1010") == []


_APPROVED_RECORD_KEYS = {
    "gate_name",
    "gate_result",
    "candidate_action",
    "actually_suppressed",
    "superseded_by",
}


@pytest.mark.parametrize(
    ("override", "gate_name"),
    [
        ({"industry_model_applied": False}, "INDUSTRY_MODEL_NOT_APPLIED"),
        ({"days_to_next_earnings_business_days": None}, "EARNINGS_DAYS_UNKNOWN"),
        ({"days_to_next_earnings_business_days": 1}, "EARNINGS_TOO_CLOSE"),
        ({"partial_sale_executable": False}, "PARTIAL_SALE_NOT_EXECUTABLE"),
        ({"has_strong_counter_material": True}, "STRONG_COUNTER_MATERIAL_PRESENT"),
    ],
)
def test_each_blocked_input_actually_suppressing_fv_strong_full_is_recorded(
    override: dict[str, Any], gate_name: str
) -> None:
    """適正価格ベースのFULLが成立する入力で、gateを1つだけ閉じるとWATCHへ落ちる。
    そのgateは「実際に抑制した」として、承認された5項目だけで値つきに残る。"""
    records = _trace(_fv_strong_inputs(**override), "1010")

    assert records == [
        {
            "gate_name": gate_name,
            "gate_result": "BLOCKED",
            "candidate_action": RecommendationType.FULL_PROFIT_TAKE.value,
            "actually_suppressed": True,
            "superseded_by": None,
        }
    ]


def test_gate_covered_by_another_path_is_not_counted_as_suppression() -> None:
    """価格位置(PRICE_POSITION)が独立にFULLへ届く場合、適正価格ベースのgateが
    閉じていても判定は変わらない。抑制ではなくORIGIN_ONLY(superseded_by=採用された
    根拠)として区別される(静的な「他の条件が全部真か」では区別できないケース)。"""
    inputs = dataclasses.replace(
        _fv_strong_inputs(partial_sale_executable=False),
        industry_classification=IndustryClassification.GENERAL_CORPORATE,
    )

    records = _trace(inputs, "1600")

    baseline_action = _real_evaluate_factory("1600")(inputs).final_action.value
    assert records == [
        {
            "gate_name": "PARTIAL_SALE_NOT_EXECUTABLE",
            "gate_result": "BLOCKED",
            # 開けても最終判定は変わらない(他の経路が既にカバーしている)
            "candidate_action": baseline_action,
            "actually_suppressed": False,
            "superseded_by": "PRICE_POSITION",
        }
    ]


def test_the_same_input_closing_two_families_still_suppresses_when_other_path_is_closed() -> None:
    """決算直前は適正価格ベース(1)と上限価格の利用可否(2)を同時に閉じる。
    PRICE_POSITIONも同時に閉じるため、同じ価格位置でも今度はACTION_CHANGEDになる
    (同じ入力が複数familyへ効く実例)。"""
    inputs = dataclasses.replace(
        _fv_strong_inputs(days_to_next_earnings_business_days=1),
        industry_classification=IndustryClassification.GENERAL_CORPORATE,
    )

    records = _trace(inputs, "1600")

    assert [r["gate_name"] for r in records] == ["EARNINGS_TOO_CLOSE"]
    assert records[0]["actually_suppressed"] is True
    assert records[0]["superseded_by"] is None


def test_counterfactual_passes_the_earnings_threshold_exactly() -> None:
    """決算日数の通過側の値はconfigの下限値ちょうど(gateは`>=`)。"""
    seen: list[int | None] = []

    def _evaluate(inputs: ProfitTakingConditionInputs) -> ProfitTakingResult:
        seen.append(inputs.days_to_next_earnings_business_days)
        return _canned_result(RecommendationType.WATCH)

    # 遮断側の入力が決算日数だけになるよう、他のgateは通過側にしておく。
    inputs = ProfitTakingConditionInputs(
        industry_model_applied=True, days_to_next_earnings_business_days=1
    )
    build_profit_taking_gate_trace(inputs, _evaluate, _evaluate(inputs), _MIN_DAYS)

    assert seen == [1, _MIN_DAYS]


def test_joint_only_when_only_passing_every_blocked_input_changes_the_action() -> None:
    """単独では変わらないが、遮断中の全入力を同時に通すと変わる場合はJOINT_ONLY。"""

    def _evaluate(inputs: ProfitTakingConditionInputs) -> ProfitTakingResult:
        ok = inputs.partial_sale_executable and not inputs.has_strong_counter_material
        action = RecommendationType.FULL_PROFIT_TAKE if ok else RecommendationType.WATCH
        return dataclasses.replace(_canned_result(action), final_action=action, origin="NONE")

    inputs = ProfitTakingConditionInputs(
        industry_model_applied=True,
        days_to_next_earnings_business_days=_MIN_DAYS,
        partial_sale_executable=False,
        has_strong_counter_material=True,
    )

    records = build_profit_taking_gate_trace(inputs, _evaluate, _evaluate(inputs), _MIN_DAYS)

    assert [r["gate_name"] for r in records] == [
        "PARTIAL_SALE_NOT_EXECUTABLE",
        "STRONG_COUNTER_MATERIAL_PRESENT",
    ]
    assert all(r["actually_suppressed"] is True for r in records)
    assert all(r["superseded_by"] is None for r in records)
    # candidate_actionは「そのgateを開けた場合の候補」。単独では変わらないため、
    # 遮断中の全入力を同時に開けたときの候補(FULL)を記録する(単独の再評価結果の
    # WATCHは実際の判定と同じで、「抑制された」記録と矛盾して見えるため)。
    full = RecommendationType.FULL_PROFIT_TAKE.value
    assert all(r["candidate_action"] == full for r in records)


def test_a_record_carries_exactly_the_five_approved_fields() -> None:
    """USER決定が列挙した5項目だけを記録する(拡張項目を紛れ込ませない)。
    抑制・非抑制(他の経路がカバー)・JOINT・変化なしのどの場合も同じ5項目。"""
    suppressing = _trace(_fv_strong_inputs(partial_sale_executable=False), "1010")
    covered = _trace(
        dataclasses.replace(
            _fv_strong_inputs(partial_sale_executable=False),
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
        "1600",
    )
    unchanged = _trace(
        _fv_strong_inputs(partial_sale_executable=False, has_strong_counter_material=True),
        "1010",
    )

    def _joint_evaluate(inputs: ProfitTakingConditionInputs) -> ProfitTakingResult:
        ok = inputs.partial_sale_executable and not inputs.has_strong_counter_material
        action = RecommendationType.FULL_PROFIT_TAKE if ok else RecommendationType.WATCH
        return dataclasses.replace(_canned_result(action), final_action=action, origin="NONE")

    joint_inputs = ProfitTakingConditionInputs(
        industry_model_applied=True,
        days_to_next_earnings_business_days=_MIN_DAYS,
        partial_sale_executable=False,
        has_strong_counter_material=True,
    )
    joint = build_profit_taking_gate_trace(
        joint_inputs, _joint_evaluate, _joint_evaluate(joint_inputs), _MIN_DAYS
    )

    all_records = suppressing + covered + unchanged + joint
    assert len(all_records) >= 5
    for record in all_records:
        assert set(record) == _APPROVED_RECORD_KEYS


# ============================================================================
# service経由(実際のbuild_stock_snapshot()を通し、監査記録を読む)
# ============================================================================


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> object:
        self.records.append(kwargs)
        return SimpleNamespace(audit_id="audit-720-test")


def _fake_evaluate(calls: list[dict[str, Any]], *, fail_after_first: bool = False):  # type: ignore[no-untyped-def]
    """partial_sale_executableだけで判定が変わる偽のevaluate(各引数は記録する)。"""

    def _evaluate(**kwargs: Any) -> ProfitTakingResult:
        calls.append(kwargs)
        if fail_after_first and len(calls) > 1:
            raise RuntimeError("boom: counterfactual evaluation (injected)")
        action = (
            RecommendationType.FULL_PROFIT_TAKE
            if kwargs["condition_inputs"].partial_sale_executable
            else RecommendationType.WATCH
        )
        return dataclasses.replace(_canned_result(action), final_action=action, origin="NONE")

    return _evaluate


def _fake_evaluate_never_changing(calls: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    """入力を置換しても判定が変わらない偽のevaluate(呼び出し回数を数えるため)。"""

    def _evaluate(**kwargs: Any) -> ProfitTakingResult:
        calls.append(kwargs)
        action = RecommendationType.WATCH
        return dataclasses.replace(_canned_result(action), final_action=action, origin="NONE")

    return _evaluate


def _analyze(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shares: int,
    fake: Any,
) -> tuple[Any, dict[str, Any], _RecordingCorporateActionProvider]:
    monkeypatch.setattr(service_module, "evaluate_profit_taking", fake)
    provider = _RecordingCorporateActionProvider([])
    providers = dataclasses.replace(_providers(None, None), corporate_action=provider)
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    audit = _RecordingAudit()
    monkeypatch.setattr(service, "_audit", audit)

    outcome = service.analyze(_holding("2914").model_copy(update={"shares": shares}), _NOW)

    profit_records = [r for r in audit.records if r.get("decision_type") == "profit_taking"]
    assert len(profit_records) == 1
    return outcome, profit_records[0]["output_values"], provider


def test_service_records_the_trace_with_values_through_the_real_snapshot_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """売買単位ちょうど(100株)では一部売却が実行不能。実際のsnapshot経由で
    組み立てられたcondition_inputsに対して、partial_sale_executableの遮断が
    「実際に抑制した」として値で残る。"""
    calls: list[dict[str, Any]] = []

    outcome, output_values, _ = _analyze(monkeypatch, shares=100, fake=_fake_evaluate(calls))

    assert outcome.recommendation is not None
    assert _TRACE_KEY in output_values
    by_gate = {r["gate_name"]: r for r in output_values[_TRACE_KEY]}
    partial = by_gate["PARTIAL_SALE_NOT_EXECUTABLE"]
    assert partial == {
        "gate_name": "PARTIAL_SALE_NOT_EXECUTABLE",
        "gate_result": "BLOCKED",
        "candidate_action": RecommendationType.FULL_PROFIT_TAKE.value,
        "actually_suppressed": True,
        "superseded_by": None,
    }
    # 現行のサービスは業種別モデルを常に未適用(False)として渡すため、同じ実入力に
    # 対して遮断側として記録される(他の入力は単独では判定を変えず、全入力を同時に
    # 開けたときだけ変わる)。
    industry = by_gate["INDUSTRY_MODEL_NOT_APPLIED"]
    assert industry["actually_suppressed"] is True
    assert industry["candidate_action"] == RecommendationType.FULL_PROFIT_TAKE.value


def test_service_records_nothing_for_an_input_that_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300株なら一部売却は実行可能。遮断側にない入力は記録しない。"""
    calls: list[dict[str, Any]] = []

    _, output_values, _ = _analyze(monkeypatch, shares=300, fake=_fake_evaluate(calls))

    assert "PARTIAL_SALE_NOT_EXECUTABLE" not in {r["gate_name"] for r in output_values[_TRACE_KEY]}


@pytest.mark.parametrize("shares", [100, 300])
def test_the_industry_model_gate_is_blocked_for_every_holding_with_the_real_service_inputs(
    monkeypatch: pytest.MonkeyPatch, shares: int
) -> None:
    """現状のサービスは`industry_model_applied`を定数Falseで渡す(業種別の専用モデルは
    未実装)。そのため実入力ではこの入力が**全保有で常に遮断側**となり、
    INDUSTRY_MODEL_NOT_APPLIEDの記録が必ず1件入る。これは「業種モデルの遮断が至る所で
    起きている」という発見ではなく、現在の配線の定数の反映である(監査記録を後から読む
    人が誤読しないよう、事実としてテストで固定する。配線が実値へ変われば、この
    テストとgate_trace moduleのdocstringを見直す合図になる)。

    あわせて、遮断が他に1つでもあれば遮断入力が2件以上になるため、JOINT判定の
    追加の再評価(全入力を同時に通した1回)が走る。単独で変わる入力が無い場合の
    evaluate呼び出し回数 = 実判定1 + 遮断入力ごとの単独1 + JOINT1。
    """
    calls: list[dict[str, Any]] = []
    unchanged_fake = _fake_evaluate_never_changing(calls)

    _, output_values, _ = _analyze(monkeypatch, shares=shares, fake=unchanged_fake)

    trace = output_values[_TRACE_KEY]
    assert "INDUSTRY_MODEL_NOT_APPLIED" in {r["gate_name"] for r in trace}
    assert len(trace) >= 2
    assert len(calls) == 1 + len(trace) + 1


def test_counterfactual_calls_use_the_same_arguments_as_the_real_judgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反実仮想の再評価は、condition_inputs以外の引数が実判定と完全に同じ
    (引数を2か所で別々に書いて食い違わせない)。"""
    calls: list[dict[str, Any]] = []

    _analyze(monkeypatch, shares=100, fake=_fake_evaluate(calls))

    assert len(calls) >= 2
    others = [{k: v for k, v in c.items() if k != "condition_inputs"} for c in calls]
    assert all(o == others[0] for o in others)


def test_trace_failure_does_not_change_the_judgment_or_other_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反実仮想の再評価が例外を送出しても、利確判定・戻り値・他のoutput_valuesは
    変わらず、失敗は「算出できなかった」として記録側へ残る(空listへ偽装しない)。"""
    ok_outcome, ok_values, _ = _analyze(monkeypatch, shares=100, fake=_fake_evaluate([]))
    failing_outcome, failing_values, _ = _analyze(
        monkeypatch, shares=100, fake=_fake_evaluate([], fail_after_first=True)
    )

    assert failing_outcome.recommendation is not None
    assert failing_values[_TRACE_KEY] == [
        {"shadow_state": "COMPUTATION_FAILED", "error_type": "RuntimeError"}
    ]
    assert failing_values["recommendation_type"] == ok_values["recommendation_type"]
    assert failing_values["final_action"] == ok_values["final_action"]
    assert {k: v for k, v in failing_values.items() if k != _TRACE_KEY} == {
        k: v for k, v in ok_values.items() if k != _TRACE_KEY
    }
    assert (
        failing_outcome.recommendation.recommendation_type
        == ok_outcome.recommendation.recommendation_type
    )


def test_judgment_is_identical_with_and_without_the_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """traceを無効化(空list)しても、Recommendationと他のoutput_valuesは完全一致する
    (観測のみ追加で、判定・通知を変えない)。"""
    with_outcome, with_values, _ = _analyze(monkeypatch, shares=100, fake=_fake_evaluate([]))
    monkeypatch.setattr(service_module, "build_profit_taking_gate_trace", lambda *a, **k: [])
    without_outcome, without_values, _ = _analyze(monkeypatch, shares=100, fake=_fake_evaluate([]))

    def _normalized(outcome: Any) -> dict[str, Any]:
        dumped = outcome.recommendation.model_dump(mode="json")
        dumped.pop("recommendation_id", None)
        return dumped  # type: ignore[no-any-return]

    assert _normalized(with_outcome) == _normalized(without_outcome)
    assert {k: v for k, v in with_values.items() if k != _TRACE_KEY} == {
        k: v for k, v in without_values.items() if k != _TRACE_KEY
    }
    assert without_values[_TRACE_KEY] == []
    assert with_values[_TRACE_KEY] != []


def test_trace_adds_no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """gate追跡は純粋関数の再評価のみ。企業行動providerの呼び出しは保有1件あたり
    1回のまま(本変更前と同じ)。"""
    _, _, provider = _analyze(monkeypatch, shares=100, fake=_fake_evaluate([]))

    assert len(provider.calls) == 1


def test_trace_key_is_not_read_by_the_user_facing_audit_view() -> None:
    """利用者向けの保有継続理由の復元は許可リスト(_PROFIT_TAKING_HOLD_AUDIT_KEYS)
    だけを読む。新キーは許可リストに無く、表示へ漏れない。"""
    assert _TRACE_KEY not in _PROFIT_TAKING_HOLD_AUDIT_KEYS

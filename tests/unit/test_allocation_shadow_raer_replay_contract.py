"""購入側 Shadow の RAER の式の再現(`allocation_shadow_raer_replay`。Issue #934 BT-2a。#602
の再現性)の契約テスト。

固定するもの:
  (1) 本番の経路(`compute_owner`)で作った記録 → 独立に再実装した式で完全一致する(全軸 /
  リスクの重複除外と上限 /
      営業トレンドの減点 / 企業品質の coverage 不足 / Fair Value の信頼度 LOW・HIGH)
  (2) 変異(記録の値・寄与・入力・語・信頼度・状態・軸・鍵・仕様 flag の書換え)
  は食い違いのパスとして検出される
  (3) 門の順序: スキーマ → ★ モデル版 → 係数の束の識別子 ★(版が上がった記録は
  MODEL_VERSION_UNSUPPORTED で、
      COEFFICIENT_SET_MISMATCH にならない)。値の無い候補は
      RAER_UNAVAILABLE(再現の対象外・件数で別掲)
  (4) 営業トレンドの部分 coverage: ★ 記録の丸めた商で判定しない(交差乗算)★・境界 3
  ケース・片側の検査であることの
      報告・範囲外の下限は『渡されていない』・データが縛った上限は『重みの組』(商にしない)・出所
  (5) 数値の regime: 本番の定数との一致・既定の context に依存しない・Inexact を握らず
  NUMERIC_REGIME_VIOLATION(= regime_violations)に数える(NOT_REPLAYABLE に混ぜない)・
      偶数丸めが効く(四捨五入と答えが分かれる)ケース
  (6) 構造の定数が本番と一致(版・軸・リスクの語の順序・信頼度の規則・仕様 flag)
  (7) 到達: どこからも import されない。戻り値に値・銘柄を含めない
"""

from __future__ import annotations

import ast
import copy
import decimal
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.signals import allocation_shadow_raer_replay as replay
from jstock_advisor.domain.signals.allocation_shadow_raer_replay import (
    CandidateGate,
    RecordGate,
    replay_raer_in_record,
)
from jstock_advisor.domain.valuation import raer as production_raer
from jstock_advisor.domain.valuation import raer_composition as production_composition
from jstock_advisor.domain.valuation.raer_coefficients_config import load_raer_coefficients
from tests.unit import test_allocation_shadow_compute_contract as base

_BUNDLE = load_raer_coefficients()
assert _BUNDLE.coefficients is not None
COEFFICIENTS = _BUNDLE.coefficients
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_NAME = "allocation_shadow_raer_replay"


def _detection(flags: list[str]) -> dict[str, object]:
    return {
        "version": 1,
        "flags": flags,
        "observed": {"earnings_date": True, "liquidity": True, "industry": True, "eps": True},
    }


_ALL_RISKS = [
    "cyclical_industry",
    "temporary_earnings_boost_risk",
    "earnings_within_3_business_days",
    "earnings_within_7_business_days",
    "small_cap_or_low_liquidity",
    "major_customer_dependency",
]
_ABSENT = "ABSENT"


def _record(tmp_path: Path, **kw: Any) -> dict[str, Any]:
    """本番の経路で作った項目つきの記録(owner 1 件・候補 2 件)。"""

    def make(code: str) -> Any:
        facts: dict[str, Any] = {"common_quality_shadow": kw.get("quality", base._quality())}
        det = kw.get("det", base._DETECTION_OBSERVED)
        if det is not _ABSENT:
            facts["raer_risk_detection"] = det
        overrides: dict[str, Any] = {"buy_score_input_facts": facts}
        if "trend" in kw:
            overrides["earnings_trend_metrics"] = kw["trend"]
        if "conf" in kw:
            overrides["fair_value_overall_confidence"] = kw["conf"]
        overrides.update(kw.get("rec", {}))
        return base.rec(code, **overrides)

    reader = base._standard_reader(
        recs={base._rec_id(code): make(code) for code in ("0001", "0002", "9001")}
    )
    inputs = base._ready(tmp_path, reader)
    record = base._owner(inputs, reader).record
    assert record is not None
    copied: dict[str, Any] = json.loads(json.dumps(record))
    return copied


def _first(record: dict[str, Any]) -> dict[str, Any]:
    first: dict[str, Any] = record["candidates"][0]
    return first


_CASES: dict[str, dict[str, Any]] = {
    "all-axes": {},
    "risk-duplicates-and-cap": {"det": _detection(_ALL_RISKS)},
    "negative-trend": {
        "trend": {
            "state": "EVALUATED",
            "operating_income_trend_component": -60.0,
            "operating_cashflow_trend_component": -20.0,
        }
    },
    "quality-coverage-below-minimum": {
        "quality": {"shadow_state": "COMPUTED", "score": 30.0, "coverage_ratio": 0.4}
    },
    "fair-value-confidence-low": {"conf": ConfidenceLevel.LOW},
    "fair-value-confidence-high": {"conf": ConfidenceLevel.HIGH},
    # 企業品質の coverage が下限とちょうど等しい(下限以上 = 適用)
    "quality-coverage-equal-to-minimum": {
        "quality": {"shadow_state": "COMPUTED", "score": 30.0, "coverage_ratio": 0.6}
    },
    # 上値余地が負(Fair Value が価格より低い)→ Fair Value の軸の割引は max(上値余地, 0) = 0
    "negative-upside": {"rec": {"fair_value_neutral": Decimal("800")}},
}


# =================================================================================================
# (1) 本番の経路の記録が完全一致する
# =================================================================================================


@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_record_made_by_the_production_path_replays_exactly(name: str, tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES[name])

    report = replay_raer_in_record(record, COEFFICIENTS)

    assert report.record_gate is None
    assert report.candidates == 2
    assert report.replayable == 2
    assert report.mismatches == ()
    assert report.matched == 2
    assert report.regime_violations == 0
    assert report.all_replayed_matched is True


def test_the_risk_cap_is_actually_exercised_by_the_cap_case(tmp_path: Path) -> None:
    """上限で切る規則が効くケースである(数えた合計 > 上限)。効かないケースでは cap
    の再現を確かめていない。"""
    record = _record(tmp_path, **_CASES["risk-duplicates-and-cap"])
    flags = set(_first(record)["raer_out"]["risk_flags"])
    counted = replay._risk_counted(frozenset(flags), COEFFICIENTS)  # noqa: SLF001

    assert sum(counted.values()) > COEFFICIENTS.risk_deduction_cap
    assert replay_raer_in_record(record, COEFFICIENTS).all_replayed_matched


def test_a_duplicate_risk_is_excluded_by_the_replay_rules(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["risk-duplicates-and-cap"])
    recorded = _first(record)["raer_out"]["extra"]["risk_contributions"]

    assert "earnings_within_7_business_days" not in recorded  # 決算 3 営業日と併存 → 数えない
    assert not {"cyclical_industry", "temporary_earnings_boost_risk"} <= set(
        recorded
    )  # 大きい方だけ


# =================================================================================================
# (2) 変異は検出される
# =================================================================================================


def _bump_value(c: dict[str, Any]) -> None:
    c["raer_out"]["value"] = str(Decimal(c["raer_out"]["value"]) + 1)


_MUTATIONS = {
    "value": (_bump_value, "candidates[0].raer_out.value"),
    "fair-value-contribution": (
        lambda c: c["raer_out"]["extra"]["contributions"].__setitem__(
            "FAIR_VALUE_CONFIDENCE", "9.9"
        ),
        "candidates[0].raer_out.extra.contributions.FAIR_VALUE_CONFIDENCE",
    ),
    "upside": (
        lambda c: c["raer_in"].__setitem__("upside_pct", "41.0"),
        "candidates[0].raer_out.value",
    ),
    "risk-word-dropped": (
        lambda c: c["raer_out"]["risk_flags"].pop(),
        "candidates[0].raer_out.extra.risk_contributions",
    ),
    "confidence": (
        lambda c: c["raer_out"].__setitem__("confidence", "HIGH"),
        "candidates[0].raer_out.confidence",
    ),
    "state": (
        lambda c: c["raer_out"].__setitem__("state", "AVAILABLE"),
        "candidates[0].raer_out.state",
    ),
    "applied-axes": (
        lambda c: c["raer_out"]["applied_axes"].pop(),
        "candidates[0].raer_out.applied_axes",
    ),
    "comparison-key": (
        lambda c: c["raer_out"].__setitem__("comparison_key", "other"),
        "candidates[0].raer_out.comparison_key",
    ),
    "spec-flags": (
        lambda c: c["raer_out"]["extra"].__setitem__("spec_flags", []),
        "candidates[0].raer_out.extra.spec_flags",
    ),
    "base": (
        lambda c: c["raer_out"]["extra"]["base"].__setitem__("base", "1"),
        "candidates[0].raer_out.extra.base.base",
    ),
    "unapplied-axes": (
        lambda c: c["raer_out"]["unapplied_axes"].pop(),
        "candidates[0].raer_out.unapplied_axes",
    ),
    "risk-words-without-observation": (
        lambda c: c["raer_in"].__setitem__("risk_flags_observed", False),
        "candidates[0].raer_out.risk_flags",
    ),
    "extra-contribution-axis": (
        lambda c: c["raer_out"]["extra"]["contributions"].__setitem__("DATA_QUALITY", "0"),
        "candidates[0].raer_out.extra.contributions",
    ),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_a_mutated_record_is_detected_with_the_path(name: str, tmp_path: Path) -> None:
    mutate, expected_path = _MUTATIONS[name]
    record = _record(tmp_path, **_CASES["risk-duplicates-and-cap"])
    mutate(_first(record))

    report = replay_raer_in_record(record, COEFFICIENTS)

    assert expected_path in report.mismatches
    assert report.matched == 1  # もう 1 件の候補は影響を受けない
    assert report.all_replayed_matched is False


def test_a_changed_coefficient_with_a_changed_identifier_is_refused_not_replayed(
    tmp_path: Path,
) -> None:
    import dataclasses

    record = _record(tmp_path)
    changed = dataclasses.replace(
        COEFFICIENTS, quality_deduction_max=COEFFICIENTS.quality_deduction_max + 1
    )

    report = replay_raer_in_record(record, changed)

    assert changed.coefficient_set_id != COEFFICIENTS.coefficient_set_id
    assert report.record_gate is RecordGate.COEFFICIENT_SET_MISMATCH
    assert report.replayable == 0  # 別の束で黙って計算しない


# =================================================================================================
# (3) 門の順序と対象外
# =================================================================================================


def test_a_newer_model_version_is_reported_as_the_version_not_the_coefficient_set(
    tmp_path: Path,
) -> None:
    """★ 版を先に判定する ★: 識別子はモデル版を含むので、版が上がると識別子も変わる。
    理由は『式が違う』。"""
    record = _record(tmp_path)
    record["model"]["raer_model_version"] = SUPPORTED_NEWER
    record["model"]["coefficient_set_id"] = "ffffffffffffffff-c1"  # 版が上がれば識別子も変わる

    report = replay_raer_in_record(record, COEFFICIENTS)

    assert report.record_gate is RecordGate.MODEL_VERSION_UNSUPPORTED
    assert report.candidates == 2 and report.replayable == 0


SUPPORTED_NEWER = replay.SUPPORTED_MODEL_VERSION + "-next"


def test_a_different_coefficient_set_id_alone_is_the_coefficient_gate(tmp_path: Path) -> None:
    record = _record(tmp_path)
    record["model"]["coefficient_set_id"] = "ffffffffffffffff-c1"

    assert (
        replay_raer_in_record(record, COEFFICIENTS).record_gate
        is RecordGate.COEFFICIENT_SET_MISMATCH
    )


def test_a_record_of_another_shape_is_unsupported_not_an_exception(tmp_path: Path) -> None:
    for broken in (
        {**_record(tmp_path), "record_kind": "allocation_shadow_index"},
        {**_record(tmp_path), "schema_version": 2},
        {k: v for k, v in _record(tmp_path).items() if k != "candidates"},
        {**_record(tmp_path), "model": "not-a-mapping"},
        "not-a-record",
        None,
    ):
        report = replay_raer_in_record(broken, COEFFICIENTS)  # type: ignore[arg-type]

        assert report.record_gate is RecordGate.SCHEMA_UNSUPPORTED
        assert report.candidates == 0


def test_a_candidate_with_a_missing_item_is_a_mismatch_not_an_exception(tmp_path: Path) -> None:
    record = _record(tmp_path)
    del _first(record)["raer_in"]["upside_pct"]

    report = replay_raer_in_record(record, COEFFICIENTS)

    assert "candidates[0]" in report.mismatches
    assert report.matched == 1


def test_candidates_without_a_value_are_counted_apart_and_not_replayed(tmp_path: Path) -> None:
    """適用できる軸が最低軸数(3)に満たないと値が作れない → 入力は記録されず、
    再現の対象外(件数で別掲)。"""
    record = _record(
        tmp_path,
        det=_ABSENT,
        trend={},
        quality={"shadow_state": "COMPUTED", "score": 30.0, "coverage_ratio": 0.4},
    )

    report = replay_raer_in_record(record, COEFFICIENTS)

    assert all(c["raer_out"]["value"] is None for c in record["candidates"])
    assert report.record_gate is None
    assert report.replayable == 0 and report.matched == 0
    assert report.not_replayable == ((CandidateGate.RAER_UNAVAILABLE, 2),)
    assert report.mismatches == ()


# =================================================================================================
# (4) 営業トレンドの部分 coverage(交差乗算・片側・上限・出所)
# =================================================================================================


def _with_trend_weights(
    record: dict[str, Any],
    *,
    weights: dict[str, str],
    used: list[str],
    recorded_partial: str,
) -> None:
    for candidate in record["candidates"]:
        candidate["raer_in"]["trend_weights"] = dict(weights)
        candidate["raer_in"]["trend_components_used"] = list(used)
        candidate["raer_in"]["trend_partial_coverage"] = recorded_partial


def _weights(first: str, second: str, third: str, fourth: str) -> dict[str, str]:
    return {
        "operating_income": first,
        "operating_cashflow": second,
        "dividend_direction": third,
        "acceleration": fourth,
    }


def test_the_minimum_is_judged_by_cross_multiplication_not_by_the_rounded_quotient(
    tmp_path: Path,
) -> None:
    """境界ケース 1: 利用 0.2999996 / 合計 1 / 下限 0.3。商は 0.300000 に丸まり下限以上に見えるが、
    厳密には未満。"""
    record = _record(tmp_path)
    _with_trend_weights(
        record,
        weights=_weights("0.2999996", "0.4000004", "0.2", "0.1"),
        used=["operating_income"],
        recorded_partial="0.300000",
    )

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.3"),
        trend_min_coverage_source="test",
    )

    assert (
        report.trend_coverage.violations == 2
    )  # 適用された 2 候補とも(丸めた商で判定する実装なら 0 になる)
    assert "candidates[0].raer_in.trend_partial_coverage" in report.mismatches


def test_a_minimum_with_more_digits_does_not_flag_a_valid_application(tmp_path: Path) -> None:
    """境界ケース 2: 下限 0.3000005・利用 0.3000005 / 合計 1。商は 0.300000
    に丸まり下限未満に見えるが、厳密には以上。"""
    record = _record(tmp_path)
    _with_trend_weights(
        record,
        weights=_weights("0.3000005", "0.3999995", "0.2", "0.1"),
        used=["operating_income"],
        recorded_partial="0.300000",
    )

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.3000005"),
        trend_min_coverage_source="test",
    )

    assert report.trend_coverage.violations == 0
    assert report.mismatches == ()


def test_a_used_weight_exactly_at_the_minimum_is_valid(tmp_path: Path) -> None:
    """境界ケース 3: 利用 = 下限 × 合計(ちょうど)→ 下限以上 = 妥当。"""
    record = _record(tmp_path)
    _with_trend_weights(
        record,
        weights=_weights("0.3", "0.4", "0.2", "0.1"),
        used=["operating_income"],
        recorded_partial="0.300000",
    )

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.3"),
        trend_min_coverage_source="test",
    )

    assert report.trend_coverage.violations == 0
    assert report.mismatches == ()


@pytest.mark.parametrize(
    "bad", [None, Decimal(0), Decimal("-0.1"), Decimal("1.01"), "0.3", 0.3, Decimal("NaN")]
)
def test_an_out_of_range_minimum_is_treated_as_not_supplied_and_counted(
    bad: object, tmp_path: Path
) -> None:
    record = _record(tmp_path)

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=bad,  # type: ignore[arg-type]
        trend_min_coverage_source="test",
    )

    coverage = report.trend_coverage
    assert coverage.supplied is False
    assert coverage.checked == 0
    assert coverage.not_checked == coverage.applied == 2
    assert coverage.violations == 0
    assert coverage.source is None  # 検査を実行していないので、出所も載せない
    assert (
        report.mismatches == ()
    )  # 値の偽の一致は作らない: 下限が渡されなくても再現の結果は変わらない


def test_the_minimum_never_changes_the_reproduction_of_the_value(tmp_path: Path) -> None:
    record = _record(tmp_path)

    without = replay_raer_in_record(record, COEFFICIENTS)
    tiny = replay_raer_in_record(
        record, COEFFICIENTS, trend_min_coverage_required=Decimal("0.0001")
    )

    assert (without.replayable, without.matched, without.mismatches) == (
        tiny.replayable,
        tiny.matched,
        tiny.mismatches,
    )


def test_the_report_states_that_the_check_is_one_sided_and_names_the_bound(tmp_path: Path) -> None:
    record = _record(tmp_path)  # 利用した重み: 営業利益 + 営業 CF = 1.75 / 合計 2.5(shipped の重み)

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.3"),
        trend_min_coverage_source="earnings_trend_rules@test",
    )

    coverage = report.trend_coverage
    assert coverage.note == replay.TREND_COVERAGE_CHECK_IS_ONE_SIDED
    assert "片側" in coverage.note and "小さすぎる下限は検出できない" in coverage.note
    assert coverage.supplied and coverage.checked == 2 and coverage.not_checked == 0
    assert coverage.source == "earnings_trend_rules@test"
    assert coverage.upper_bound is not None
    used, total = (Decimal(x) for x in coverage.upper_bound)
    assert (used, total) == (Decimal("1.75"), Decimal("2.5"))  # 商にしない: 2 つの値の組


def test_a_tiny_minimum_checks_nothing_but_the_bound_shows_it(tmp_path: Path) -> None:
    """片側性: 0 に近い下限は必ず通る。データが縛った上限(下限 ≦ 利用/合計)
    と比べれば『実質何も確かめていない』と読める。"""
    record = _record(tmp_path)
    _with_trend_weights(
        record,
        weights=_weights("0.4", "0.3", "0.2", "0.1"),
        used=["operating_income"],
        recorded_partial="0.400000",
    )

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.01"),
        trend_min_coverage_source="test",
    )

    assert report.trend_coverage.violations == 0
    assert report.trend_coverage.upper_bound == ("0.4", "1.0")


def test_without_a_source_the_result_is_marked_as_reference_only(tmp_path: Path) -> None:
    record = _record(tmp_path)

    stated = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.3"),
        trend_min_coverage_source="x",
    )
    unstated = replay_raer_in_record(
        record, COEFFICIENTS, trend_min_coverage_required=Decimal("0.3")
    )

    assert stated.trend_coverage.source == "x"
    assert unstated.trend_coverage.supplied is True
    assert unstated.trend_coverage.source is None  # 出所なし = 参考扱い(合格の根拠に使わない)


def test_the_bound_is_the_smallest_ratio_over_the_applied_candidates(tmp_path: Path) -> None:
    record = _record(tmp_path)
    _with_trend_weights(
        record,
        weights=_weights("0.5", "0.2", "0.2", "0.1"),
        used=["operating_income"],
        recorded_partial="0.500000",
    )
    second = record["candidates"][1]["raer_in"]
    second["trend_weights"] = _weights("0.3", "0.3", "0.3", "0.1")
    second["trend_components_used"] = ["operating_income"]
    second["trend_partial_coverage"] = "0.300000"

    report = replay_raer_in_record(
        record,
        COEFFICIENTS,
        trend_min_coverage_required=Decimal("0.2"),
        trend_min_coverage_source="x",
    )

    assert report.trend_coverage.upper_bound == ("0.3", "1.0")


# =================================================================================================
# (5) 数値の regime
# =================================================================================================


def test_the_recorded_partial_coverage_is_checked_with_half_even_rounding(tmp_path: Path) -> None:
    """7 桁目がちょうど 5 で後ろが 0: 偶数丸めは 0.123456、四捨五入は 0.123457。
    記録が偶数丸めなら一致。"""
    weights = _weights("0.1234565", "0.4", "0.3", "0.1765435")  # 合計 1.0

    even = _record_with(tmp_path, weights, "0.123456")
    half_up = _record_with(tmp_path, weights, "0.123457")

    assert replay_raer_in_record(even, COEFFICIENTS).mismatches == ()
    assert (
        "candidates[0].raer_in.trend_partial_coverage"
        in replay_raer_in_record(half_up, COEFFICIENTS).mismatches
    )


def test_a_non_terminating_quotient_goes_through_the_single_rounding_place(tmp_path: Path) -> None:
    weights = _weights(
        "1", "1", "0.5", "0.5"
    )  # 合計 3。利用 1 → 1/3 = 0.333333…(有限小数にならない)

    right = _record_with(tmp_path, weights, "0.333333")
    wrong = _record_with(tmp_path, weights, "0.333334")

    assert replay_raer_in_record(right, COEFFICIENTS).mismatches == ()
    assert replay_raer_in_record(wrong, COEFFICIENTS).mismatches != ()


def _record_with(tmp_path: Path, weights: dict[str, str], recorded_partial: str) -> dict[str, Any]:
    record = _record(tmp_path)
    _with_trend_weights(
        record, weights=weights, used=["operating_income"], recorded_partial=recorded_partial
    )
    return record


def test_the_default_decimal_context_does_not_change_the_result(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["negative-trend"])
    before = replay_raer_in_record(record, COEFFICIENTS)

    with decimal.localcontext() as context:
        context.prec = 3  # 既定の context(丸めあり)が使われていれば、桁が足りず結果が変わる
        context.rounding = decimal.ROUND_DOWN
        during = replay_raer_in_record(record, COEFFICIENTS)

    assert before == during
    assert before.all_replayed_matched


def test_inexact_is_not_swallowed_and_is_counted_apart(tmp_path: Path) -> None:
    """丸めが起きる式(満点が 7 → 点 / 満点が有限小数にならない)は、握らず NUMERIC_REGIME_VIOLATION
    として数える。"""
    record = _record(tmp_path)
    odd = SimpleNamespace(
        coefficient_set_id="oddodd0000000000-c1",
        fv_confidence_factor=COEFFICIENTS.fv_confidence_factor,
        quality_deduction_max=COEFFICIENTS.quality_deduction_max,
        quality_max_score=Decimal(7),
        risk_deductions=COEFFICIENTS.risk_deductions,
        risk_deduction_cap=COEFFICIENTS.risk_deduction_cap,
        trend_deduction_scale=COEFFICIENTS.trend_deduction_scale,
        min_applied_axes=COEFFICIENTS.min_applied_axes,
        required_axes=COEFFICIENTS.required_axes,
    )
    record["model"]["coefficient_set_id"] = odd.coefficient_set_id

    report = replay_raer_in_record(record, odd)  # type: ignore[arg-type]

    assert report.record_gate is None
    assert report.regime_violations == 2  # 2 候補とも
    assert report.matched == 0
    assert report.not_replayable == ()  # NOT_REPLAYABLE に混ぜない
    assert report.all_replayed_matched is False


def test_the_regime_constants_equal_the_production_ones() -> None:
    assert replay.EXACT_PRECISION == production_raer.EXACT_PRECISION
    assert replay.TREND_SCORE_QUANTUM == production_composition.TREND_SCORE_QUANTUM
    assert replay.TREND_SCORE_ROUNDING == decimal.ROUND_HALF_EVEN
    context = production_raer.exact_decimal_context()
    assert context.prec == replay.EXACT_PRECISION
    assert context.traps[decimal.Inexact]


# =================================================================================================
# (6) 構造の定数が本番と一致(版が上がる・規則が変わると、ここが落ちて replay の更新を強制する)
# =================================================================================================


def test_the_record_kind_and_version_equal_the_record_module_ones() -> None:
    from jstock_advisor.domain.signals import allocation_shadow_record as record_module

    assert replay.RECORD_KIND == record_module.RECORD_KIND
    assert replay.RECORD_SCHEMA_VERSION == record_module.SCHEMA_VERSION


def test_the_replay_module_does_not_mention_the_record_module() -> None:
    """記録の書き手 module の importer の網(#603 の契約。本文の言及も数える)を広げない。"""
    text = (_SRC / "domain" / "signals" / f"{_MODULE_NAME}.py").read_text(encoding="utf-8")

    assert "allocation_shadow_record" not in text


def test_the_structural_constants_equal_the_production_ones() -> None:
    assert replay.SUPPORTED_MODEL_VERSION == production_raer.COMBINED_MODEL_VERSION
    assert tuple(axis.value for axis in production_raer.AXIS_ORDER) == replay.AXIS_NAMES
    assert tuple(flag.value for flag in production_raer.RiskFlag) == replay.RISK_ORDER
    assert (
        replay.LOW_CONFIDENCE_MAX_APPLIED_AXES
        == production_composition.LOW_CONFIDENCE_MAX_APPLIED_AXES
    )
    assert (
        production_composition.PROVISIONAL_CONFIDENCE_CEILING.value
        == replay.PROVISIONAL_CONFIDENCE_CEILING
    )
    assert (
        tuple(sorted({production_raer.SpecFlag.HORIZON_MISMATCH_PROVISIONAL.value}))
        == replay.EXPECTED_SPEC_FLAGS
    )
    assert tuple(replay._CONFIDENCE_ORDER) == tuple(  # noqa: SLF001
        level.value
        for level in production_composition._CONFIDENCE_ORDER  # noqa: SLF001
    )
    assert "DATA_QUALITY" in replay.UNIMPLEMENTED_AXES


def test_the_duplicate_rules_and_the_unimplemented_axis_match_the_production_behavior() -> None:
    """重複除外の語(決算 3・7 営業日 / 景気業種・一時増益)が本番の語彙にあり、
    本番の式と同じ結果になる。"""
    flags = frozenset(
        {
            "earnings_within_3_business_days",
            "earnings_within_7_business_days",
            "cyclical_industry",
            "temporary_earnings_boost_risk",
        }
    )
    production_items, _, _ = production_composition._risk_items(  # noqa: SLF001
        frozenset(production_raer.RiskFlag(f) for f in flags), COEFFICIENTS
    )
    production_counted = {
        item.flag.value: item.deduction_pct
        for item in production_items
        if item.status is production_raer.RiskContributionStatus.COUNTED
    }

    assert replay._risk_counted(flags, COEFFICIENTS) == production_counted  # noqa: SLF001


# =================================================================================================
# (7) 到達・不出力
# =================================================================================================


def test_no_module_imports_the_replay_module() -> None:
    importers = []
    for path in _SRC.rglob("*.py"):
        if path.stem == _MODULE_NAME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(_MODULE_NAME in name for name in names):
                importers.append(str(path.relative_to(_SRC)))
    assert importers == []


def test_the_module_name_contains_raer_so_the_606_name_net_covers_it() -> None:
    assert "raer" in _MODULE_NAME


def test_the_report_carries_no_value_stock_code_or_owner(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["risk-duplicates-and-cap"])
    _MUTATIONS["value"][0](_first(record))

    text = repr(
        replay_raer_in_record(record, COEFFICIENTS, trend_min_coverage_required=Decimal("0.3"))
    )

    for forbidden in ("0001", "0002", "9001", "owner", base._SENTINEL_OWNER):
        assert forbidden not in text
    assert str(Decimal(_first(record)["raer_out"]["value"])) not in text  # 値を持たない


def test_the_replay_does_not_mutate_the_record(tmp_path: Path) -> None:
    record = _record(tmp_path)
    snapshot = copy.deepcopy(record)

    replay_raer_in_record(record, COEFFICIENTS, trend_min_coverage_required=Decimal("0.3"))

    assert record == snapshot


# =================================================================================================
# 変異の生存を潰す(同じ条件でしか効かない規則を、その条件で固定する)
# =================================================================================================


def test_a_negative_upside_gives_no_fair_value_discount(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["negative-upside"])
    row = _first(record)

    assert Decimal(row["raer_in"]["upside_pct"]) < 0
    assert Decimal(row["raer_out"]["extra"]["contributions"]["FAIR_VALUE_CONFIDENCE"]) == 0
    assert replay_raer_in_record(record, COEFFICIENTS).all_replayed_matched


def test_a_quality_coverage_equal_to_the_minimum_applies_the_axis(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["quality-coverage-equal-to-minimum"])
    row = _first(record)

    assert row["raer_in"]["quality_coverage_ratio"] == row["raer_in"][
        "quality_min_coverage"
    ] or Decimal(row["raer_in"]["quality_coverage_ratio"]) == Decimal(
        row["raer_in"]["quality_min_coverage"]
    )
    assert "COMPANY_QUALITY" in row["raer_out"]["applied_axes"]


def _namespace_like(coefficients: Any, **changes: Any) -> SimpleNamespace:
    values = {
        "coefficient_set_id": coefficients.coefficient_set_id,
        "fv_confidence_factor": coefficients.fv_confidence_factor,
        "quality_deduction_max": coefficients.quality_deduction_max,
        "quality_max_score": coefficients.quality_max_score,
        "risk_deductions": coefficients.risk_deductions,
        "risk_deduction_cap": coefficients.risk_deduction_cap,
        "trend_deduction_scale": coefficients.trend_deduction_scale,
        "min_applied_axes": coefficients.min_applied_axes,
        "required_axes": coefficients.required_axes,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_equal_deductions_keep_the_earlier_word_of_the_duplicate_pair() -> None:
    """同値のときは RiskFlag の定義順で先(景気業種)を数える(同値は shipped
    の係数では起きないので別に固定)。"""
    equal = {flag: Decimal("2.0") for flag in COEFFICIENTS.risk_deductions}
    coefficients = _namespace_like(COEFFICIENTS, risk_deductions=equal)

    counted = replay._risk_counted(  # noqa: SLF001
        frozenset({"cyclical_industry", "temporary_earnings_boost_risk"}),
        coefficients,  # type: ignore[arg-type]
    )

    assert set(counted) == {"cyclical_industry"}


def test_a_value_that_breaks_the_required_axes_is_a_mismatch(tmp_path: Path) -> None:
    """必須軸を満たさない値のある記録 = 不一致(他の項目が一致していても、この規則で検出する)。"""
    from jstock_advisor.domain.valuation.raer import AxisCode

    record = _record(tmp_path, **_CASES["quality-coverage-below-minimum"])  # 企業品質の軸は不適用
    strict = _namespace_like(
        COEFFICIENTS,
        required_axes=frozenset({AxisCode.FAIR_VALUE_CONFIDENCE, AxisCode.COMPANY_QUALITY}),
    )

    report = replay_raer_in_record(record, strict)  # type: ignore[arg-type]

    assert report.mismatches and set(report.mismatches) == {
        "candidates[0].raer_out.applied_axes",
        "candidates[1].raer_out.applied_axes",
    }


def test_a_value_below_the_minimum_axis_count_is_a_mismatch(tmp_path: Path) -> None:
    record = _record(tmp_path)  # 適用軸 4
    strict = _namespace_like(COEFFICIENTS, min_applied_axes=5)

    report = replay_raer_in_record(record, strict)  # type: ignore[arg-type]

    assert "candidates[0].raer_out.applied_axes" in report.mismatches


def test_a_trend_score_off_the_six_digit_grid_is_a_mismatch(tmp_path: Path) -> None:
    record = _record(tmp_path, **_CASES["negative-trend"])
    _first(record)["raer_in"]["trend_score"] = "-27.1234567"

    assert (
        "candidates[0].raer_in.trend_score"
        in replay_raer_in_record(record, COEFFICIENTS).mismatches
    )

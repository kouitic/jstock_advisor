"""Fair Value Revision の規則(純粋関数)の契約テスト(Issue #601 PR-2 案 A)。

## この PR の範囲(USER 承認: #122 issuecomment-6110016281)
  履歴の列を入力にする純粋関数。UP / STABLE / DOWN / 未確認 / UNDETERMINED の判定、水準の持続による
  確認、UP と DOWN の対称な確認(M_up / M_down は別パラメータ)、未確認は期待リターンに算入しない、
  履歴不足・手法変更・鮮度不足の理由コード。
  ★ 未決のパラメータ(W・K・θ・M_up・M_down・鮮度基準)は推測で決めない: 未決の間は常に UNDETERMINED
    (REVISION_PARAMETERS_NOT_DECIDED)。出荷する既定値は置かない。
  ★ 履歴の取得・ExpectedReturnResult.revision への接続・配線・保存は含めない(dormant)。

## 固定するもの
  (0) 先行(characterization): 本 PR が接続しない既存の契約を固定する
      (ExpectedReturnResult.revision は REVISION_NOT_IMPLEMENTED のまま / ReasonCode の集合)
  (1) 判定例 E1〜E15 の全件
  (2) 境界: ±θ ちょうど(≥ θ は方向あり)・Decimal の厳密さ
  (3) K・窓 W・鮮度基準の境界
  (4) M_up / M_down は別のパラメータ・確認は『最新の M 点がすべて θ 以上、同じ側』
  (5) 同日重複は拒否(fail-closed)・入力順・評価日より後の点
  (6) 手法の集合の変更をまたいで比べない
  (7) 未決のパラメータ: 常に UNDETERMINED・既定値を置かない
  (8) UNDETERMINED の理由(欠測を 0 や STABLE で埋めない)
  (9) 性質: 符号の対称性・単調性・決定性
  (10) 期待リターンへの算入は確認済みだけ
  (11) 型の不変条件
  (12) 構造: 標準ライブラリだけ・時計なし・dormant

時間意味論: 評価日(date)の差の日数計算のみ。時計・営業日・timezone を読まない
(TIME_SEMANTICS_IMPACT は PR 本文で確定)。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Sequence
from dataclasses import MISSING, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation import fair_value_revision as fair_value_revision_module
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    FairValueInput,
    IncomeInput,
    ReasonCode,
    compute_expected_return,
)
from jstock_advisor.domain.valuation.fair_value_revision import (
    MODEL_VERSION,
    UNDETERMINED_REASONS,
    FairValueRevisionResult,
    RevisionDirection,
    RevisionParameters,
    RevisionPoint,
    RevisionReason,
    evaluate_fair_value_revision,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

# ---------------------------------------------------------------------------
# (0) 先行(characterization): 本 PR が接続しない既存の契約
# ---------------------------------------------------------------------------


def test_expected_return_revision_is_still_not_implemented() -> None:
    """PR-2 は ExpectedReturnResult.revision に接続しない: revision は常に未実装の理由のまま。"""
    result = compute_expected_return(
        evaluation_date=date(2026, 10, 9),
        current_price=Decimal("100"),
        price_freshness=PriceFreshnessVerdict.NORMAL,
        fair_value=FairValueInput(neutral=Decimal("120"), usable_for_trading_judgment=True),
        income=IncomeInput(
            dividend_yield_pct=1.0,
            benefit_yield_pct=0.0,
            benefit_state=BenefitProgramState.NO_PROGRAM,
        ),
    )
    assert result.revision == ComponentResult(reasons=(ReasonCode.REVISION_NOT_IMPLEMENTED,))


def test_expected_return_reason_code_set_is_unchanged() -> None:
    """ReasonCode に Revision 用の理由を足さない(接続は別 PR。PR-1b の adapter を壊さない)。"""
    assert {code.value for code in ReasonCode} == {
        "CURRENT_PRICE_UNAVAILABLE",
        "CURRENT_PRICE_STALE",
        "FAIR_VALUE_UNAVAILABLE",
        "FAIR_VALUE_NOT_USABLE",
        "DIVIDEND_UNKNOWN",
        "BENEFIT_UNVALUABLE",
        "REVISION_NOT_IMPLEMENTED",
        "COMPOSITION_RULE_NOT_DECIDED",
        "HORIZON_NOT_ESTIMATED",
    }


# ---------------------------------------------------------------------------
# テスト用の数値と履歴の組み立て
#   ★ 下の数値(W=30・K=3・θ=5%・M=2・鮮度=3)は、規則の動きを確かめるための例示であり、
#     決定値ではない。実際の数値は Shadow の観測の後に USER が決める(D-601-9)。
# ---------------------------------------------------------------------------

_START = date(2026, 9, 1)
_METHODS = frozenset({"DCF", "PER"})
_OTHER = frozenset({"DCF"})


def _params(**overrides: object) -> RevisionParameters:
    values: dict[str, object] = {
        "window_days": 30,
        "min_points": 3,
        "theta_pct": Decimal(5),
        "confirm_up": 2,
        "confirm_down": 2,
        "max_staleness_days": 3,
    }
    values.update(overrides)
    return RevisionParameters(**values)  # type: ignore[arg-type]


def _point(
    day: int,
    value: str | int | None,
    *,
    usable: bool = True,
    methods: frozenset[str] = _METHODS,
) -> RevisionPoint:
    return RevisionPoint(
        evaluation_date=_START + timedelta(days=day),
        fair_value=None if value is None else Decimal(value),
        usable_for_trading_judgment=usable,
        method_set=methods,
    )


def _series(values: Sequence[int | str]) -> list[RevisionPoint]:
    """日々 1 点の履歴(0 日目から)。"""
    return [_point(day, value) for day, value in enumerate(values)]


def _at(day: int) -> date:
    return _START + timedelta(days=day)


def _eval_date(history: Sequence[RevisionPoint]) -> date:
    return max(point.evaluation_date for point in history)


def _evaluate(
    history: Sequence[RevisionPoint], day: int, parameters: RevisionParameters | None = None
) -> FairValueRevisionResult:
    return evaluate_fair_value_revision(
        history,
        evaluation_date=_at(day),
        parameters=_params() if parameters is None else parameters,
    )


def _run(
    values: Sequence[int | str], parameters: RevisionParameters | None = None
) -> FairValueRevisionResult:
    """日々 1 点の履歴を、最後の点の日に評価する。"""
    history = _series(values)
    return evaluate_fair_value_revision(
        history,
        evaluation_date=_eval_date(history),
        parameters=_params() if parameters is None else parameters,
    )


D = RevisionDirection
R = RevisionReason

# ---------------------------------------------------------------------------
# (1) 判定例 E1〜E15(構造レビュー …6103040578・補足 …6103199351。θ=5%・M=2・K=3 は例示)
# ---------------------------------------------------------------------------

_EXAMPLES: list[tuple[str, list[int], RevisionDirection, float, bool]] = [
    ("E1", [100, 100, 100, 100, 80], D.DOWN_UNCONFIRMED, -20.0, False),
    ("E2", [100, 100, 100, 100, 80, 80], D.DOWN, -20.0, False),
    ("E3", [100, 100, 100, 100, 80, 100], D.STABLE, 0.0, False),
    ("E4", [100, 100, 100, 100, 120], D.UP_UNCONFIRMED, 20.0, False),
    ("E5", [100, 100, 100, 100, 120, 120], D.UP, 20.0, False),
    ("E6", [100, 100, 100, 100, 120, 100], D.STABLE, 0.0, False),
    ("E7", [100, 98, 96, 94, 92], D.DOWN, -8.0, False),
    ("E8", [100, 90, 100, 90, 100, 90], D.DOWN_UNCONFIRMED, -10.0, False),
    ("E9", [100, 100, 100, 96], D.STABLE, -4.0, False),
    ("E10", [100, 100, 100, 95, 95], D.DOWN, -5.0, False),
    ("E11", [100, 100, 90, 60], D.DOWN, -40.0, False),
    ("E12", [100, 100, 100, 120, 80], D.DOWN_UNCONFIRMED, -20.0, True),
    ("E13", [100, 100, 100, 80, 120], D.UP_UNCONFIRMED, 20.0, True),
    ("E14", [100, 100, 100, 120, 98], D.STABLE, -2.0, False),
    ("E15", [100, 100, 100, 80, 120, 120], D.UP, 20.0, False),
]


@pytest.mark.parametrize(
    ("name", "values", "direction", "pct", "disagree"),
    _EXAMPLES,
    ids=[example[0] for example in _EXAMPLES],
)
def test_examples_e1_to_e15(
    name: str, values: list[int], direction: RevisionDirection, pct: float, disagree: bool
) -> None:
    result = _run(values)
    assert result.direction is direction, name
    assert result.revision_pct == pytest.approx(pct), name
    assert result.latest_points_disagree is disagree, name


def test_the_examples_cover_every_judged_direction() -> None:
    assert {example[2] for example in _EXAMPLES} == {
        D.UP,
        D.STABLE,
        D.DOWN,
        D.UP_UNCONFIRMED,
        D.DOWN_UNCONFIRMED,
    }


def test_confirmed_results_have_no_reason_and_unconfirmed_results_say_why() -> None:
    for _, values, direction, _, _ in _EXAMPLES:
        result = _run(values)
        if direction in (D.UP_UNCONFIRMED, D.DOWN_UNCONFIRMED):
            assert result.reason is R.REVISION_UNCONFIRMED
        else:
            assert result.reason is None


def test_result_records_the_points_it_used() -> None:
    result = _run([100, 100, 100, 100, 80, 80])
    assert result.n_points == 6
    assert result.reference_date == _at(0)
    assert result.latest_date == _at(5)
    assert result.evaluation_date == _at(5)
    assert result.model_version == MODEL_VERSION


# ---------------------------------------------------------------------------
# (2) 境界: θ ちょうど(≥ θ は方向あり)・Decimal の厳密さ
# ---------------------------------------------------------------------------


def test_exactly_plus_theta_is_up_and_exactly_minus_theta_is_down() -> None:
    assert _run([100, 100, 100, 105, 105]).direction is D.UP
    assert _run([100, 100, 100, 95, 95]).direction is D.DOWN


@pytest.mark.parametrize(
    "values",
    [
        [100, 100, 100, "104.99", "104.99"],
        [100, 100, 100, "95.01", "95.01"],
        [100, 100, 100, 100, 100],
    ],
)
def test_just_inside_theta_is_stable(values: list[int | str]) -> None:
    assert _run(values).direction is D.STABLE


def test_theta_boundary_is_exact_where_float_arithmetic_would_miss_it() -> None:
    """Decimal の厳密な比較: 参照 0.07 の +5% = 0.0735 ちょうど。割り算の浮動小数点では外れうる。"""
    assert _run(["0.07", "0.07", "0.07", "0.0735", "0.0735"]).direction is D.UP
    assert _run(["0.07", "0.07", "0.07", "0.07", "0.0665"]).direction is D.DOWN_UNCONFIRMED
    assert _run(["0.07", "0.07", "0.07", "0.0665", "0.0665"]).direction is D.DOWN


def test_theta_is_a_parameter_not_a_constant() -> None:
    values = [100, 100, 100, 110, 110]
    assert _run(values, _params(theta_pct=Decimal(5))).direction is D.UP
    assert _run(values, _params(theta_pct=Decimal(10))).direction is D.UP
    assert _run(values, _params(theta_pct=Decimal("10.01"))).direction is D.STABLE


# ---------------------------------------------------------------------------
# (3) K(最低点数)・窓 W・鮮度基準の境界
# ---------------------------------------------------------------------------


def test_exactly_k_points_is_enough_and_one_fewer_is_not() -> None:
    assert _run([100, 100, 96]).direction is D.STABLE  # K = 3
    result = _run([100, 100])
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.INSUFFICIENT_HISTORY
    assert result.n_points == 2


def test_the_window_includes_the_start_day_and_excludes_the_day_before() -> None:
    params = _params(window_days=10)  # 評価日 = 15 日目。窓 = 5 日目以降
    inside = [_point(5, 100), _point(12, 100), _point(14, 100), _point(15, 80)]
    result = _evaluate(inside, 15, params)
    assert result.reference_date == _at(5)
    assert result.n_points == 4
    result = _evaluate([_point(4, 50), *inside], 15, params)
    assert result.reference_date == _at(5)  # 4 日目(窓の外)は使わない
    assert result.n_points == 4


def test_points_before_the_window_do_not_count_toward_k() -> None:
    params = _params(window_days=10)
    history = [_point(0, 100), _point(1, 100), _point(14, 100), _point(15, 80)]
    result = _evaluate(history, 15, params)
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.INSUFFICIENT_HISTORY
    assert result.n_points == 2


def test_the_reference_is_the_oldest_eligible_point_in_the_window() -> None:
    params = _params(window_days=10)
    history = [
        _point(5, 100, usable=False),
        _point(6, 200),
        _point(8, 200),
        _point(14, 200),
        _point(15, 150),
        _point(16, 150),
    ]
    result = _evaluate(history, 16, params)
    assert result.reference_date == _at(6)  # 5 日目は不適格
    assert result.revision_pct == pytest.approx(-25.0)
    assert result.direction is D.DOWN


def test_staleness_boundary_is_inclusive_of_the_limit() -> None:
    history = [_point(0, 100), _point(1, 100), _point(2, 100), _point(3, 80), _point(4, 80)]
    fresh = _evaluate(history, 7)  # 最新の点は 3 日前 = 基準ちょうど
    assert fresh.direction is D.DOWN
    stale = _evaluate(history, 8)  # 4 日前
    assert stale.direction is D.UNDETERMINED
    assert stale.reason is R.STALE_HISTORY
    assert stale.latest_date == _at(4)


def test_staleness_is_checked_before_the_point_count() -> None:
    result = _evaluate([_point(0, 100), _point(1, 100)], 20)
    assert result.reason is R.STALE_HISTORY


def test_staleness_uses_the_latest_eligible_point_not_the_latest_point() -> None:
    history = [_point(0, 100), _point(1, 100), _point(2, 100), _point(9, 100, usable=False)]
    assert _evaluate(history, 9).reason is R.STALE_HISTORY


# ---------------------------------------------------------------------------
# (4) M(確認点数): 上下で別のパラメータ・境界
# ---------------------------------------------------------------------------


def test_confirmation_needs_exactly_m_points_on_the_same_side() -> None:
    params = _params(confirm_up=3, confirm_down=3, min_points=4)
    assert _run([100, 100, 100, 100, 120, 120], params).direction is D.UP_UNCONFIRMED
    assert _run([100, 100, 100, 100, 120, 120, 120], params).direction is D.UP
    assert _run([100, 100, 100, 100, 80, 80], params).direction is D.DOWN_UNCONFIRMED
    assert _run([100, 100, 100, 100, 80, 80, 80], params).direction is D.DOWN


def test_m_up_and_m_down_are_independent_parameters() -> None:
    params = _params(confirm_up=1, confirm_down=3, min_points=4)  # UP は 1 点、DOWN は 3 点
    assert _run([100, 100, 100, 100, 120], params).direction is D.UP
    assert _run([100, 100, 100, 100, 80], params).direction is D.DOWN_UNCONFIRMED
    assert _run([100, 100, 100, 100, 80, 80], params).direction is D.DOWN_UNCONFIRMED
    assert _run([100, 100, 100, 100, 80, 80, 80], params).direction is D.DOWN
    mirrored = _params(confirm_up=3, confirm_down=1, min_points=4)
    assert _run([100, 100, 100, 100, 80], mirrored).direction is D.DOWN
    assert _run([100, 100, 100, 100, 120], mirrored).direction is D.UP_UNCONFIRMED


def test_with_m_equal_one_a_single_point_confirms() -> None:
    params = _params(confirm_up=1, confirm_down=1, min_points=2)
    assert _run([100, 100, 80], params).direction is D.DOWN
    assert _run([100, 100, 120], params).direction is D.UP


def test_confirmation_points_must_all_clear_theta_not_merely_share_a_side() -> None:
    # 最新の 2 点が同じ側(下)でも、片方が θ 未満なら確認しない
    assert _run([100, 100, 100, 98, 80]).direction is D.DOWN_UNCONFIRMED
    assert _run([100, 100, 100, "95.01", 80]).direction is D.DOWN_UNCONFIRMED
    assert _run([100, 100, 100, 95, 80]).direction is D.DOWN


def test_disagree_is_set_only_when_both_sides_appear_in_the_confirmation_points() -> None:
    assert _run([100, 100, 100, 120, 80]).latest_points_disagree is True
    assert _run([100, 100, 100, 100, 80]).latest_points_disagree is False  # 片側 + 帯の中
    assert _run([100, 100, 100, 98, 80]).latest_points_disagree is False
    params = _params(confirm_up=3, confirm_down=3, min_points=4)
    assert _run([100, 100, 100, 100, 120, 100, 80], params).latest_points_disagree is True
    assert _run([100, 100, 100, 100, 100, 100, 80], params).latest_points_disagree is False
    assert _run([100, 100, 100, 100, 120, 80, 80], params).latest_points_disagree is True


def test_earlier_swings_do_not_block_a_confirmation_that_the_latest_points_satisfy() -> None:
    assert _run([100, 100, 100, 80, 120, 120]).direction is D.UP  # E15


# ---------------------------------------------------------------------------
# (5) 同日重複・順序・未来の点
# ---------------------------------------------------------------------------


def test_two_points_on_the_same_day_are_rejected_not_resolved_by_input_order() -> None:
    """同じ評価日の複数点は拒否する(fail-closed)。入力順で勝者を決めると非決定になる。"""
    history = [_point(0, 100), _point(1, 100), _point(2, 100), _point(3, 70), _point(3, 100)]
    with pytest.raises(ValueError):
        _evaluate(history, 3)
    with pytest.raises(ValueError):
        _evaluate([*history[:3], history[4], history[3]], 3)  # 順序を変えても同じ


def test_duplicates_are_rejected_even_when_the_points_are_identical_or_unusable() -> None:
    with pytest.raises(ValueError):
        _evaluate([_point(0, 100), _point(0, 100), _point(1, 100), _point(2, 100)], 2)
    with pytest.raises(ValueError):
        _evaluate([_point(0, 100), _point(1, 100), _point(2, 100), _point(2, 5, usable=False)], 2)
    with pytest.raises(ValueError):
        _evaluate([_point(0, None), _point(0, None)], 0)


def test_duplicates_after_the_evaluation_date_are_not_checked() -> None:
    history = [*_series([100, 100, 100, 100]), _point(10, 10), _point(10, 20)]
    assert _evaluate(history, 3).direction is D.STABLE  # 未来の点は知り得ないので見ない


def test_undecided_parameters_stay_undetermined_even_with_duplicates() -> None:
    history = [_point(0, 100), _point(0, 90)]
    result = evaluate_fair_value_revision(history, evaluation_date=_at(0), parameters=None)
    assert result.reason is R.REVISION_PARAMETERS_NOT_DECIDED


def test_the_duplicate_rule_is_documented_for_the_caller() -> None:
    doc = fair_value_revision_module.__doc__ or ""
    assert "評価日ごとに 1 件" in doc
    assert "評価日ごとに 1 件" in (evaluate_fair_value_revision.__doc__ or "")


def test_the_input_order_does_not_matter_for_distinct_days() -> None:
    history = _series([100, 100, 100, 100, 80, 80])
    expected = _evaluate(history, 5)
    for order in (list(reversed(history)), history[3:] + history[:3]):
        assert _evaluate(order, 5) == expected


def test_points_after_the_evaluation_date_are_ignored() -> None:
    history = [*_series([100, 100, 100, 100]), _point(10, 10), _point(11, 10)]
    result = _evaluate(history, 3)
    assert result.direction is D.STABLE
    assert result.latest_date == _at(3)
    assert _evaluate([_point(10, 10)], 3).reason is R.NO_HISTORY


def test_evaluate_does_not_mutate_its_input() -> None:
    history = _series([100, 100, 100, 100, 80, 80])
    snapshot = list(history)
    _evaluate(history, 5)
    assert history == snapshot


# ---------------------------------------------------------------------------
# (6) 手法の集合の変更(P3): またいで比べない
# ---------------------------------------------------------------------------


def test_a_changed_method_set_between_reference_and_latest_is_undetermined() -> None:
    history = [_point(0, 100), _point(1, 100), _point(2, 100), _point(3, 80, methods=_OTHER)]
    result = _evaluate(history, 3)
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.METHOD_SET_CHANGED
    assert result.revision_pct is None


def test_a_method_change_is_detected_even_when_the_values_look_stable() -> None:
    history = [_point(0, 100), _point(1, 100), _point(2, 100, methods=_OTHER)]
    assert (
        _evaluate(history, 2).reason is R.METHOD_SET_CHANGED
    )  # 同じ値でも別の手法の点とは比べない


def test_a_method_change_inside_the_confirmation_points_is_undetermined() -> None:
    history = [
        _point(0, 100),
        _point(1, 100),
        _point(2, 100),
        _point(3, 80, methods=_OTHER),
        _point(4, 80),
    ]
    assert _evaluate(history, 4).reason is R.METHOD_SET_CHANGED


def test_a_method_change_outside_the_compared_points_does_not_block() -> None:
    history = [
        _point(0, 100),
        _point(1, 100, methods=_OTHER),  # 参照でも確認の点でもない
        _point(2, 100),
        _point(3, 80),
        _point(4, 80),
    ]
    assert _evaluate(history, 4).direction is D.DOWN


def test_method_sets_are_compared_as_sets_not_as_ordered_lists() -> None:
    history = [
        _point(0, 100, methods=frozenset({"A", "B"})),
        _point(1, 100, methods=frozenset({"B", "A"})),
        _point(2, 100, methods=frozenset({"A", "B"})),
    ]
    assert _evaluate(history, 2).direction is D.STABLE


# ---------------------------------------------------------------------------
# (7) 未決のパラメータ: 常に UNDETERMINED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [[], [100], [100, 100, 100, 100, 80, 80], [100, 98, 96, 94, 92]],
)
def test_undecided_parameters_always_give_undetermined(values: list[int]) -> None:
    result = evaluate_fair_value_revision(_series(values), evaluation_date=_at(10), parameters=None)
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.REVISION_PARAMETERS_NOT_DECIDED
    assert result.revision_pct is None
    assert result.counts_toward_expected_return is False
    assert result.confirmed_revision_pct is None


def test_undecided_parameters_are_reported_before_any_data_problem() -> None:
    result = evaluate_fair_value_revision([], evaluation_date=_START, parameters=None)
    assert result.reason is R.REVISION_PARAMETERS_NOT_DECIDED  # NO_HISTORY ではない


def test_no_default_parameters_are_shipped() -> None:
    parameter = inspect.signature(evaluate_fair_value_revision).parameters["parameters"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert all(
        field.default is MISSING and field.default_factory is MISSING
        for field in fields(RevisionParameters)
    )
    module_values = vars(fair_value_revision_module).values()
    assert not [value for value in module_values if isinstance(value, RevisionParameters)]


def test_no_config_or_infra_file_mentions_the_revision_parameters() -> None:
    root = Path(__file__).resolve().parents[2]
    for folder in ("config", "infra"):
        for path in (root / folder).rglob("*"):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".json", ".toml"}:
                text = path.read_text(encoding="utf-8")
                assert "fair_value_revision" not in text, path
                assert "theta_pct" not in text, path


def test_model_version_says_the_parameters_are_undecided() -> None:
    assert MODEL_VERSION == "fv-revision-v1-params-undecided"


# ---------------------------------------------------------------------------
# (8) UNDETERMINED の理由
# ---------------------------------------------------------------------------


def test_no_history() -> None:
    result = evaluate_fair_value_revision([], evaluation_date=_START, parameters=_params())
    assert result.reason is R.NO_HISTORY
    assert result.n_points == 0


@pytest.mark.parametrize(
    ("usable", "value"),
    [
        (False, 100),
        (True, None),
        (True, 0),
        (True, -5),
        (True, "NaN"),
        (True, "Infinity"),
    ],
    ids=["unusable", "none", "zero", "negative", "nan", "infinity"],
)
def test_ineligible_points_are_never_used(usable: bool, value: str | int | None) -> None:
    history = [_point(day, value, usable=usable) for day in range(4)]
    result = _evaluate(history, 3)
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.ONLY_UNUSABLE_POINTS


def test_ineligible_points_do_not_count_toward_k() -> None:
    history = [
        _point(0, 100),
        _point(2, 0),
        _point(3, 100, usable=False),
        _point(4, 80),
    ]
    result = _evaluate(history, 4)
    assert result.direction is D.UNDETERMINED
    assert result.reason is R.INSUFFICIENT_HISTORY
    assert result.n_points == 2


def test_undetermined_never_carries_a_value_or_defaults_to_stable() -> None:
    cases = [
        evaluate_fair_value_revision([], evaluation_date=_START, parameters=_params()),
        evaluate_fair_value_revision([], evaluation_date=_START, parameters=None),
        _run([100, 100]),
        _evaluate([_point(0, 100)], 30),
    ]
    for result in cases:
        assert result.direction is D.UNDETERMINED
        assert result.revision_pct is None
        assert result.counts_toward_expected_return is False


# ---------------------------------------------------------------------------
# (9) 性質: 符号の対称性・単調性・決定性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [5, 6, 10, 25, 49])
def test_up_and_down_are_mirror_images_for_the_same_distance_from_the_reference(
    offset: int,
) -> None:
    up = _run([100, 100, 100, 100 + offset, 100 + offset])
    down = _run([100, 100, 100, 100 - offset, 100 - offset])
    assert (up.direction, down.direction) == (D.UP, D.DOWN)
    assert up.revision_pct is not None and down.revision_pct is not None
    assert up.revision_pct == pytest.approx(-down.revision_pct)
    up_u = _run([100, 100, 100, 100, 100 + offset])
    down_u = _run([100, 100, 100, 100, 100 - offset])
    assert (up_u.direction, down_u.direction) == (D.UP_UNCONFIRMED, D.DOWN_UNCONFIRMED)


@pytest.mark.parametrize(("a", "b"), [(100, 80), (100, 150), (37, 41), (200, 199)])
def test_swapping_reference_and_latest_flips_the_sign_of_revision_pct(a: int, b: int) -> None:
    forward = _run([a, a, b]).revision_pct
    backward = _run([b, b, a]).revision_pct
    assert forward is not None and backward is not None
    assert (forward > 0) == (backward < 0)
    assert forward != 0


def test_revision_pct_is_monotone_in_the_latest_fair_value() -> None:
    latest_values = [50, 80, 95, 99, 100, 101, 105, 120, 200]
    pcts = [_run([100, 100, 100, value]).revision_pct for value in latest_values]
    floats = [pct for pct in pcts if pct is not None]
    assert len(floats) == len(latest_values)
    assert floats == sorted(floats)
    assert len(set(floats)) == len(floats)


def test_the_result_is_deterministic() -> None:
    history = _series([100, 100, 100, 100, 80, 80])
    first = _evaluate(history, 5)
    for _ in range(3):
        assert _evaluate(history, 5) == first


# ---------------------------------------------------------------------------
# (10) 期待リターンへの算入: 確認済みだけ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "counts"),
    [
        (D.UP, True),
        (D.STABLE, True),
        (D.DOWN, True),
        (D.UP_UNCONFIRMED, False),
        (D.DOWN_UNCONFIRMED, False),
        (D.UNDETERMINED, False),
    ],
)
def test_only_confirmed_directions_count_toward_expected_return(
    direction: RevisionDirection, counts: bool
) -> None:
    by_direction = {example[2]: example[1] for example in _EXAMPLES}
    result = _run([100, 100]) if direction is D.UNDETERMINED else _run(by_direction[direction])
    assert result.direction is direction
    assert result.counts_toward_expected_return is counts
    if counts:
        assert result.confirmed_revision_pct == result.revision_pct
    else:
        assert result.confirmed_revision_pct is None


def test_unconfirmed_results_keep_the_reference_value_but_never_expose_it_as_confirmed() -> None:
    result = _run([100, 100, 100, 100, 80])
    assert result.revision_pct == pytest.approx(-20.0)
    assert result.confirmed_revision_pct is None


# ---------------------------------------------------------------------------
# (11) 型の不変条件
# ---------------------------------------------------------------------------


def _result(**overrides: object) -> FairValueRevisionResult:
    values: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "evaluation_date": _START,
        "direction": D.UP,
        "revision_pct": 10.0,
        "reason": None,
    }
    values.update(overrides)
    return FairValueRevisionResult(**values)  # type: ignore[arg-type]


def test_result_invariants() -> None:
    _result()
    _result(direction=D.STABLE, revision_pct=0.0)
    _result(direction=D.DOWN_UNCONFIRMED, revision_pct=-9.0, reason=R.REVISION_UNCONFIRMED)
    _result(
        direction=D.UP_UNCONFIRMED,
        revision_pct=9.0,
        reason=R.REVISION_UNCONFIRMED,
        latest_points_disagree=True,
    )
    _result(direction=D.UNDETERMINED, revision_pct=None, reason=R.NO_HISTORY)
    bad_cases: list[dict[str, object]] = [
        {"reason": R.NO_HISTORY},  # 確認済みに理由
        {"revision_pct": None},  # 確認済みに値なし
        {"direction": D.UP_UNCONFIRMED, "reason": None},  # 未確認に理由なし
        {"direction": D.UP_UNCONFIRMED, "reason": R.NO_HISTORY},  # 未確認に別の理由
        {"direction": D.DOWN_UNCONFIRMED, "reason": R.REVISION_UNCONFIRMED, "revision_pct": None},
        {"direction": D.UNDETERMINED, "reason": None, "revision_pct": None},
        {"direction": D.UNDETERMINED, "reason": R.REVISION_UNCONFIRMED, "revision_pct": None},
        {"direction": D.UNDETERMINED, "reason": R.NO_HISTORY, "revision_pct": 0.0},  # 0 で埋めない
        {"latest_points_disagree": True},  # 確認済みに両側の印
        {"evaluation_date": datetime(2026, 9, 1, 12, 0)},
    ]
    for overrides in bad_cases:
        with pytest.raises(ValueError):
            _result(**overrides)


def test_reasons_and_directions_are_fixed() -> None:
    assert set(R) - UNDETERMINED_REASONS == {R.REVISION_UNCONFIRMED}
    assert {reason.value for reason in R} == {
        "REVISION_PARAMETERS_NOT_DECIDED",
        "NO_HISTORY",
        "ONLY_UNUSABLE_POINTS",
        "INSUFFICIENT_HISTORY",
        "STALE_HISTORY",
        "METHOD_SET_CHANGED",
        "REVISION_UNCONFIRMED",
    }
    assert {direction.value for direction in D} == {
        "UP",
        "STABLE",
        "DOWN",
        "UP_UNCONFIRMED",
        "DOWN_UNCONFIRMED",
        "UNDETERMINED",
    }


def test_parameter_invariants() -> None:
    _params()
    bad_cases: list[dict[str, object]] = [
        {"window_days": 0},
        {"window_days": True},
        {"min_points": 2},  # K ≤ M
        {"confirm_up": 3},  # K = 3 ≤ M_up
        {"confirm_down": 3},
        {"confirm_up": 0},
        {"confirm_down": 0},
        {"max_staleness_days": -1},
        {"max_staleness_days": 1.5},
        {"theta_pct": Decimal(0)},
        {"theta_pct": Decimal(-1)},
        {"theta_pct": Decimal(100)},
        {"theta_pct": Decimal("NaN")},
        {"theta_pct": Decimal("Infinity")},
        {"theta_pct": 5.0},  # float は受けない
    ]
    for overrides in bad_cases:
        with pytest.raises(ValueError):
            _params(**overrides)
    _params(max_staleness_days=0)
    _params(theta_pct=Decimal("0.01"))
    _params(theta_pct=Decimal("99.99"))


def test_k_just_above_m_is_accepted_and_can_always_confirm() -> None:
    params = _params(min_points=3, confirm_up=2, confirm_down=2)
    # K 点ちょうどでも、参照点を除いた M 点がそろえば確認できる
    assert _run([100, 80, 80], params).direction is D.DOWN


def test_point_invariants() -> None:
    _point(0, 100)
    _point(0, None)
    with pytest.raises(ValueError):
        RevisionPoint(
            evaluation_date=datetime(2026, 9, 1, 9, 0),
            fair_value=Decimal(1),
            usable_for_trading_judgment=True,
            method_set=_METHODS,
        )
    with pytest.raises(ValueError):
        RevisionPoint(
            evaluation_date=_START,
            fair_value=100.0,  # type: ignore[arg-type]
            usable_for_trading_judgment=True,
            method_set=_METHODS,
        )
    with pytest.raises(ValueError):
        RevisionPoint(
            evaluation_date=_START,
            fair_value=Decimal(1),
            usable_for_trading_judgment=True,
            method_set={"DCF"},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        evaluate_fair_value_revision(
            [], evaluation_date=datetime(2026, 9, 1, 9, 0), parameters=_params()
        )


# ---------------------------------------------------------------------------
# (12) 構造: 純粋・自己完結・dormant
# ---------------------------------------------------------------------------

_MODULE_PATH = Path(fair_value_revision_module.__file__ or "")
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_TREE = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
}


def _imported_modules() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_module_imports_only_the_standard_library_pieces_it_needs() -> None:
    assert _imported_modules() == _ALLOWED_IMPORTS


def test_the_module_never_reads_a_clock_or_does_io() -> None:
    forbidden_calls = {"now", "today", "utcnow", "time", "monotonic", "sleep", "open", "print"}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in forbidden_calls, name


def test_the_module_names_no_other_inputs() -> None:
    names = {node.id for node in ast.walk(_TREE) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(_TREE) if isinstance(node, ast.Attribute)
    }
    forbidden = {
        "available_cash",
        "acquisition_price",
        "purchase_price",
        "average_cost",
        "unrealized_profit_loss",
        "holding_id",
        "owner_id",
        "stock_code",
    }
    assert not (names & forbidden)


def test_the_threshold_comparison_does_not_divide() -> None:
    side = next(
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "_side"
    )
    nodes = list(ast.walk(side))
    assert not [n for n in nodes if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)]
    assert not [
        n for n in nodes if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "float"
    ]


def test_the_module_is_dormant_nothing_in_src_imports_it() -> None:
    importers = [
        path.name
        for path in _SRC.rglob("*.py")
        if path != _MODULE_PATH and "fair_value_revision" in path.read_text(encoding="utf-8")
    ]
    assert importers == []

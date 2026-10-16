"""転帰の計算・最小セル数の表・事前登録の検証の契約テスト(Issue #880 R1a)。

対象: `outcome_window`(a-4)・`replay_tables`(a-6)・`preregistration_check`(a-7)。
fixture は合成のみ(実在の銘柄・価格・値を使わない)。

  O1  転帰: 窓ごとの forward return・最大下落・反発・新高値更新。起点 2 種。足りない列は値を作らない
  O2  売却の効果: 早売り・回避した下落・売り遅れ(HOLD 100% との比較。税・手数料は引数)
  T1  最小セル数: 満たないとき割合・平均・中央値を出さず件数のみ
  G1  事前登録: 正規化・SHA-256・登録 ID・使った値の照合(自己申告に頼らない)
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction

import pytest

from jstock_advisor.domain.backtest.errors import ReplayInputError
from jstock_advisor.domain.backtest.outcome_window import (
    Bar,
    OriginRule,
    OutcomeSpec,
    OutcomeStatus,
    compute_outcomes,
    sale_effect,
)
from jstock_advisor.domain.backtest.preregistration_check import (
    RegistrationCheck,
    canonical_json,
    check_report,
    compare_used_values,
    content_hash,
    registered_values,
)
from jstock_advisor.domain.backtest.replay_tables import rate_table, summarize

_D = Decimal


def _bar(o: str, h: str, low: str, c: str) -> Bar:
    return Bar(_D(o), _D(h), _D(low), _D(c))


#: 合成の価格列(0 = 判定日)。判定日の終値 100・翌日の始値 102
_BARS = [
    _bar("100", "101", "99", "100"),  # 0 判定日
    _bar("102", "104", "98", "103"),  # 1
    _bar("103", "106", "95", "96"),  # 2 最安値 95
    _bar("96", "110", "96", "108"),  # 3 最高値 110(最安値の後)
    _bar("108", "109", "100", "105"),  # 4
]
_SPEC = OutcomeSpec(windows=(2, 4), origin=OriginRule.DECISION_DAY_CLOSE)


# =================================================================================================
# O1 転帰
# =================================================================================================


def test_the_outcome_of_each_window_is_computed_from_the_registered_origin() -> None:
    short, long = compute_outcomes(_BARS, 0, _SPEC, prior_high=_D("105"))

    assert short.status is OutcomeStatus.COMPLETE and short.window == 2
    assert short.forward_return == _D("96") / _D("100") - 1  # 窓 2 の終値 / 起点
    assert short.max_drawdown == _D("95") / _D("100") - 1  # 窓の中の最安値
    assert short.rebound == _D("106") / _D("95") - 1  # 最安値の日(2)の後の最高値 106
    assert short.new_high is True  # 106 > 判定前の高値 105
    assert long.forward_return == _D("105") / _D("100") - 1
    assert long.rebound == _D("110") / _D("95") - 1
    assert long.new_high is True


def test_the_next_day_open_origin_uses_the_first_executable_price() -> None:
    spec = OutcomeSpec(windows=(2,), origin=OriginRule.NEXT_DAY_OPEN)

    [outcome] = compute_outcomes(_BARS, 0, spec, prior_high=_D("200"))

    assert outcome.forward_return == _D("96") / _D("102") - 1  # 起点 = 翌営業日の始値 102
    assert outcome.new_high is False  # 106 <= 200


def test_a_window_the_series_cannot_cover_makes_no_value() -> None:
    spec = OutcomeSpec(windows=(2, 9), origin=OriginRule.DECISION_DAY_CLOSE)

    ok, short = compute_outcomes(_BARS, 0, spec, prior_high=_D("100"))

    assert ok.status is OutcomeStatus.COMPLETE
    assert short.status is OutcomeStatus.INSUFFICIENT_SERIES
    assert (short.forward_return, short.max_drawdown, short.rebound, short.new_high) == (
        None,
        None,
        None,
        None,
    )  # 値を補完しない


def test_the_rebound_counts_only_the_highs_after_the_lowest_low() -> None:
    bars = [
        _bar("100", "100", "100", "100"),
        _bar("100", "130", "99", "100"),  # 最安値より前の高値 130 は反発に数えない
        _bar("98", "99", "90", "92"),  # 最安値 90
        _bar("92", "100", "91", "99"),  # 最安値の後の最高値 100
    ]

    [outcome] = compute_outcomes(
        bars, 0, OutcomeSpec((3,), OriginRule.DECISION_DAY_CLOSE), prior_high=_D("500")
    )

    assert outcome.rebound == _D("100") / _D("90") - 1
    assert outcome.new_high is False


def test_a_window_that_ends_exactly_after_the_series_is_insufficient_not_an_error() -> None:
    spec = OutcomeSpec(
        windows=(len(_BARS),), origin=OriginRule.DECISION_DAY_CLOSE
    )  # 最後の価格の 1 つ先

    [outcome] = compute_outcomes(_BARS, 0, spec, prior_high=_D("100"))

    assert outcome.status is OutcomeStatus.INSUFFICIENT_SERIES


def test_a_new_high_needs_to_exceed_the_prior_high_not_equal_it() -> None:
    bars = [_bar("100", "100", "100", "100"), _bar("100", "110", "99", "105")]
    spec = OutcomeSpec((1,), OriginRule.DECISION_DAY_CLOSE)

    [equal] = compute_outcomes(bars, 0, spec, prior_high=_D("110"))
    [above] = compute_outcomes(bars, 0, spec, prior_high=_D("109"))

    assert equal.new_high is False and above.new_high is True


def test_the_missed_upside_uses_the_sold_share_and_the_remaining_loss_uses_the_rest() -> None:
    rising = sale_effect(_outcome("0.10"), sold_ratio=_D("0.25"), sale_cost_rate=_D("0"))
    falling = sale_effect(_outcome("-0.10"), sold_ratio=_D("0.25"), sale_cost_rate=_D("0"))

    assert rising is not None and falling is not None
    assert rising.missed_upside == _D("0.025")  # 売った 25% × 上昇 10%(残りの 75% ではない)
    assert falling.avoided_loss == _D("0.025")
    assert falling.further_loss_on_remaining == _D("0.075")


def test_the_next_day_open_origin_without_a_next_bar_makes_no_value() -> None:
    spec = OutcomeSpec(windows=(1,), origin=OriginRule.NEXT_DAY_OPEN)

    [outcome] = compute_outcomes(_BARS, len(_BARS) - 1, spec, prior_high=_D("100"))

    assert outcome.status is OutcomeStatus.INSUFFICIENT_SERIES


def test_the_drawdown_is_never_positive_and_the_rebound_never_negative() -> None:
    rising = [_bar("100", "100", "100", "100")] + [
        _bar(str(100 + i), str(100 + i), str(100 + i), str(100 + i)) for i in range(1, 5)
    ]

    [outcome] = compute_outcomes(
        rising, 0, OutcomeSpec((4,), OriginRule.DECISION_DAY_CLOSE), prior_high=_D("1")
    )

    assert outcome.max_drawdown == 0
    assert outcome.rebound is not None and outcome.rebound >= 0


@pytest.mark.parametrize(
    "bad",
    [
        {"open": _D("0"), "high": _D("1"), "low": _D("1"), "close": _D("1")},
        {"open": 1.0, "high": _D("1"), "low": _D("1"), "close": _D("1")},
        {"open": _D("1"), "high": _D("1"), "low": _D("2"), "close": _D("1")},  # low > high
        {"open": _D("5"), "high": _D("4"), "low": _D("3"), "close": _D("4")},  # open が範囲外
        {"open": _D("3"), "high": _D("4"), "low": _D("3"), "close": _D("9")},  # close が範囲外
        {"open": _D("NaN"), "high": _D("1"), "low": _D("1"), "close": _D("1")},
    ],
)
def test_an_inconsistent_bar_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ReplayInputError):
        Bar(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "windows",
    [(), (0,), (2, 2), (3, 2), (-1,), (True,), ("2",), [2]],
)
def test_the_windows_must_be_a_strictly_increasing_positive_tuple(windows: object) -> None:
    with pytest.raises(ReplayInputError):
        OutcomeSpec(windows=windows, origin=OriginRule.DECISION_DAY_CLOSE)  # type: ignore[arg-type]


def test_the_spec_has_no_default_window_or_origin() -> None:
    with pytest.raises(TypeError):
        OutcomeSpec()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        compute_outcomes(_BARS, 0, _SPEC)  # type: ignore[call-arg]  # prior_high は必須


@pytest.mark.parametrize("prior", [None, 100, _D("0"), _D("-1"), _D("Infinity")])
def test_the_prior_high_is_a_positive_finite_decimal(prior: object) -> None:
    with pytest.raises(ReplayInputError):
        compute_outcomes(_BARS, 0, _SPEC, prior_high=prior)  # type: ignore[arg-type]


def test_the_decision_index_must_be_inside_the_series() -> None:
    with pytest.raises(ReplayInputError):
        compute_outcomes(_BARS, len(_BARS), _SPEC, prior_high=_D("100"))
    with pytest.raises(ReplayInputError):
        compute_outcomes(_BARS, -1, _SPEC, prior_high=_D("100"))


# =================================================================================================
# O2 売却の効果
# =================================================================================================


def _outcome(forward_return: str):  # type: ignore[no-untyped-def]
    from jstock_advisor.domain.backtest.outcome_window import WindowOutcome

    return WindowOutcome(5, OutcomeStatus.COMPLETE, _D(forward_return), _D("0"), _D("0"), False)


def test_selling_before_a_rise_is_an_early_sale() -> None:
    effect = sale_effect(_outcome("0.10"), sold_ratio=_D("0.5"), sale_cost_rate=_D("0.01"))

    assert effect is not None
    assert effect.hold_value == _D("1.10")
    assert effect.after_sale_value == _D("0.5") * _D("0.99") + _D("0.5") * _D("1.10")
    assert effect.missed_upside == _D("0.05")  # 売った割合 × 上昇
    assert effect.avoided_loss == 0 and effect.further_loss_on_remaining == 0


def test_a_fall_is_split_into_the_avoided_loss_and_the_loss_that_remains() -> None:
    effect = sale_effect(_outcome("-0.20"), sold_ratio=_D("0.25"), sale_cost_rate=_D("0"))

    assert effect is not None
    assert effect.avoided_loss == _D("0.05")  # 売った割合 × 下落
    assert effect.further_loss_on_remaining == _D("0.15")  # 売れ残った割合 × 下落(売り遅れ)
    assert effect.missed_upside == 0
    assert effect.avoided_loss + effect.further_loss_on_remaining == _D("0.20")


def test_selling_everything_leaves_no_loss_on_the_remaining_and_costs_apply() -> None:
    effect = sale_effect(_outcome("-0.20"), sold_ratio=_D("1"), sale_cost_rate=_D("0.02"))

    assert effect is not None
    assert effect.further_loss_on_remaining == 0
    assert effect.after_sale_value == _D("0.98")


def test_an_incomplete_outcome_gives_no_effect() -> None:
    from jstock_advisor.domain.backtest.outcome_window import WindowOutcome

    incomplete = WindowOutcome(5, OutcomeStatus.INSUFFICIENT_SERIES)

    assert sale_effect(incomplete, sold_ratio=_D("0.5"), sale_cost_rate=_D("0")) is None


@pytest.mark.parametrize(
    ("ratio", "cost"),
    [
        (_D("-0.1"), _D("0")),
        (_D("1.1"), _D("0")),
        (0.5, _D("0")),
        (_D("0.5"), _D("1")),
        (_D("0.5"), 0.01),
    ],
)
def test_the_ratio_and_the_cost_are_registered_decimals_in_range(
    ratio: object, cost: object
) -> None:
    with pytest.raises(ReplayInputError):
        sale_effect(_outcome("0.1"), sold_ratio=ratio, sale_cost_rate=cost)  # type: ignore[arg-type]


# =================================================================================================
# T1 最小セル数
# =================================================================================================


def test_a_numeric_cell_below_the_minimum_gives_the_count_only() -> None:
    cell = summarize([_D("1"), _D("2")], min_cell=3)

    assert (cell.n, cell.suppressed, cell.mean, cell.median) == (2, True, None, None)


def test_a_numeric_cell_at_the_minimum_gives_mean_and_median() -> None:
    odd = summarize([_D("3"), _D("1"), _D("2")], min_cell=3)
    even = summarize([_D("1"), _D("2"), _D("3"), _D("4")], min_cell=3)

    assert (odd.suppressed, odd.mean, odd.median) == (False, _D("2"), _D("2"))
    assert (even.mean, even.median) == (_D("2.5"), _D("2.5"))


def test_an_empty_cell_never_gives_a_value() -> None:
    cell = summarize([], min_cell=1)

    assert cell.n == 0 and cell.suppressed and cell.mean is None


def test_a_rate_is_shown_only_when_the_cell_and_the_total_reach_the_minimum() -> None:
    cells = rate_table({"A": 6, "B": 2, "C": 0}, min_cell=3)

    by_label = {c.label: c for c in cells}
    assert by_label["A"].rate == Fraction(6, 8) and not by_label["A"].suppressed
    assert by_label["B"].rate is None and by_label["B"].suppressed and by_label["B"].n == 2
    assert by_label["C"].suppressed and by_label["C"].n == 0
    assert [c.label for c in cells] == ["A", "B", "C"]  # 固定の順序


def test_a_small_total_shows_no_rate_at_all() -> None:
    cells = rate_table({"A": 2, "B": 1}, min_cell=5)

    assert all(c.suppressed and c.rate is None for c in cells)
    assert [c.n for c in cells] == [2, 1]  # 件数は出す
    assert all(c.rate is None for c in rate_table({}, min_cell=1))


@pytest.mark.parametrize("minimum", [0, -1, True, "3", 2.5, None])
def test_the_minimum_cell_is_a_required_positive_integer(minimum: object) -> None:
    with pytest.raises(ReplayInputError):
        summarize([_D("1")], min_cell=minimum)  # type: ignore[arg-type]
    with pytest.raises(ReplayInputError):
        rate_table({"A": 1}, min_cell=minimum)  # type: ignore[arg-type]


def test_the_minimum_cell_has_no_default() -> None:
    with pytest.raises(TypeError):
        summarize([_D("1")])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        rate_table({"A": 1})  # type: ignore[call-arg]


# =================================================================================================
# G1 事前登録の検証
# =================================================================================================

_BODY = {
    "content_version": 1,
    "parameter_set": "SYNTHETIC",
    "registration_id": "PREREG-0-01",
    "parameters": [
        {"name": "window", "kind": "NUMBER", "value": 5},
        {"name": "origin", "kind": "CHOICE", "value": "NEXT_DAY_OPEN"},
    ],
}


def test_the_canonical_form_is_sorted_compact_utf8() -> None:
    text = canonical_json({"b": [1, {"d": None, "c": True}], "a": "日本"})

    assert text == '{"a":"日本","b":[1,{"c":true,"d":null}]}'
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})  # キー順に依らない


def test_the_hash_is_sha256_of_the_canonical_utf8_body() -> None:
    expected = hashlib.sha256(
        json.dumps(_BODY, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    assert content_hash(_BODY) == expected
    assert len(content_hash(_BODY)) == 64
    changed = {**_BODY, "parameter_set": "OTHER"}
    assert content_hash(changed) != content_hash(_BODY)  # 1 文字でも変われば変わる


@pytest.mark.parametrize(
    "body",
    [
        {"a": 1.5},
        {"a": _D("1")},
        {"a": {1, 2}},
        {"a": float("nan")},
        {1: "x"},
        {"a": b"x"},
        {"a": object()},
    ],
)
def test_only_json_types_are_allowed_in_a_body(body: object) -> None:
    with pytest.raises(ReplayInputError):
        canonical_json(body)  # type: ignore[arg-type]


def test_a_matching_report_is_accepted() -> None:
    verdict = check_report(
        _BODY,
        cited_registration_id="PREREG-0-01",
        cited_hash=content_hash(_BODY),
        used={"window": 5, "origin": "NEXT_DAY_OPEN"},
    )

    assert verdict is RegistrationCheck.MATCH


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"cited_hash": "0" * 64}, RegistrationCheck.HASH_MISMATCH),
        ({"cited_hash": None}, RegistrationCheck.HASH_MISMATCH),
        ({"cited_registration_id": "PREREG-0-02"}, RegistrationCheck.ID_MISMATCH),
        ({"used": {"window": 10, "origin": "NEXT_DAY_OPEN"}}, RegistrationCheck.VALUES_DIFFER),
        ({"used": {"window": 5}}, RegistrationCheck.VALUES_DIFFER),  # 使われていない登録値
        (
            {"used": {"window": 5, "origin": "NEXT_DAY_OPEN", "extra": 1}},
            RegistrationCheck.VALUES_DIFFER,
        ),
    ],
)
def test_each_kind_of_mismatch_has_its_own_verdict(
    kwargs: dict[str, object], expected: RegistrationCheck
) -> None:
    arguments: dict[str, object] = {
        "cited_registration_id": "PREREG-0-01",
        "cited_hash": content_hash(_BODY),
        "used": {"window": 5, "origin": "NEXT_DAY_OPEN"},
    }
    arguments.update(kwargs)

    assert check_report(_BODY, **arguments) is expected  # type: ignore[arg-type]


def test_the_order_of_the_verdicts_is_shape_then_hash_then_id_then_values() -> None:
    both_wrong = check_report(
        _BODY, cited_registration_id="X", cited_hash="bad", used={"window": 99}
    )

    assert both_wrong is RegistrationCheck.HASH_MISMATCH  # 最初に当たったもので返る


@pytest.mark.parametrize(
    "body",
    [
        {"parameters": []},  # registration_id なし
        {"registration_id": "", "parameters": []},
        {"registration_id": "P", "parameters": "x"},
        {"registration_id": "P", "parameters": [{"value": 1}]},  # 名前なし
        {
            "registration_id": "P",
            "parameters": [{"name": "a", "value": 1}, {"name": "a", "value": 2}],
        },
        {"registration_id": "P", "parameters": [{"name": "a"}]},  # 値なし
        {"registration_id": "P", "parameters": [], "x": 1.5},
    ],
)
def test_a_malformed_body_is_reported_as_malformed_without_raising(body: dict[str, object]) -> None:
    verdict = check_report(body, cited_registration_id="P", cited_hash="x", used={})

    assert verdict is RegistrationCheck.MALFORMED


def test_the_values_comparison_reports_missing_unexpected_and_differing_separately() -> None:
    comparison = compare_used_values(_BODY, {"window": 6, "extra": 1})

    assert comparison.missing == ("origin",)
    assert comparison.unexpected == ("extra",)
    assert comparison.differing == ("window",)
    assert not comparison.ok
    assert compare_used_values(_BODY, {"window": 5, "origin": "NEXT_DAY_OPEN"}).ok


def test_registered_values_are_read_by_name() -> None:
    assert registered_values(_BODY) == {"window": 5, "origin": "NEXT_DAY_OPEN"}


def test_an_edited_registration_is_detected_by_the_cited_hash() -> None:
    """登録の本文が後から書き換えられると、報告書が引用する(元の)ハッシュと一致しなくなる。"""
    original_hash = content_hash(_BODY)
    edited = {
        **_BODY,
        "parameters": [{"name": "window", "kind": "NUMBER", "value": 6}, _BODY["parameters"][1]],
    }  # type: ignore[index]

    verdict = check_report(
        edited,
        cited_registration_id="PREREG-0-01",
        cited_hash=original_hash,
        used={"window": 6, "origin": "NEXT_DAY_OPEN"},
    )

    assert (
        verdict is RegistrationCheck.HASH_MISMATCH
    )  # 使った値が(書き換え後の)本体と一致していても無効

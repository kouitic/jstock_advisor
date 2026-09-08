"""downside scenario導出(Issue #20 O-C)のテスト。

保存済みRecommendationの判定時点スナップショットだけから下方除外を再構成できる
こと、上方除外・未知コードを取り込まないこと(fail-closed)、復元できない場合に
値を捏造しないこと、「下方除外0件」と「観測不能」を混同しないことを固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.downside_valuation_scenario import (
    UNAVAILABLE_ACTUAL_VALUE_MISSING,
    UNAVAILABLE_METHOD_MISSING,
    UNAVAILABLE_NO_EXCLUSION_SNAPSHOT,
    DownsideScenarioKind,
    derive_downside_valuation_observation,
)
from jstock_advisor.domain.valuation.valuation_spread_observation import ObservationStatus

_NOW = dt.datetime(2026, 9, 5, 23, 1, tzinfo=dt.UTC)


def _recommendation(facts: dict[str, Any] | None) -> Recommendation:
    return Recommendation(
        recommendation_id="downside-rec-1",
        stock_code="8306",
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1408"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-test",
        buy_score_input_facts=facts,
    )


def _entry(
    method: str,
    code: str,
    *,
    actual_value: str | None = "620",
    reference_value: str | None = "700",
    message: str | None = "判定時点の除外理由",
) -> dict[str, Any]:
    return {
        "method": method,
        "code": code,
        "message": message,
        "actual_value": actual_value,
        "reference_value": reference_value,
    }


def test_each_downward_reason_becomes_a_scenario() -> None:
    """T1: 下方3コードがそれぞれ固有のscenario_kindへ分類されること。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("per", "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE"),
                    _entry("target_yield", "EXTREME_LOW_RELATIVE_TO_MEDIAN"),
                    _entry("dcf", "BELOW_52_WEEK_LOW"),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.AVAILABLE
    assert [s.scenario_kind for s in observation.scenarios] == [
        DownsideScenarioKind.EXTREME_RELATIVE_TO_CURRENT_PRICE,
        DownsideScenarioKind.METHOD_DIVERGENT_DOWNSIDE,
        DownsideScenarioKind.HISTORICAL_PRICE_RELATIVE_DOWNSIDE,
    ]
    first = observation.scenarios[0]
    assert first.method == "per"
    assert first.code == "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE"
    assert first.fair_value == Decimal("620")
    assert first.reference_value == Decimal("700")
    assert first.message == "判定時点の除外理由"
    # 一覧はanchor集計から除外された値だけを含むため、常にFalse。
    assert all(s.used_in_anchor is False for s in observation.scenarios)


def test_upward_exclusions_are_not_scenarios() -> None:
    """T2: 上方除外はdownside scenarioにしないこと。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "DCF_UPWARD_DIVERGENCE"),
                    _entry("per", "EXTREME_HIGH_RELATIVE_TO_MEDIAN"),
                ]
            }
        )
    )

    # 観測自体はできている(スナップショットは存在する)が、下方除外は0件。
    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.scenarios == ()


def test_unknown_code_is_fail_closed() -> None:
    """T3: 将来追加されうる未知コードを下方と推測して取り込まないこと。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "SOME_FUTURE_UNKNOWN_CODE"),
                    _entry("per", "BELOW_52_WEEK_LOW"),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.AVAILABLE
    assert [s.code for s in observation.scenarios] == ["BELOW_52_WEEK_LOW"]


def test_multiple_scenarios_preserve_persisted_order() -> None:
    """T4: 複数の下方除外を全件保持し、保存順を維持すること。

    最も低い1件へ畳み込まない。
    """
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW", actual_value="620"),
                    _entry("per", "EXTREME_LOW_RELATIVE_TO_MEDIAN", actual_value="300"),
                    _entry("target_yield", "BELOW_52_WEEK_LOW", actual_value="480"),
                ]
            }
        )
    )

    assert [s.method for s in observation.scenarios] == ["dcf", "per", "target_yield"]
    assert [s.fair_value for s in observation.scenarios] == [
        Decimal("620"),
        Decimal("300"),
        Decimal("480"),
    ]


def test_snapshot_present_with_no_downward_exclusion_is_available_and_empty() -> None:
    """T5: スナップショットがあり下方除外0件はAVAILABLE(空)であること。"""
    observation = derive_downside_valuation_observation(
        _recommendation({"valuation_outlier_exclusions": []})
    )

    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.scenarios == ()
    assert observation.unavailable_reason is None


def test_legacy_record_without_snapshot_is_unavailable() -> None:
    """T6: スナップショット自体が無い旧レコードは観測不能であること。

    「下方除外0件」と同じ状態にしない。
    """
    for facts in (None, {}, {"valuation_outlier_exclusions": None}):
        observation = derive_downside_valuation_observation(_recommendation(facts))
        assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
        assert observation.unavailable_reason == UNAVAILABLE_NO_EXCLUSION_SNAPSHOT
        assert observation.scenarios == ()


def test_missing_actual_value_does_not_fabricate_a_price() -> None:
    """T7: 除外値が復元できない場合に値を捏造しないこと。

    一部だけを提示すると件数・水準を過小に見せるため、観測全体を不能とする。
    """
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW", actual_value="620"),
                    _entry("per", "EXTREME_LOW_RELATIVE_TO_MEDIAN", actual_value=None),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert observation.unavailable_reason == UNAVAILABLE_ACTUAL_VALUE_MISSING
    assert observation.scenarios == ()


def test_unparsable_actual_value_is_treated_as_missing() -> None:
    """T7補: 数値として解釈できない値も推測で補完しないこと。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW", actual_value="N/A"),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert observation.unavailable_reason == UNAVAILABLE_ACTUAL_VALUE_MISSING


def test_low_52_week_is_not_reverse_calculated_from_threshold() -> None:
    """T8: reference_valueを0.50で割って52週安値を復元しないこと。

    0.50は将来変更されうる閾値であり、現在の閾値から過去の事実を再構成すると
    閾値変更時に過去レコードの表示が遡って誤りになる。導出結果は保存済みの
    reference_valueをそのまま持つだけであることを固定する。
    """
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW", reference_value="857.000"),
                ]
            }
        )
    )

    scenario = observation.scenarios[0]
    assert scenario.reference_value == Decimal("857.000")
    # 52週安値そのもの(= 857 / 0.50 = 1714)を持つフィールドは存在しない。
    assert not any("52" in name for name in vars(scenario))
    assert Decimal("1714") not in set(vars(scenario).values())


def test_missing_reference_value_is_allowed() -> None:
    """reference_valueが保存されていなくてもscenario自体は成立すること。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW", reference_value=None),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.AVAILABLE
    assert observation.scenarios[0].reference_value is None


def test_malformed_entries_are_skipped_without_raising() -> None:
    """下方かどうかを判定できないentryは、例外を投げずにskipすること。

    entry自体がdictでない場合とcodeが無い/不正な場合は、そもそも下方除外だと
    認識できないためskipでよい(下方と分かったうえでの欠損とは別扱い。
    後続のテスト参照)。
    """
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    "not-a-dict",
                    {"method": "dcf"},
                    {"method": "dcf", "code": ""},
                    {"method": "dcf", "code": 123},
                    _entry("per", "BELOW_52_WEEK_LOW"),
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.AVAILABLE
    assert [s.method for s in observation.scenarios] == ["per"]


def test_known_downward_entry_without_method_is_unavailable() -> None:
    """T19: 下方除外と分かったentryのmethodが欠けていたら観測不能とすること。

    actual_value欠損と同じ意味論にそろえる。下方除外の存在を認識できた以上、
    そのentryだけを黙って落として件数を過小に見せてはならない。
    """
    observation = derive_downside_valuation_observation(
        _recommendation({"valuation_outlier_exclusions": [{"code": "BELOW_52_WEEK_LOW"}]})
    )

    assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert observation.unavailable_reason == UNAVAILABLE_METHOD_MISSING
    assert observation.scenarios == ()


def test_valid_scenario_plus_method_missing_returns_no_partial_result() -> None:
    """T20: 復元できた分だけを返さないこと(件数の過小表示を防ぐ)。"""
    observation = derive_downside_valuation_observation(
        _recommendation(
            {
                "valuation_outlier_exclusions": [
                    _entry("dcf", "BELOW_52_WEEK_LOW"),
                    {"code": "EXTREME_LOW_RELATIVE_TO_MEDIAN", "actual_value": "300"},
                ]
            }
        )
    )

    assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
    assert observation.unavailable_reason == UNAVAILABLE_METHOD_MISSING
    assert observation.scenarios == ()


def test_known_downward_entry_with_blank_method_is_unavailable() -> None:
    """T21: methodが空文字・空白のみでも観測不能とすること。"""
    for blank in ("", "   ", None, 123):
        observation = derive_downside_valuation_observation(
            _recommendation(
                {
                    "valuation_outlier_exclusions": [
                        _entry("dcf", "BELOW_52_WEEK_LOW") | {"method": blank}
                    ]
                }
            )
        )
        assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE
        assert observation.unavailable_reason == UNAVAILABLE_METHOD_MISSING


def test_non_finite_actual_value_is_rejected() -> None:
    """T22/T23: NaN・Infinityを有効な金額として通さないこと。

    DecimalとしてはNaN/Infinityも構築できてしまうため、欠損と同じ扱いにする。
    """
    for bad in ("NaN", "nan", "Infinity", "-Infinity", "inf", Decimal("NaN")):
        observation = derive_downside_valuation_observation(
            _recommendation(
                {
                    "valuation_outlier_exclusions": [
                        _entry("dcf", "BELOW_52_WEEK_LOW", actual_value=bad)  # type: ignore[arg-type]
                    ]
                }
            )
        )
        assert observation.status is ObservationStatus.OBSERVATION_UNAVAILABLE, bad
        assert observation.unavailable_reason == UNAVAILABLE_ACTUAL_VALUE_MISSING


def test_non_finite_reference_value_is_dropped_but_scenario_survives() -> None:
    """T24: reference_valueが不正・非有限でもscenarioは成立し、Noneへ落とすこと。

    reference_valueは補助情報のため観測全体を落とさないが、非有限値を
    表示層へは渡さない。
    """
    for bad in ("NaN", "Infinity", "-Infinity", "not-a-number", Decimal("Infinity")):
        observation = derive_downside_valuation_observation(
            _recommendation(
                {
                    "valuation_outlier_exclusions": [
                        _entry("dcf", "BELOW_52_WEEK_LOW", reference_value=bad)  # type: ignore[arg-type]
                    ]
                }
            )
        )
        assert observation.status is ObservationStatus.AVAILABLE, bad
        assert len(observation.scenarios) == 1
        assert observation.scenarios[0].reference_value is None
        assert observation.scenarios[0].fair_value == Decimal("620")


def test_derivation_is_deterministic_and_does_not_mutate_the_recommendation() -> None:
    """導出は判定時点値の再編成であり、入力を変更しないこと。"""
    recommendation = _recommendation(
        {"valuation_outlier_exclusions": [_entry("dcf", "BELOW_52_WEEK_LOW")]}
    )
    before = recommendation.model_dump()

    first = derive_downside_valuation_observation(recommendation)
    second = derive_downside_valuation_observation(recommendation)

    assert first == second
    assert recommendation.model_dump() == before

"""Issue #189: valuation のばらつき閾値(low_max / medium_max / auto_buy_block / anchor_block)と
集約規則の版を、判定時点のスナップショット(`Recommendation.config_values_used["valuation_dispersion"]`)
へ記録する。

背景:
    `config_values_used` は「判定時点の config 値を記録し、後から過去の判定を説明するときに、
    config 変更後の"現在の"値で誤って再解釈しない」ために存在する。valuation_dispersion の
    4 閾値のうち、記録されていたのは medium_max と anchor_block(#186)だけで、low_max と
    auto_buy_block は記録されていなかった。集約規則(band → 集約器の選択)の版も、保存済みの
    Recommendation を後から再計算して検証するために要る(#260 の是正の前後を区別できない)。

固定するもの(設計 = #189 issuecomment-5956867675 §4 の T1〜T7、MANAGER 判断 Q1・Q2 =
issuecomment-5956930955):
    T1  BUY の Recommendation が、4 閾値 + 集約規則の版を、判定時点の config の値で持つ
    T2  判定後に config を変えても、保存済みの値は変わらない(過去判定を現在値で再解釈しない)。
        別の config で判定すれば、別の値が記録される(定数ではなく、判定時点の値である)
    T3  既存の個別キー(valuation_dispersion_anchor_block / valuation_dispersion_medium_max)は
        従来どおりの値で残る(読み手の互換。ブロックとの重複は意図的)
    T4  DecisionSnapshot へ config_values_used として伝播する
    T5  旧 record(キー無し)を読み込める。欠落は「不明」として扱われ、現在値で埋まらない
    T6  ValuationDispersionThresholds の全 field がブロックに出ている(新しい field の記録漏れを
        検出する。Q2 の guard)
    T7  判定結果が記録の追加で変わらない(平常時不変)
    ほか 集約規則の版は v2(Q1)。規則を変えたら版を上げる運用(人が上げる)

限界:
    ・集約規則の版は人が上げる値である。規則そのものの変更の検知は、band 別の集約器を固定する
      tests/unit/test_issue_260_anchor_monotone_clamp.py が担う(版を上げ忘れの自動検知ではない)
    ・旧 record へ版を backfill しない(キー無し = 規則の版が不明。v1 以前を含む)

fixture は既存の BuySignalService のテスト(tests/unit/test_buy_signal_service.py)の架空値を
再利用する(実在の氏名・所有者・数量・単価を使わない)。
"""

from __future__ import annotations

from typing import Any

import pytest

from jstock_advisor.config.models import ValuationDispersionThresholds
from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.enums import DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.valuation_methods import (
    VALUATION_AGGREGATION_RULE_VERSION,
    valuation_dispersion_config_values,
)
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome, BuySignalService
from tests.factories import build_recommendation
from tests.unit.test_buy_signal_service import (
    _CALENDAR,
    _CONFIG,
    _NIHON_SHINYAKU,
    _NOW,
    _build_snapshot,
    _providers,
)

_BLOCK_KEY = "valuation_dispersion"

# 実際の config(config/buy_decision_rules.yaml)と値が異なる、別の閾値(T2)。
# 順序制約(low_max < medium_max < auto_buy_block < anchor_block)を満たす。
_OTHER_THRESHOLDS = ValuationDispersionThresholds(
    low_max=1.20, medium_max=1.50, auto_buy_block=1.90, anchor_block=40.0
)


def _analyze_with(
    monkeypatch: pytest.MonkeyPatch, config: Any, helper_override: Any = None
) -> BuyAnalysisOutcome:
    snapshot = _build_snapshot(_NIHON_SHINYAKU)
    monkeypatch.setattr(service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None))
    if helper_override is not None:
        monkeypatch.setattr(service_module, "valuation_dispersion_config_values", helper_override)
    service = BuySignalService(providers=_providers(), config=config, business_calendar=_CALENDAR)
    return service.analyze(_NIHON_SHINYAKU.stock_code, _NOW, RecommendationType.BUY)


def _config_with_thresholds(thresholds: ValuationDispersionThresholds) -> Any:
    buy_decision = _CONFIG.buy_decision.model_copy(update={"valuation_dispersion": thresholds})
    return _CONFIG.model_copy(update={"buy_decision": buy_decision})


def _recommendation_of(outcome: BuyAnalysisOutcome) -> Recommendation:
    assert outcome.recommendation is not None
    return outcome.recommendation


def _expected_block(thresholds: ValuationDispersionThresholds) -> dict[str, Any]:
    return {
        "low_max": thresholds.low_max,
        "medium_max": thresholds.medium_max,
        "auto_buy_block": thresholds.auto_buy_block,
        "anchor_block": thresholds.anchor_block,
        "aggregation_rule_version": "v2",
    }


# --- T1 ---------------------------------------------------------------------


def test_t1_buy_recommendation_records_four_thresholds_and_rule_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))

    assert rec.config_values_used[_BLOCK_KEY] == _expected_block(
        _CONFIG.buy_decision.valuation_dispersion
    )


def test_t1_block_has_exactly_the_documented_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))

    assert set(rec.config_values_used[_BLOCK_KEY]) == {
        "low_max",
        "medium_max",
        "auto_buy_block",
        "anchor_block",
        "aggregation_rule_version",
    }


# --- T2 ---------------------------------------------------------------------


def test_t2_recorded_values_follow_the_decision_time_config_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別の config で判定すれば別の値が記録される。記録を欠く・定数にした実装で落ちる。"""
    other_config = _config_with_thresholds(_OTHER_THRESHOLDS)

    rec_default = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))
    rec_other = _recommendation_of(_analyze_with(monkeypatch, other_config))

    assert rec_default.config_values_used[_BLOCK_KEY] == _expected_block(
        _CONFIG.buy_decision.valuation_dispersion
    )
    assert rec_other.config_values_used[_BLOCK_KEY] == _expected_block(_OTHER_THRESHOLDS)
    assert rec_default.config_values_used[_BLOCK_KEY] != rec_other.config_values_used[_BLOCK_KEY]


def test_t2_saved_record_is_not_changed_by_a_later_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定後に別 config で別の判定をしても、先に作った記録の値は変わらない(config への参照を
    持たない独立した値)。保存・復元(JSON の往復)でも同じ値が残る。"""
    rec_before = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))
    snapshot_before = dict(rec_before.config_values_used[_BLOCK_KEY])

    _analyze_with(monkeypatch, _config_with_thresholds(_OTHER_THRESHOLDS))

    assert rec_before.config_values_used[_BLOCK_KEY] == snapshot_before
    restored = Recommendation.model_validate_json(rec_before.model_dump_json())
    assert restored.config_values_used[_BLOCK_KEY] == snapshot_before


# --- T3 ---------------------------------------------------------------------


def test_t3_existing_individual_keys_remain_with_the_same_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))
    thresholds = _CONFIG.buy_decision.valuation_dispersion

    assert rec.config_values_used["valuation_dispersion_anchor_block"] == thresholds.anchor_block
    assert rec.config_values_used["valuation_dispersion_medium_max"] == thresholds.medium_max
    # ブロックとの重複は意図的(読み手の互換)。値は常に一致する。
    block = rec.config_values_used[_BLOCK_KEY]
    assert block["anchor_block"] == rec.config_values_used["valuation_dispersion_anchor_block"]
    assert block["medium_max"] == rec.config_values_used["valuation_dispersion_medium_max"]


# --- T4 ---------------------------------------------------------------------


def test_t4_block_propagates_to_decision_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))

    decision = build_decision_snapshot(rec, DecisionType.BUY)

    assert decision.config_values_used[_BLOCK_KEY] == rec.config_values_used[_BLOCK_KEY]


# --- T5 ---------------------------------------------------------------------


def test_t5_old_record_without_the_key_is_readable_and_not_backfilled() -> None:
    """キーを持たない旧 record は読み込める。欠落は「不明」のまま(現在値で埋めない)。"""
    old = build_recommendation(config_values_used={"valuation_dispersion_medium_max": 1.6})

    restored = Recommendation.model_validate_json(old.model_dump_json())

    assert _BLOCK_KEY not in restored.config_values_used
    assert restored.config_values_used.get(_BLOCK_KEY) is None
    decision = build_decision_snapshot(restored, DecisionType.BUY)
    assert _BLOCK_KEY not in decision.config_values_used


# --- T6 ---------------------------------------------------------------------


def test_t6_block_covers_every_field_of_the_thresholds_model() -> None:
    """新しい field を ValuationDispersionThresholds へ足したら、記録も要る(Q2 の guard)。"""
    block = valuation_dispersion_config_values(_OTHER_THRESHOLDS)

    missing = set(ValuationDispersionThresholds.model_fields) - set(block)
    assert missing == set(), f"ブロックに出ていない閾値 field: {sorted(missing)}"
    # ブロックの余分なキーは、版のみ(閾値以外が黙って混ざらない)。
    assert set(block) - set(ValuationDispersionThresholds.model_fields) == {
        "aggregation_rule_version"
    }


def test_t6_block_values_equal_the_given_thresholds() -> None:
    block = valuation_dispersion_config_values(_OTHER_THRESHOLDS)

    for name in ValuationDispersionThresholds.model_fields:
        assert block[name] == getattr(_OTHER_THRESHOLDS, name)


# --- T7 ---------------------------------------------------------------------


def test_t7_decision_is_unchanged_by_the_added_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """記録の追加は判定結果を変えない。ブロックを出さない実装(旧挙動)と、判定・価格・信頼度が一致し、
    config_values_used の差は新しいキー 1 つだけである。"""
    with_block = _recommendation_of(_analyze_with(monkeypatch, _CONFIG))
    without_block = _recommendation_of(_analyze_with(monkeypatch, _CONFIG, lambda _config: {}))

    # 旧挙動の再現(ブロックが空 dict)が実際に差を作っていること(この比較が空振りでない)。
    assert without_block.config_values_used[_BLOCK_KEY] == {}
    assert with_block.config_values_used[_BLOCK_KEY] != {}

    for field in (
        "buy_action",
        "buy_prices",
        "valuation_anchor",
        "confidence",
        "recommendation_type",
        "reasons",
        "total_score",
        "valuation_dispersion_ratio",
    ):
        assert getattr(with_block, field) == getattr(without_block, field), field
    keys_with = set(with_block.config_values_used)
    keys_without = set(without_block.config_values_used)
    assert keys_with == keys_without
    others_with = {k: v for k, v in with_block.config_values_used.items() if k != _BLOCK_KEY}
    others_without = {k: v for k, v in without_block.config_values_used.items() if k != _BLOCK_KEY}
    assert others_with == others_without


# --- 集約規則の版(Q1)-------------------------------------------------------


def test_aggregation_rule_version_is_v2_after_the_issue_260_fix() -> None:
    """Q1: #260 の是正後の規則を v2 とし、それ以前(キー無し)は版が不明として扱う。

    落ちたら: 集約規則(band → 集約器の選択。compute_valuation_anchor)を変えたか、版を上げた。
    規則の変更なら、版を上げ、tests/unit/test_issue_260_anchor_monotone_clamp.py(band 別の集約器を
    固定するテスト)の更新とあわせてレビューで示す。
    """
    assert VALUATION_AGGREGATION_RULE_VERSION == "v2"

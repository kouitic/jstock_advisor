"""Issue #67 F-I2 / F-I6: 保有判断経路のfair value保存とrule_version分離。

F-I2  保有判断のRecommendationがfair value 6フィールド + #21の3フィールドを
      保存しない欠陥(profit_taking_service.pyでは既に保存している)。
F-I6  保有判断のみrule_versionへscoring_model_versionを格納しており、
      RuleVersionServiceが管理する「ルール版」概念と混在していた欠陥。

fixtureは既存のtest_issue_67_recommendation_provenance_transfer.pyの
架空データ・helperを再利用する(同じsnapshot構築コードを重複させない)。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueUnusableReasonCode,
)
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.rule_version_service import RuleVersionService
from tests.unit.test_issue_67_recommendation_provenance_transfer import (
    _CONFIG,
    _NOT_EVALUATED_EXIT_PRICE_RANGE,
    _base_snapshot,
    _holding,
    _holding_decision_result,
    _register_fictional_stock,  # noqa: F401 - autouse fixture
)

# --- F-I2: fair value 6フィールド + #21の3フィールド ---------------------------------


def test_usable_fair_value_range_is_transferred_to_the_recommendation() -> None:
    """fair_value_rangeが使用可能な場合、9フィールドすべてが転記される。

    profit_taking_service.pyの既存規約(fair_value_methodsは
    method/fair_value/confidence/exclusion_reasonのdict列)と同じ形。
    """
    snapshot = _base_snapshot()
    fv_range = snapshot.fair_value_range
    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )

    assert rec.fair_value_bear == fv_range.bear
    assert rec.fair_value_neutral == fv_range.neutral
    assert rec.fair_value_bull == fv_range.bull
    assert rec.fair_value_overall_confidence == fv_range.overall_confidence
    assert rec.fair_value_spread_ratio is not None or fv_range.bear in (None, 0)
    assert rec.fair_value_usable_for_trading_judgment == fv_range.usable_for_trading_judgment
    assert rec.fair_value_unusable_reason == fv_range.unusable_reason
    expected_method_count = len(fv_range.methods_used) + len(fv_range.methods_excluded)
    assert len(rec.fair_value_methods) == expected_method_count
    if expected_method_count:
        sample = rec.fair_value_methods[0]
        assert set(sample) == {"method", "fair_value", "confidence", "exclusion_reason"}


def test_unusable_fair_value_range_reason_is_preserved() -> None:
    """usable_for_trading_judgment=Falseの場合、unusable_reason/reason_codeが
    捏造されず、fair_value_rangeそのままの値で保存される
    (#21が解こうとした「理由がレコードに残らない」失敗モードの回帰防止)。
    """
    snapshot = _base_snapshot()
    unusable_fv_range = snapshot.fair_value_range.model_copy(
        update={
            "usable_for_trading_judgment": False,
            "unusable_reason": "テスト用: 算出方式が1つも成立しなかった",
            "unusable_reason_code": FairValueUnusableReasonCode.NO_VALID_METHODS,
        }
    )
    snapshot = dataclasses.replace(snapshot, fair_value_range=unusable_fv_range)

    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )

    assert rec.fair_value_usable_for_trading_judgment is False
    assert rec.fair_value_unusable_reason == "テスト用: 算出方式が1つも成立しなかった"
    assert rec.fair_value_unusable_reason_code == FairValueUnusableReasonCode.NO_VALID_METHODS.value


def test_fair_value_spread_ratio_uses_the_same_formula_as_profit_taking() -> None:
    """fair_value_spread_ratio = bull/bear(profit_taking_service.pyと同じ式)。

    bear=1000/bull=1500という具体値でspread_ratio=1.5を直接assertする
    (レビュー指摘対応: bear=None一本槍のテストでは、実装をbull/bearから
    bear/bullへ反転してもPASSしてしまい、「式の固定」になっていなかった。
    具体値のassertにより、逆式(bear/bull=1000/1500=0.666...)ならこの
    assertが必ず失敗する)。
    """
    snapshot = _base_snapshot()
    fv_range = snapshot.fair_value_range.model_copy(
        update={"bear": Decimal("1000"), "bull": Decimal("1500")}
    )
    snapshot = dataclasses.replace(snapshot, fair_value_range=fv_range)

    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )
    assert rec.fair_value_spread_ratio == pytest.approx(1.5)


def test_fair_value_spread_ratio_is_none_when_bear_is_missing() -> None:
    """bearが0またはNoneの場合はNoneのまま(ゼロ除算・捏造をしない)既存契約を
    維持する(上のテストとは別観点として残す)。
    """
    snapshot = _base_snapshot()
    fv_range = snapshot.fair_value_range.model_copy(
        update={
            "bear": None,
            "methods_used": [
                FairValueMethodResult(
                    method="per",
                    fair_value=None,
                    confidence=ConfidenceLevel.LOW,
                )
            ],
            "methods_excluded": [],
        }
    )
    snapshot = dataclasses.replace(snapshot, fair_value_range=fv_range)

    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )
    assert rec.fair_value_spread_ratio is None


# --- F-I6: rule_version / scoring_model_version の分離 -------------------------------


def test_rule_version_argument_is_stored_as_is() -> None:
    """build_holding_decision_recommendation()自体はrule_version引数を
    そのまま保存する(呼び出し元がRuleVersionServiceの値を渡す責務を持つ。
    呼び出し元側の実測はtest_holdings_watchlist_handler.py側で行う)。
    """
    snapshot = _base_snapshot()
    rule_version_service = RuleVersionService()
    active_version = rule_version_service.get_active_version_or("v1-mvp")

    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        active_version,
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )
    assert rec.rule_version == active_version
    # scoring_model_versionの文字列表現(旧実装が誤ってrule_versionへ入れていた値)と
    # 偶然一致しない限り、両者は別の値であるべき(回帰防止の弱い保証)。
    scoring_model_version_str = str(_holding_decision_result().scoring_model_version)
    if active_version != scoring_model_version_str:
        assert rec.rule_version != scoring_model_version_str


def test_scoring_model_version_is_stored_in_config_values_used() -> None:
    """scoring_model_versionはrule_versionから分離し、config_values_usedへ
    明示キーとして保存される(値そのものは失わない)。
    """
    snapshot = _base_snapshot()
    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )
    assert rec.config_values_used["scoring_model_version"] == str(
        _CONFIG.holding_decision.scoring_model_version
    )


# --- F-I2 M-2: DecisionSnapshotへの伝播(既存の汎用転記経路。コード変更なし) -----------


def test_fair_value_propagates_to_the_decision_snapshot() -> None:
    """build_decision_snapshot()は変更していない。M-1でRecommendation側に
    fair_value_*が正しく入るようになったことで、既存の転記経路が実際に
    DecisionSnapshotへ伝播することを実測して固定する(下流: Issue #67本文の
    「decision_snapshot_builder.py:65-68」参照)。
    """
    snapshot = _base_snapshot()
    rec = build_holding_decision_recommendation(
        _holding(),
        _holding_decision_result(),
        snapshot,
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
    )
    decision_snapshot = build_decision_snapshot(rec, DecisionType.HOLDING_DECISION)

    assert decision_snapshot.fair_value_bear == rec.fair_value_bear
    assert decision_snapshot.fair_value_neutral == rec.fair_value_neutral
    assert decision_snapshot.fair_value_bull == rec.fair_value_bull
    assert decision_snapshot.fair_value_confidence == rec.fair_value_overall_confidence
    # 対策前は rec.fair_value_bear 等が常にNoneだったため、このassertは
    # 「Noneのまま伝播する」ことしか固定できなかった。fixtureのfair_value_rangeが
    # 実際に値を持つことを前提として明示する(空振りテストにしない)。
    assert rec.fair_value_bear is not None

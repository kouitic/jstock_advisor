"""Issue #476: 優待の明示的な廃止を、投資ストーリー維持スコアの優待条件で EVALUATED・0点にする。

以前は、優待が明示的に廃止(`is_abolished=True`)されても、優待条件が「優待非保有銘柄」
(NOT_APPLICABLE)になり、減点されなかった。一方、大幅改悪は EVALUATED・0点で反映されていた。
改悪は2軸(投資ストーリー・リスク控除)・廃止は1軸(リスク控除のみ)という非対称を、
USER決定 U-A に従って解消する(投資ストーリー軸へも反映する)。

不変条件を固定する:
  * baselineに優待あり + 明示的な廃止 -> ABOLISHED(評価・0点)。大幅改悪(DOWNGRADED)と同じ点数。
  * 廃止と大幅改悪が同時なら、廃止を優先する(点数は同じ)。
  * baselineに優待なし + 現在が廃止登録は、従来どおり NOT_APPLICABLE(U-B)。
  * 初回評価(baselineを今作る)は比較不能のまま。データ欠落(登録なし)は DATA_MISSING のまま(#470)。
  * 「優待非保有銘柄」という理由文言は、廃止の保有には出ない。
  * リスク控除・企業品質は変わらない(新しい重みを足さない)。採点式の重みも変えない。
  * 共通enumの値の集合と、保存形式(`ScoreItemDetail`)は変えない。

★ 銘柄コード・優待は架空値のみ(実在の銘柄・所有者・保有データを含まない)。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    AccountType,
    EvidenceCoverageStatus,
    ExecutionPlanReason,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    InvestmentThesisScore,
    ScoreItemDetail,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.investment_thesis_scoring import (
    BenefitConditionState,
    InvestmentThesisInputs,
    derive_benefit_condition_state,
    score_investment_thesis,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.interfaces.types import (
    BenefitDetail,
    BenefitUtilityCategory,
    ShareholderBenefit,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_S = BenefitConditionState
_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.investment_thesis_weights
_TEMPLATE = _CFG.investment_thesis_template
_FRESH = _CFG.holding_decision.fresh_within_days
_STALE = _CFG.holding_decision.stale_after_days
_NOW = dt.datetime(2026, 9, 7, tzinfo=dt.UTC)
_NOT_APPLICABLE_REASON = "優待非保有銘柄"


def _derive(
    baseline: bool | None,
    *,
    first: bool = False,
    registered: bool = True,
    abolished: bool = False,
    downgraded: bool = False,
) -> BenefitConditionState:
    return derive_benefit_condition_state(
        baseline_has_benefit=baseline,
        is_first_evaluation=first,
        benefit_registered=registered,
        benefit_is_abolished=abolished,
        benefit_is_major_downgrade=downgraded,
    )


def _score(state: BenefitConditionState) -> InvestmentThesisScore:
    inputs = InvestmentThesisInputs(
        current_total_yield_pct=_TEMPLATE.min_total_yield_pct,
        benefit_state=state,
        dividend_cut_or_omission_confirmed=False,
        profit_cf_premise_broken=False,
        financial_premise_broken=False,
        thesis=None,
    )
    return score_investment_thesis(inputs, _WEIGHTS, _TEMPLATE, _FRESH, _STALE, _NOW)


def _benefit_item(score: InvestmentThesisScore) -> ScoreItemDetail:
    return next(i for i in score.items if i.item_code == "benefit_condition")


# --- 導出(状態 #4: 明示的な廃止) -----------------------------------------------------


def test_baseline_benefit_and_explicit_abolition_is_the_abolished_state() -> None:
    assert _derive(True, abolished=True) is _S.ABOLISHED


def test_abolition_wins_over_a_simultaneous_major_downgrade() -> None:
    assert _derive(True, abolished=True, downgraded=True) is _S.ABOLISHED


def test_baseline_without_benefit_and_abolished_registration_stays_not_applicable() -> None:
    """★ U-B: baselineに優待なしなら、現在が廃止登録でも NOT_APPLICABLE(廃止を減点しない)。"""
    assert _derive(False, abolished=True) is _S.NOT_APPLICABLE
    assert _derive(False, abolished=True, downgraded=True) is _S.NOT_APPLICABLE


def test_first_evaluation_and_data_missing_are_not_changed_by_this_issue() -> None:
    assert _derive(True, first=True, abolished=True) is _S.BASELINE_NOT_COMPARABLE
    assert _derive(True, registered=False) is _S.DATA_MISSING
    assert _derive(None, abolished=True) is _S.BASELINE_NOT_COMPARABLE


# --- スコアの写像(廃止 = 大幅改悪と同じ・評価・0点) ------------------------------------


def test_abolished_maps_to_evaluated_zero_points() -> None:
    item = _benefit_item(_score(_S.ABOLISHED))

    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0
    assert item.reason != _NOT_APPLICABLE_REASON  # 「優待非保有銘柄」は廃止に出ない


def test_abolished_and_downgraded_are_symmetric_in_the_score() -> None:
    """★ 廃止と大幅改悪の非対称を解消する(同じ点数・同じcoverage)。"""
    abolished = _score(_S.ABOLISHED)
    downgraded = _score(_S.DOWNGRADED)

    assert abolished.score == downgraded.score == pytest.approx(44.444, abs=0.01)
    assert abolished.coverage_ratio == downgraded.coverage_ratio == pytest.approx(1.0)


def test_abolished_scores_lower_than_maintained_and_than_no_benefit() -> None:
    """廃止は、維持(50.0)・優待なし(50.0)より低く出る。以前は廃止が維持と同じ50.0だった。"""
    maintained = _score(_S.MAINTAINED)
    no_benefit = _score(_S.NOT_APPLICABLE)
    abolished = _score(_S.ABOLISHED)

    assert maintained.score == no_benefit.score == pytest.approx(50.0)
    assert abolished.score < maintained.score


def test_the_weight_is_the_existing_benefit_condition_weight() -> None:
    """★ 新しい重みを足さない。0点は、既存のbenefit_conditionの重みに対する0点。"""
    item = _benefit_item(_score(_S.ABOLISHED))

    assert item.weight == _WEIGHTS.benefit_condition
    assert _WEIGHTS.model_dump().keys() == {
        "dividend_policy",
        "total_yield",
        "benefit_condition",
        "profit_cf_premise",
        "financial_premise",
        "custom_conditions",
    }


def test_shared_enum_values_and_stored_shape_are_unchanged() -> None:
    assert {s.value for s in EvidenceCoverageStatus} == {
        "EVALUATED",
        "NOT_EVALUATED",
        "NOT_APPLICABLE",
    }
    assert set(ScoreItemDetail.model_fields) == {
        "item_code",
        "axis",
        "weight",
        "status",
        "points_earned",
        "reason",
    }


# --- HoldingDecisionService.evaluate(baselineの値を読み、状態を導く) ---------------------

_CODE = "2914"  # mock providerがデータを返す銘柄コード(評価の入力用)
_NOW1 = dt.datetime(2026, 8, 18, 7, 0, tzinfo=dt.UTC)
_NOW2 = dt.datetime(2026, 8, 19, 7, 0, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW2)


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, _CODE),
        stock_code=_CODE,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW1,
        updated_at=_NOW1,
    )


def _benefit(*, abolished: bool = False, downgraded: bool = False) -> ShareholderBenefit:
    return ShareholderBenefit(
        stock_code=_CODE,
        min_shares_required=100,
        benefits=[
            BenefitDetail(
                category=next(iter(BenefitUtilityCategory)),
                description="架空の優待",
                estimated_value=Decimal("1000"),
                min_shares_for_tier=100,
            )
        ],
        frequency_per_year=1,
        is_abolished=abolished,
        is_major_downgrade=downgraded,
        source=DataSourceReference(provider="test-fixture", fetched_at=_NOW1),
    )


def _snapshot(benefit: ShareholderBenefit | None, now: dt.datetime) -> StockSnapshot:
    snapshot, error = build_stock_snapshot(_PROVIDERS, _CODE, now, _CFG)
    assert snapshot is not None, error
    return dataclasses.replace(snapshot, benefit=benefit)


def _evaluate_twice(
    store_dir: Path, first: ShareholderBenefit | None, second: ShareholderBenefit | None
) -> Any:
    """1回目(初回評価。baselineを作る)と、2回目(baselineと比較する)を、同じ保存先で評価する。"""
    service = HoldingDecisionService(
        _PROVIDERS,
        _CFG,
        investment_thesis_service=InvestmentThesisService(store_dir=store_dir),
        runtime_config_service=HoldingDecisionRuntimeConfigService(store_dir=store_dir),
        audit_service=AuditService(AuditLogRepository(store_dir)),
    )
    holding = _holding()
    service.evaluate(
        holding, _NOW1, ExecutionPlanReason.NORMAL_SHADOW, snapshot=_snapshot(first, _NOW1)
    )
    outcome = service.evaluate(
        holding, _NOW2, ExecutionPlanReason.NORMAL_SHADOW, snapshot=_snapshot(second, _NOW2)
    )
    assert outcome.result is not None
    return outcome.result


def test_service_baseline_benefit_then_abolished_is_evaluated_zero_points(tmp_path: Path) -> None:
    result = _evaluate_twice(tmp_path, _benefit(), _benefit(abolished=True))

    item = _benefit_item(result.investment_thesis)
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0
    assert item.reason != _NOT_APPLICABLE_REASON


def test_service_abolished_and_downgraded_score_the_same_in_the_thesis_axis(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "d").mkdir()
    abolished = _evaluate_twice(tmp_path / "a", _benefit(), _benefit(abolished=True))
    downgraded = _evaluate_twice(tmp_path / "d", _benefit(), _benefit(downgraded=True))

    assert abolished.investment_thesis.score == pytest.approx(downgraded.investment_thesis.score)


def test_service_baseline_without_benefit_then_abolished_registration_is_not_applicable(
    tmp_path: Path,
) -> None:
    """U-B: 初回評価で優待が無かった(baselineに優待なし)保有は、後から廃止登録されても対象外。"""
    result = _evaluate_twice(tmp_path, None, _benefit(abolished=True))

    assert _benefit_item(result.investment_thesis).status == EvidenceCoverageStatus.NOT_APPLICABLE


def test_service_baseline_created_while_abolished_means_baseline_without_benefit(
    tmp_path: Path,
) -> None:
    """初回評価の時点で既に廃止されていた優待は、baselineに優待なしとして保存される(従来どおり)。"""
    result = _evaluate_twice(tmp_path, _benefit(abolished=True), _benefit(abolished=True))

    assert _benefit_item(result.investment_thesis).status == EvidenceCoverageStatus.NOT_APPLICABLE


def test_service_abolition_changes_only_the_benefit_item_of_the_thesis_axis(
    tmp_path: Path,
) -> None:
    """★ リスク控除・企業品質は変わらない(新しい重みを足さない)。変わるのは優待条件の1項目だけ。

    比較の相手 = baselineに優待なし + 廃止登録(NOT_APPLICABLE。以前の廃止の扱いと同じ点数)。
    リスク控除は現在のsnapshotだけで決まるため、両者で一致する。
    """
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    abolished = _evaluate_twice(tmp_path / "with", _benefit(), _benefit(abolished=True))
    previous = _evaluate_twice(tmp_path / "without", None, _benefit(abolished=True))

    assert abolished.risk_deduction == previous.risk_deduction
    assert abolished.company_quality == previous.company_quality
    changed = [
        a.item_code
        for a, b in zip(
            abolished.investment_thesis.items, previous.investment_thesis.items, strict=True
        )
        if a != b
    ]
    assert changed == ["benefit_condition"]
    assert abolished.investment_thesis.score < previous.investment_thesis.score

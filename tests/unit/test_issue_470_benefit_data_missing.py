"""Issue #470: 優待条件のデータ欠落(baselineで優待あり + 現在の登録なし)の semantics。

以前は、優待条件(benefit_condition)が「現在の優待の有無」だけで NOT_APPLICABLE を決めていた。
baseline時に優待があった保有で、現在の台帳に登録が無くなった(取得不能・台帳の削除・移行漏れ)場合も
「優待非保有銘柄」と区別できず、減点も不評価もされなかった。状態を、baselineの値と現在の入力から
1つの純関数(`derive_benefit_condition_state`)で導く。

不変条件を固定する:
  * データ欠落は、不評価(NOT_EVALUATED)で、廃止(0点)にも優待なし(NOT_APPLICABLE)にも倒さない。
  * 不評価の理由コード`BENEFIT_DATA_MISSING`で、初回評価の`BASELINE_NOT_COMPARABLE`と区別できる。
  * スコアは変わらず(分母から外す)、coverageだけが下がる(確認できていない事実を残す)。
  * Issue #55 Phase A Decision 3(total_yieldの欠測は分母に残る)は変えない。
  * 共通enum(`EvidenceCoverageStatus`)の値の集合と、保存形式(`ScoreItemDetail`)は変えない。
  * 明示的な廃止の扱いは #476 の範囲(本ファイルは廃止の期待値を、#476 で ABOLISHED へ更新済み)。

★ 銘柄コード・優待は架空値のみ(実在の銘柄・所有者・保有データを含まない)。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
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
    BaselineValueSnapshot,
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
_MISSING = "BENEFIT_DATA_MISSING"
_NOT_COMPARABLE = "BASELINE_NOT_COMPARABLE"


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


# --- 導出関数(状態 #1〜#6。設計 = #470 issuecomment-5747265415 §4) -----------------


def test_state_1_baseline_without_benefit_is_not_applicable_even_if_currently_abolished() -> None:
    assert _derive(False, registered=False) is _S.NOT_APPLICABLE
    assert _derive(False, abolished=True) is _S.NOT_APPLICABLE  # 現在が廃止登録でも同じ(U-B)
    assert _derive(False, downgraded=True) is _S.NOT_APPLICABLE


def test_state_2_first_evaluation_with_baseline_benefit_is_not_comparable() -> None:
    assert _derive(True, first=True) is _S.BASELINE_NOT_COMPARABLE
    assert _derive(True, first=True, registered=False) is _S.BASELINE_NOT_COMPARABLE


def test_state_3_baseline_benefit_and_no_current_registration_is_data_missing() -> None:
    assert _derive(True, registered=False) is _S.DATA_MISSING


def test_data_missing_is_never_folded_into_abolished_or_no_benefit() -> None:
    """★ 欠落を「優待なし」「廃止」へ倒さない(本Issueの目的)。"""
    missing = _derive(True, registered=False)

    assert missing is not _S.NOT_APPLICABLE
    assert missing is not _S.DOWNGRADED


def test_state_4_downgrade_is_evaluated_and_maintained_is_evaluated() -> None:
    assert _derive(True, downgraded=True) is _S.DOWNGRADED
    assert _derive(True) is _S.MAINTAINED


def test_abolished_is_its_own_evaluated_state_since_issue_476() -> None:
    """明示的な廃止は、NOT_APPLICABLEではなくABOLISHED(評価・0点)。詳細は #476 のテスト。"""
    assert _derive(True, abolished=True) is _S.ABOLISHED


def test_state_6_unknown_baseline_value_is_not_evaluated_not_no_benefit_nor_maintained() -> None:
    """baselineの値がNone(テスト・repair経路のみ)は、優待なしにも維持にも倒さない。"""
    assert _derive(None) is _S.BASELINE_NOT_COMPARABLE
    assert _derive(None, registered=False) is _S.BASELINE_NOT_COMPARABLE
    assert _derive(None, abolished=True) is _S.BASELINE_NOT_COMPARABLE


def _reference(
    baseline: bool | None, first: bool, registered: bool, abolished: bool, downgraded: bool
) -> BenefitConditionState:
    """表(設計 §4)を、導出関数とは別の書き方(優先順位の表引き)で書いた期待値。"""
    if baseline is False:
        return _S.NOT_APPLICABLE
    if baseline is None:
        return _S.BASELINE_NOT_COMPARABLE
    if first:
        return _S.BASELINE_NOT_COMPARABLE
    if not registered:
        return _S.DATA_MISSING
    return {
        (True, False): _S.ABOLISHED,
        (True, True): _S.ABOLISHED,
        (False, True): _S.DOWNGRADED,
        (False, False): _S.MAINTAINED,
    }[(abolished, downgraded)]


@pytest.mark.parametrize(
    ("baseline", "first", "registered", "abolished", "downgraded"),
    list(
        itertools.product(
            [True, False, None], [True, False], [True, False], [True, False], [True, False]
        )
    ),
)
def test_every_combination_matches_the_reference_table(
    baseline: bool | None, first: bool, registered: bool, abolished: bool, downgraded: bool
) -> None:
    """全組合せ(baseline True/False/None × 初回 × 登録 × 廃止 × 改悪)を固定する。"""
    assert _derive(
        baseline, first=first, registered=registered, abolished=abolished, downgraded=downgraded
    ) is _reference(baseline, first, registered, abolished, downgraded)


# --- スコアの写像(状態 -> status / reason / points) ----------------------------------


def _inputs(state: BenefitConditionState, **overrides: Any) -> InvestmentThesisInputs:
    base: dict[str, Any] = {
        "current_total_yield_pct": _TEMPLATE.min_total_yield_pct,
        "benefit_state": state,
        "dividend_cut_or_omission_confirmed": False,
        "profit_cf_premise_broken": False,
        "financial_premise_broken": False,
        "thesis": None,
    }
    base.update(overrides)
    return InvestmentThesisInputs(**base)


def _score(inputs: InvestmentThesisInputs) -> InvestmentThesisScore:
    return score_investment_thesis(inputs, _WEIGHTS, _TEMPLATE, _FRESH, _STALE, _NOW)


def _benefit_item(score: InvestmentThesisScore) -> ScoreItemDetail:
    return next(i for i in score.items if i.item_code == "benefit_condition")


def test_data_missing_maps_to_not_evaluated_with_its_own_reason_code() -> None:
    item = _benefit_item(_score(_inputs(_S.DATA_MISSING)))

    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED
    assert item.reason == _MISSING
    assert item.points_earned == 0.0


def test_data_missing_is_distinguishable_from_first_evaluation_and_no_benefit() -> None:
    missing = _benefit_item(_score(_inputs(_S.DATA_MISSING)))
    not_comparable = _benefit_item(_score(_inputs(_S.BASELINE_NOT_COMPARABLE)))
    not_applicable = _benefit_item(_score(_inputs(_S.NOT_APPLICABLE)))

    assert not_comparable.reason == _NOT_COMPARABLE
    assert missing.reason != not_comparable.reason
    assert not_applicable.status == EvidenceCoverageStatus.NOT_APPLICABLE
    assert missing.status != not_applicable.status


def test_downgraded_is_zero_points_and_maintained_is_full_points() -> None:
    downgraded = _benefit_item(_score(_inputs(_S.DOWNGRADED)))
    maintained = _benefit_item(_score(_inputs(_S.MAINTAINED)))

    assert (downgraded.status, downgraded.points_earned) == (EvidenceCoverageStatus.EVALUATED, 0.0)
    assert maintained.status == EvidenceCoverageStatus.EVALUATED
    assert maintained.points_earned == _WEIGHTS.benefit_condition


def test_data_missing_keeps_the_score_and_lowers_only_the_coverage() -> None:
    """★ スコアは変わらず(分母から外す)、coverageだけが下がる(確認できていない事実を残す)。"""
    maintained = _score(_inputs(_S.MAINTAINED))
    missing = _score(_inputs(_S.DATA_MISSING))
    not_applicable = _score(_inputs(_S.NOT_APPLICABLE))

    assert missing.score == pytest.approx(maintained.score) == pytest.approx(50.0)
    assert missing.coverage_ratio == pytest.approx(8 / 9)  # 優待5点分が、評価済みから外れる
    assert maintained.coverage_ratio == pytest.approx(1.0)
    assert not_applicable.coverage_ratio == pytest.approx(1.0)  # 優待なしはcoverageも下げない


def test_data_missing_is_not_treated_as_abolished() -> None:
    """★ 廃止(0点)なら 44.4 になる。欠落はそれと同じ点数にしない。"""
    downgraded = _score(_inputs(_S.DOWNGRADED))
    missing = _score(_inputs(_S.DATA_MISSING))

    assert downgraded.score == pytest.approx(44.444, abs=0.01)
    assert missing.score > downgraded.score


def test_issue_55_decision_3_missing_total_yield_still_stays_in_the_denominator() -> None:
    """★ total_yieldの欠測は従来どおり分母に残る(#55 Decision 3。本Issueは触れない)。"""
    baseline = _score(_inputs(_S.MAINTAINED, current_total_yield_pct=0.0))
    missing_yield = _score(_inputs(_S.MAINTAINED, current_total_yield_pct=None))

    assert missing_yield.score == baseline.score  # 確定0%と欠測でスコアは同じ
    assert missing_yield.coverage_ratio < baseline.coverage_ratio
    yield_item = next(i for i in missing_yield.items if i.item_code == "total_yield")
    assert yield_item.reason is None  # 理由を付けない = 分母の除外対象にならない


def test_evidence_status_values_and_stored_shape_are_unchanged() -> None:
    """共通enum(S-16)の値の集合と、ScoreItemDetailの保存形式は変えない。"""
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


def _service(store_dir: Path) -> HoldingDecisionService:
    return HoldingDecisionService(
        _PROVIDERS,
        _CFG,
        investment_thesis_service=InvestmentThesisService(store_dir=store_dir),
        runtime_config_service=HoldingDecisionRuntimeConfigService(store_dir=store_dir),
        audit_service=AuditService(AuditLogRepository(store_dir)),
    )


def _evaluate_twice(
    store_dir: Path, first: ShareholderBenefit | None, second: ShareholderBenefit | None
) -> tuple[InvestmentThesisScore, InvestmentThesisScore, Any]:
    """1回目(初回評価。baselineを作る)と、2回目(baselineと比較する)を、同じ保存先で評価する。"""
    service = _service(store_dir)
    holding = _holding()
    one = service.evaluate(
        holding, _NOW1, ExecutionPlanReason.NORMAL_SHADOW, snapshot=_snapshot(first, _NOW1)
    )
    two = service.evaluate(
        holding, _NOW2, ExecutionPlanReason.NORMAL_SHADOW, snapshot=_snapshot(second, _NOW2)
    )
    assert one.result is not None and two.result is not None
    return one.result.investment_thesis, two.result.investment_thesis, two.result


def test_service_baseline_with_benefit_then_registration_lost_is_data_missing(
    tmp_path: Path,
) -> None:
    first, second, _ = _evaluate_twice(tmp_path, _benefit(), None)

    assert _benefit_item(first).reason == _NOT_COMPARABLE  # 初回はbaselineを今作った = 比較不能
    assert _benefit_item(second).status == EvidenceCoverageStatus.NOT_EVALUATED
    assert _benefit_item(second).reason == _MISSING


def test_service_baseline_without_benefit_and_no_registration_is_not_applicable(
    tmp_path: Path,
) -> None:
    _, second, _ = _evaluate_twice(tmp_path, None, None)

    assert _benefit_item(second).status == EvidenceCoverageStatus.NOT_APPLICABLE


def test_service_baseline_with_benefit_maintained_is_full_points(tmp_path: Path) -> None:
    _, second, _ = _evaluate_twice(tmp_path, _benefit(), _benefit())

    item = _benefit_item(second)
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == _WEIGHTS.benefit_condition


def test_service_baseline_with_benefit_downgraded_is_zero_points(tmp_path: Path) -> None:
    _, second, _ = _evaluate_twice(tmp_path, _benefit(), _benefit(downgraded=True))

    item = _benefit_item(second)
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0


def test_service_abolished_is_evaluated_zero_points_since_issue_476(tmp_path: Path) -> None:
    _, second, _ = _evaluate_twice(tmp_path, _benefit(), _benefit(abolished=True))

    item = _benefit_item(second)
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0


def test_service_first_evaluation_is_not_comparable_and_uses_the_baseline_just_created(
    tmp_path: Path,
) -> None:
    first, _, _ = _evaluate_twice(tmp_path, _benefit(), _benefit())

    assert _benefit_item(first).status == EvidenceCoverageStatus.NOT_EVALUATED
    assert _benefit_item(first).reason == _NOT_COMPARABLE


def test_service_data_missing_changes_only_the_investment_thesis_coverage(
    tmp_path: Path,
) -> None:
    """★ データ欠落でも、スコア・リスク減点は変わらない。変わるのは投資ストーリーのcoverageだけ。"""
    maintained_dir = tmp_path / "maintained"
    missing_dir = tmp_path / "missing"
    maintained_dir.mkdir()
    missing_dir.mkdir()
    _, kept, kept_result = _evaluate_twice(maintained_dir, _benefit(), _benefit())
    _, lost, lost_result = _evaluate_twice(missing_dir, _benefit(), None)

    assert lost.score == pytest.approx(kept.score)
    assert lost.coverage_ratio < kept.coverage_ratio
    assert lost_result.risk_deduction == kept_result.risk_deduction
    assert lost_result.company_quality == kept_result.company_quality


def test_baseline_value_snapshot_type_allows_none_and_is_not_changed() -> None:
    """`has_shareholder_benefit`の型(bool | None)は変えない(保存形式は不変)。"""
    assert BaselineValueSnapshot().has_shareholder_benefit is None

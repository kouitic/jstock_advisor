"""投資ストーリー維持スコアのテスト(実装プラン3節・20節)。

配当維持/減配、優待維持/廃止、総合利回り、個別購入理由の未登録=NOT_APPLICABLE、
鮮度2段階を検証する。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus, ThesisConditionAttestationStatus
from jstock_advisor.domain.entities.holding_decision import (
    CustomThesisCondition,
    InvestmentThesis,
    ThesisConditionAttestation,
)
from jstock_advisor.domain.signals.investment_thesis_scoring import (
    InvestmentThesisInputs,
    score_investment_thesis,
)

_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.investment_thesis_weights
_TEMPLATE = _CFG.investment_thesis_template
_FRESH_WITHIN_DAYS = _CFG.holding_decision.fresh_within_days
_STALE_AFTER_DAYS = _CFG.holding_decision.stale_after_days
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)


def _inputs(**overrides) -> InvestmentThesisInputs:
    base = dict(
        current_total_yield_pct=_TEMPLATE.min_total_yield_pct,
        has_shareholder_benefit=True,
        benefit_abolished_or_downgraded=False,
        dividend_cut_or_omission_confirmed=False,
        profit_cf_premise_broken=False,
        financial_premise_broken=False,
        thesis=None,
    )
    base.update(overrides)
    return InvestmentThesisInputs(**base)


def _score(**overrides):
    return score_investment_thesis(
        _inputs(**overrides), _WEIGHTS, _TEMPLATE, _FRESH_WITHIN_DAYS, _STALE_AFTER_DAYS, _NOW
    )


def _item(result, code: str):
    return next(i for i in result.items if i.item_code == code)


def test_dividend_maintained_gives_full_points():
    result = _score(dividend_cut_or_omission_confirmed=False)
    item = _item(result, "dividend_policy")
    assert item.points_earned == _WEIGHTS.dividend_policy


def test_dividend_cut_gives_zero_points():
    result = _score(dividend_cut_or_omission_confirmed=True)
    item = _item(result, "dividend_policy")
    assert item.points_earned == 0.0
    assert item.status == EvidenceCoverageStatus.EVALUATED


def test_total_yield_at_minimum_gives_full_points():
    result = _score(current_total_yield_pct=_TEMPLATE.min_total_yield_pct)
    item = _item(result, "total_yield")
    assert item.points_earned == _WEIGHTS.total_yield


def test_total_yield_half_of_minimum_gives_half_points():
    result = _score(current_total_yield_pct=_TEMPLATE.min_total_yield_pct / 2)
    item = _item(result, "total_yield")
    assert abs(item.points_earned - _WEIGHTS.total_yield * 0.5) < 0.001


def test_total_yield_none_is_not_evaluated():
    result = _score(current_total_yield_pct=None)
    item = _item(result, "total_yield")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_benefit_condition_not_applicable_when_no_shareholder_benefit():
    result = _score(has_shareholder_benefit=False, benefit_abolished_or_downgraded=None)
    item = _item(result, "benefit_condition")
    assert item.status == EvidenceCoverageStatus.NOT_APPLICABLE


def test_benefit_condition_abolished_gives_zero_points():
    result = _score(has_shareholder_benefit=True, benefit_abolished_or_downgraded=True)
    item = _item(result, "benefit_condition")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0


def test_benefit_condition_not_evaluated_when_baseline_unavailable():
    result = _score(has_shareholder_benefit=True, benefit_abolished_or_downgraded=None)
    item = _item(result, "benefit_condition")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_custom_conditions_not_applicable_when_unregistered():
    result = _score(thesis=None)
    item = _item(result, "custom_conditions")
    assert item.status == EvidenceCoverageStatus.NOT_APPLICABLE
    # NOT_APPLICABLEは分母から除外されるため、他5軸だけで正規化される。
    assert item.weight not in (0.0,)


def test_custom_conditions_scored_by_attestation_ratio():
    conditions = [
        CustomThesisCondition(
            condition_id="c1",
            description="海外事業成長",
            registered_at=_NOW - dt.timedelta(days=400),
            last_attestation=ThesisConditionAttestation(
                status=ThesisConditionAttestationStatus.MAINTAINED,
                attested_at=_NOW - dt.timedelta(days=10),
                attested_by="kouichi",
            ),
        ),
        CustomThesisCondition(
            condition_id="c2",
            description="自社株買い方針",
            registered_at=_NOW - dt.timedelta(days=400),
            last_attestation=ThesisConditionAttestation(
                status=ThesisConditionAttestationStatus.BROKEN,
                attested_at=_NOW - dt.timedelta(days=10),
                attested_by="kouichi",
            ),
        ),
    ]
    thesis = InvestmentThesis(
        investment_thesis_id="t1",
        holding_id="7203",
        stock_code="7203",
        conditions=conditions,
        updated_at=_NOW,
    )
    result = _score(thesis=thesis)
    item = _item(result, "custom_conditions")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert abs(item.points_earned - _WEIGHTS.custom_conditions * 0.5) < 0.001


def test_custom_conditions_not_evaluated_when_no_usable_attestation():
    condition = CustomThesisCondition(
        condition_id="c1", description="事業ポートフォリオ", registered_at=_NOW, last_attestation=None
    )
    thesis = InvestmentThesis(
        investment_thesis_id="t1", holding_id="7203", stock_code="7203",
        conditions=[condition], updated_at=_NOW,
    )
    result = _score(thesis=thesis)
    item = _item(result, "custom_conditions")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_custom_conditions_ignores_attestation_beyond_stale_after_days():
    condition = CustomThesisCondition(
        condition_id="c1",
        description="特定ブランド成長",
        registered_at=_NOW - dt.timedelta(days=1000),
        last_attestation=ThesisConditionAttestation(
            status=ThesisConditionAttestationStatus.MAINTAINED,
            attested_at=_NOW - dt.timedelta(days=_STALE_AFTER_DAYS + 1),
            attested_by="kouichi",
        ),
    )
    thesis = InvestmentThesis(
        investment_thesis_id="t1", holding_id="7203", stock_code="7203",
        conditions=[condition], updated_at=_NOW,
    )
    result = _score(thesis=thesis)
    item = _item(result, "custom_conditions")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_custom_conditions_freshness_two_tier_flags_stale_but_still_scores():
    condition = CustomThesisCondition(
        condition_id="c1",
        description="事業ポートフォリオ",
        registered_at=_NOW - dt.timedelta(days=1000),
        last_attestation=ThesisConditionAttestation(
            status=ThesisConditionAttestationStatus.MAINTAINED,
            attested_at=_NOW - dt.timedelta(days=_FRESH_WITHIN_DAYS + 1),
            attested_by="kouichi",
        ),
    )
    thesis = InvestmentThesis(
        investment_thesis_id="t1", holding_id="7203", stock_code="7203",
        conditions=[condition], updated_at=_NOW,
    )
    result = _score(thesis=thesis)
    item = _item(result, "custom_conditions")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == _WEIGHTS.custom_conditions
    assert item.reason == "STALE_ATTESTATION_PRESENT"


def test_all_items_maintained_gives_full_fifty_points():
    result = _score()
    assert abs(result.score - 50.0) < 0.01
    assert result.coverage_ratio == 1.0

"""HoldingDecisionService.evaluate()という公開entry pointを直接呼ぶ形で、
非日常経路(初回評価・財務データ欠損)に関する既存Issueの結論を統合レベルで
固定する(Issue #254)。

対象4経路のうち、経路2(非営業日/#166)は`HoldingDecisionService.evaluate()`の
call graphに業務日判定が一切存在せず(fresh確認済み。#166の修正は
`WatchStateService`という別subsystemに閉じている)到達不能なため、
本Issueのscopeから除外した(issuecomment参照)。経路4(上限価格が使えない
場合/#221)は#467が既に所有・完了済みのため、本Issueには含めない
(#254 issuecomment参照)。

各テストは、対応するIssueの修正が既にmainへ入っている前提で、
「もしこの修正が無ければ落ちていたはずの不変条件」をevaluate()経由で
固定する(反証は各Issue自身のPRが当時行っている)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ExecutionPlanReason
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.services import holding_decision_service as holding_decision_service_module
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


def _holding(stock_code: str = "2914") -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_first_evaluation_matches_second_via_evaluate() -> None:
    """なぜこの経路が危ないか(#249): investment_thesisの評価は、baseline
    比較不能な項目(profit_cf_premise/financial_premise)を初回だけ
    NOT_EVALUATEDにする。分母から除く修正が無ければ、新規保有の初日だけ
    final_scoreが18.75点低く出て、判定区分(category)を誤って引き下げ、
    本来出るべきでない売り警告に繋がりうる(#249実測)。

    tests/conftest.pyの自動fixtureにより、本テスト関数は空のstoreから
    始まる(1回目の`evaluate()`呼び出しが本物の「初回評価」になる)。
    """
    service = HoldingDecisionService(_PROVIDERS, _CFG)
    holding = _holding()

    outcome_1 = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)
    outcome_2 = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)

    assert outcome_1.result is not None
    assert outcome_2.result is not None
    # #249の核心: 同じ状態なら初回でも2回目以降と同じスコアになる(対称)。
    assert outcome_1.result.investment_thesis.score == outcome_2.result.investment_thesis.score
    assert outcome_1.result.final_score == outcome_2.result.final_score
    assert outcome_1.result.category == outcome_2.result.category


def test_coverage_ratio_still_drops_on_first_evaluation_via_evaluate() -> None:
    """#249の逆側: スコアは初回・2回目で一致するが、「評価できなかった」という
    事実自体はcoverage_ratioに残ることを、evaluate()経由でも確認する(スコアの
    対称化とcoverageの低下は両立する。#249が示した区別をevaluate()レベルでも
    崩さないこと)。
    """
    service = HoldingDecisionService(_PROVIDERS, _CFG)
    holding = _holding()

    outcome_1 = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)
    outcome_2 = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)

    assert outcome_1.result is not None
    assert outcome_2.result is not None
    assert (
        outcome_1.result.investment_thesis.coverage_ratio
        < outcome_2.result.investment_thesis.coverage_ratio
    )


def test_missing_financial_data_does_not_silently_substitute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """なぜこの経路が危ないか(#52系。データ鮮度・欠損semanticsの分離): 価格・
    財務データが取得できない場合、evaluate()が黙って何らかの既定値
    (0点・中立スコア等)で判定を継続すると、データ欠損が「正常に評価した
    結果」へロンダリングされ、利用者が気づけない。evaluate()は
    `build_stock_snapshot()`がエラーを返した場合、`data_error`付きの
    `HoldingDecisionEvaluationOutcome`(`result=None`)を返すのみで、
    架空のHoldingDecisionResultを一切生成しないことを固定する。
    """
    service = HoldingDecisionService(_PROVIDERS, _CFG)
    holding = _holding()

    monkeypatch.setattr(
        holding_decision_service_module,
        "build_stock_snapshot",
        lambda *args, **kwargs: (None, "price_and_financial_data_unavailable"),
    )

    outcome = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)

    assert outcome.result is None
    assert outcome.data_error == "price_and_financial_data_unavailable"

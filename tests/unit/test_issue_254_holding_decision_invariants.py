"""HoldingDecisionService.evaluate()という公開entry pointを直接呼ぶ形で、
非日常経路(初回評価・財務データ欠損)に関する既存Issueの結論を統合レベルで
固定する(Issue #254)。

対象4経路のうち、経路2(非営業日/#166)は本Issueの直接テスト対象から
除外した(USER承認済み。#254 issuecomment参照)。除外理由は次のとおりで
あり、「evaluate()に業務日暦の影響が一切存在しない」という一般化では
ない(サブちゃんレビュー指摘。fresh確認の結果、`evaluate()`→
`build_stock_snapshot()`→`BusinessCalendar`/`business_days_between()`
という経路が実在し、業務日暦・祝日暦がevaluate()の結果〔price_as_of・
business_days_to_earnings等〕へ実際に影響しうることを確認済み)。

```
#166が実際に修正した不変条件は WatchStateService の
  near_buy_consecutive_business_days(非営業日実行・同日再実行時の
  非加算)であり、evaluate()からは到達不能である
その不変条件は#166自身の専用テストが既に固定しており、#166自体は
  既にstatus:本番検証済・CLOSEDである
よって「#166の修正をevaluate()経由で統合レベルに再固定する」という
  意味では、本Issueの直接テスト対象にはならない
一方、evaluate()経由で業務日暦が一切影響しないと主張するものではなく、
  その一般的な影響の網羅性は本Issueのscope外(別途確認する)
```

経路4(上限価格が使えない場合/#221)は#467が既に所有・完了済みのため、
本Issueには含めない(#254 issuecomment参照)。

各テストは、対応するIssueの修正が既にmainへ入っている前提で、
「もしこの修正が無ければ落ちていたはずの不変条件」をevaluate()経由で
固定する(反証は各Issue自身のPRが当時行っている)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ExecutionPlanReason
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.services import holding_decision_service as holding_decision_service_module
from jstock_advisor.services.audit_service import AuditService
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
    tmp_path: Path,
) -> None:
    """なぜこの経路が危ないか(#52系。データ鮮度・欠損semanticsの分離): 価格・
    財務データが取得できない場合、evaluate()が黙って何らかの既定値
    (0点・中立スコア等)で判定を継続すると、データ欠損が「正常に評価した
    結果」へロンダリングされ、利用者が気づけない。evaluate()は
    `build_stock_snapshot()`がエラーを返した場合、`data_error`付きの
    `HoldingDecisionEvaluationOutcome`(`result=None`)を返すのみで、
    架空のHoldingDecisionResultを一切生成しないことを固定する。

    サブちゃんレビュー対応(PR #687 F2): 戻り値のassertだけでは
    `self._audit.record(...)`呼び出し自体を無効化する変異を検出できな
    かった(175件中0件検出)。データ欠損の事実は監査記録へも残ることを
    別途確認する(「架空の結果を作らない」ことと「欠損の事実を記録する」
    ことは別の不変条件であり、両方を固定する)。
    """
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    service = HoldingDecisionService(_PROVIDERS, _CFG, audit_service=AuditService(audit_repo))
    holding = _holding()

    monkeypatch.setattr(
        holding_decision_service_module,
        "build_stock_snapshot",
        lambda *args, **kwargs: (None, "price_and_financial_data_unavailable"),
    )

    outcome = service.evaluate(holding, _NOW, ExecutionPlanReason.NORMAL_SHADOW)

    assert outcome.result is None
    assert outcome.data_error == "price_and_financial_data_unavailable"

    audit_entries = audit_repo.list_all()
    assert len(audit_entries) == 1
    assert audit_entries[0].decision_type == "holding_decision"
    assert audit_entries[0].output_values.get("data_error") == (
        "price_and_financial_data_unavailable"
    )

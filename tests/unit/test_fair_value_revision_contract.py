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
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    FairValueInput,
    IncomeInput,
    ReasonCode,
    compute_expected_return,
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
    """ReasonCode に Revision 用の理由を足さない(接続は別 PR。PR-1b の adapter の全件対応を壊さない)。"""
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

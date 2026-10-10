"""Expected Return の型と UPSIDE / INCOME の純粋計算の契約テスト(Issue #601 PR-1a)。

## この PR の範囲(USER 承認: #122 issuecomment-6100082651 の 3 節)
  Expected Return の型・UPSIDE / INCOME の純粋計算・入力欠測時の状態と理由コード。
  ★ composite の合成式・年率換算・Fair Value Revision の規則・重み・閾値は確定しない
  (UNDETERMINED で表現する)。既存の ExpectedReturnVerdict への写像は PR-1b(exit_architecture 側)。

## 固定するもの
  (0) 先行(characterization): INCOME が使う総合利回りの真理値表(#55)は変更しない
  (1) UPSIDE = (Fair Value〔中立〕/ 現在価格 - 1) × 100。現在価格基準。境界と符号
  (2) 欠測・不適格 -> 値を作らず理由コード(現在価格・鮮度・Fair Value・使用可否)。理由は累積
  (3) INCOME は #55 の真理値表(A〜H)に従う。None から 0 を推測しない
  (4) composite・annualized・revision は常に UNDETERMINED(全入力の格子で。値を捏造しない)
  (5) 構造: 取得価格・available_cash・clock を引数に持たない / 時計を読まない /
      誰からも import されない(dormant)

時間意味論: evaluation_date は呼び出し側が渡す記録用の値で、時計・営業日・timezone を読まない
(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import pytest

from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState, compute_total_yield_pct

# --- (0) 先行: INCOME が使う既存の真理値表は変更しない(#55) -----------------------------------

_NO = BenefitProgramState.NO_PROGRAM
_VALUED = BenefitProgramState.VALUED
_UNVALUABLE = BenefitProgramState.UNVALUABLE

TRUTH_TABLE = [
    # (名前, 配当%, 優待%, 優待の状態, 期待する総合利回り)
    ("A 配当既知 + 制度なし", 2.0, None, _NO, 2.0),
    ("B 配当既知 + 評価可能", 2.0, 1.5, _VALUED, 3.5),
    ("C 配当既知 + 評価不能", 2.0, None, _UNVALUABLE, None),
    ("D 配当不明 + 制度なし", None, None, _NO, None),
    ("E 配当不明 + 評価可能", None, 1.5, _VALUED, None),
    ("F 配当不明 + 評価不能", None, None, _UNVALUABLE, None),
    ("G 配当 0(明示)+ 制度なし", 0.0, None, _NO, 0.0),
    ("H 配当 0(明示)+ 評価可能", 0.0, 1.5, _VALUED, 1.5),
]


@pytest.mark.parametrize(("name", "dividend", "benefit", "state", "expected"), TRUTH_TABLE)
def test_characterization_total_yield_truth_table_is_unchanged(
    name: str, dividend: float | None, benefit: float | None, state: BenefitProgramState, expected
) -> None:
    assert compute_total_yield_pct(dividend, benefit, benefit_state=state) == expected, name

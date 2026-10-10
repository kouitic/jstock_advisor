"""hard gate の理由コードごとの『確認状態』の分類(Issue #890 PR-2)。

再通知の条件 R3(新たに確認済みの hard gate)が使う確認状態(`GateConfirmation`)を、保有判断の
評価が既に持っている根拠から導く純粋関数。**hard gate の発動の条件そのものは変えない**
(#888 / #889 の範囲)。ここは『発動した理由コードが、どの程度確認されたものか』を分類するだけ。

分類の規則(理由コード → 確認状態)
  BANKRUPTCY_FILING / DELISTING_OR_KANRI / ACCOUNTING_FRAUD
      開示キーワード由来。評価の根拠に載る確認の段階が『重大事象を確認(MATERIAL_EVENT_CONFIRMED)』
      なら CONFIRMED、『キーワード一致のみ(RISK_KEYWORD_DETECTED)』なら KEYWORD_ONLY。
      段階が読めない(根拠が無い・未知の値)ときも KEYWORD_ONLY(キーワード一致だけで『確認済み』に
      しない: USER 指定)
  DEBT_EXCESS / DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS
      評価の根拠が一次情報の確認(primary_source_confirmed)を要件にして発動するため CONFIRMED
  GOING_CONCERN_DOUBT
      財務データの判定フラグ(FinancialSummary.is_going_concern_doubt)をそのまま入力にしており、
      一次情報の確認を経ていない(現行の無料 provider は判定不可として常に False)。出所が
      確認できない旗として UNVERIFIED(数えない側)
  INVESTMENT_THESIS_COLLAPSE
      人が承認した baseline と投資ストーリーの点による(外部の証拠ではない)ため BASELINE_CONFIRMED
  上記にない理由コード
      UNVERIFIED(出所が確認できない。数えない側に倒す)

性質: 純粋・決定的・例外を出さない(未知の入力は UNVERIFIED / KEYWORD_ONLY)。出力は理由コードの昇順。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from jstock_advisor.domain.signals.holding_decision_renotification import GateConfirmation

#: 開示の確認の段階が評価の根拠に載る rule(hard gate の理由コード -> sell_signal の rule 名)。
DISCLOSURE_RULE_BY_REASON_CODE: dict[str, str] = {
    "BANKRUPTCY_FILING": "major_scandal",
    "DELISTING_OR_KANRI": "listing_maintenance_risk",
    "ACCOUNTING_FRAUD": "accounting_problem",
}

#: 開示の確認の段階を表す値(sell_signal の DisclosureRiskConfirmationLevel の値)。
MATERIAL_EVENT_CONFIRMED = "MATERIAL_EVENT_CONFIRMED"

#: 評価の根拠(SellRuleEvaluation)の metric_name のうち、開示の確認の段階を載せるもの。
DISCLOSURE_LEVEL_METRIC_NAME = "disclosure_risk_confirmation_level"


def disclosure_levels(evaluations: Mapping[str, Any]) -> dict[str, str | None]:
    """評価の根拠(rule 名 -> SellRuleEvaluation)から、開示の確認の段階を取り出す。

    開示由来の 3 rule のうち、根拠に確認の段階(metric_name が上の値)が載っているものだけを
    返す。型を import せず、属性(metric_name / current_value)で読む(sell_signal への依存を
    増やさない)。載っていない rule は返さない(= 段階が読めない = KEYWORD_ONLY に倒れる)。
    """
    levels: dict[str, str | None] = {}
    for rule_name in DISCLOSURE_RULE_BY_REASON_CODE.values():
        evaluation = evaluations.get(rule_name)
        if evaluation is None:
            continue
        if getattr(evaluation, "metric_name", None) != DISCLOSURE_LEVEL_METRIC_NAME:
            continue
        value = getattr(evaluation, "current_value", None)
        levels[rule_name] = value if isinstance(value, str) else None
    return levels


CONFIRMED_BY_PRIMARY_SOURCE_CODES = frozenset(
    {"DEBT_EXCESS", "DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS"}
)
BASELINE_CONFIRMED_CODES = frozenset({"INVESTMENT_THESIS_COLLAPSE"})
#: 一次情報の確認を経ない旗(出所が確認できない)。明示の規則として UNVERIFIED にする。
UNVERIFIED_FLAG_CODES = frozenset({"GOING_CONCERN_DOUBT"})


def classify_gate_confirmations(
    reason_codes: Iterable[str],
    disclosure_levels: Mapping[str, str | None],
) -> tuple[tuple[str, str], ...]:
    """発動した hard gate の理由コードごとに、確認状態(GateConfirmation の値)を返す。

    disclosure_levels は rule 名(major_scandal 等)-> 確認の段階の値(MATERIAL_EVENT_CONFIRMED /
    RISK_KEYWORD_DETECTED / None)。理由コードは重複を除き昇順で返す。
    """
    out: list[tuple[str, str]] = []
    for code in sorted(set(reason_codes)):
        if code in DISCLOSURE_RULE_BY_REASON_CODE:
            level = disclosure_levels.get(DISCLOSURE_RULE_BY_REASON_CODE[code])
            confirmation = (
                GateConfirmation.CONFIRMED
                if level == MATERIAL_EVENT_CONFIRMED
                else GateConfirmation.KEYWORD_ONLY
            )
        elif code in CONFIRMED_BY_PRIMARY_SOURCE_CODES:
            confirmation = GateConfirmation.CONFIRMED
        elif code in BASELINE_CONFIRMED_CODES:
            confirmation = GateConfirmation.BASELINE_CONFIRMED
        else:
            confirmation = GateConfirmation.UNVERIFIED
        out.append((code, confirmation.value))
    return tuple(out)

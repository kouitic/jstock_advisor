"""財務データの報告サイクル鮮度を判定経路へ接続するための共通処理(Issue #52 B3-B2)。

SELLと利確は同じ意味で財務鮮度を扱う必要がある(同じ猶予日数・同じ入力・同じ
監査項目)。両サービスへ同じコードを書き写すと、片方だけ直して意味がずれる事故が
起きるため、接続部分をここへ集約する。判定そのものは
`domain/financial_freshness.py` にあり、本moduleはその呼び出し方を固定するだけ
である(判定ロジックを持たない)。

BUY(Phase B3-B1)は共通confidence scoreを持たず警告のみという別の扱いのため、
本moduleを使わない(buy_signal_service.pyは変更していない)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.financial_freshness import (
    FinancialFreshnessResult,
    FinancialFreshnessVerdict,
    evaluate_financial_freshness,
)
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.earnings_window import resolve_latest_financial_period_end
from jstock_advisor.interfaces.types import FinancialSummary

# 利用者へ見せる文言。減点理由・HIGH禁止理由(confidence_scoring.py)と同じ事実を
# 指すため、表現を揃える。「取得が古い」ではなく「発表されているはずの期の数字が
# 入っていない」ことを示す。
FINANCIAL_STALE_USER_WARNING = "最新の決算が財務データへ反映されていない可能性がある"


class FinancialFreshnessAssessment:
    """判定結果と、その判定に実際に使った入力をまとめて持つ。

    監査へ「判定に使った値そのもの」を残せるようにするため、解決済みの期間末も
    保持する(表示のために再計算しない)。
    """

    __slots__ = ("latest_financial_period_end", "result")

    def __init__(
        self, result: FinancialFreshnessResult, latest_financial_period_end: dt.date | None
    ) -> None:
        self.result = result
        self.latest_financial_period_end = latest_financial_period_end

    @property
    def is_stale(self) -> bool:
        """報告期限を過ぎても旧期のままか。

        UNKNOWNはFalseとする。「古い」ことを確認できたわけではないため、
        減点もHIGH禁止も警告も行わない(観測項目としてのみ残す)。
        """
        return self.result.verdict is FinancialFreshnessVerdict.STALE

    def audit_values(self, config: AppConfig) -> dict[str, object]:
        """判定時点の入力・出力をそのまま監査へ残す(事後に再検証できるようにする)。"""
        stale = self.is_stale
        return {
            "financial_freshness_verdict": self.result.verdict.value,
            "financial_freshness_basis": self.result.basis.value,
            "financial_freshness_reason": self.result.reason,
            "latest_financial_period_end": (
                self.latest_financial_period_end.isoformat()
                if self.latest_financial_period_end is not None
                else None
            ),
            "expected_next_financial_period_end": (
                self.result.expected_next_period_end.isoformat()
                if self.result.expected_next_period_end is not None
                else None
            ),
            "expected_financial_report_deadline": (
                self.result.expected_report_deadline.isoformat()
                if self.result.expected_report_deadline is not None
                else None
            ),
            "financial_reporting_lag_calendar_days": (
                config.screening.data_quality.financial_reporting_lag_calendar_days
            ),
            "financial_freshness_warning": stale,
            "financial_stale_confidence_penalty_applied": stale,
            "financial_stale_high_confidence_disallowed": stale,
        }


def assess_financial_freshness(
    financial: FinancialSummary,
    now: dt.datetime,
    config: AppConfig,
    *,
    latest_financial_period_end: dt.date | None = None,
    evaluation_date: dt.date | None = None,
) -> FinancialFreshnessAssessment:
    """財務データが報告サイクル上の最新かを判定する。

    取得時刻(`data_fetched_at`)は使わない。無料providerは取得の都度いまの時刻を
    入れるため、取得時刻を見ても「決算発表後なのに旧期のまま」は検知できない。
    これがIssue #52の根本原因であり、ここで再び持ち込まない。

    期間末の解決は既存の`resolve_latest_financial_period_end()`を再利用する。
    呼び出し側が既に解決済みの値を持っている場合はそれを渡すことで、同じ計算を
    二度行わない(判定に使う値を1つに保つ)。provider再取得は行わない。
    """
    resolved_evaluation_date = (
        evaluation_date if evaluation_date is not None else evaluation_date_jst(now)
    )
    period_end = (
        latest_financial_period_end
        if latest_financial_period_end is not None
        else resolve_latest_financial_period_end(financial, resolved_evaluation_date).period_end
    )
    result = evaluate_financial_freshness(
        latest_financial_period_end=period_end,
        quarter_ends=tuple(q.quarter_end for q in financial.recent_quarters),
        recent_periods_source=financial.recent_periods_source,
        fiscal_year_end_month=financial.fiscal_year_end_month,
        evaluation_date=resolved_evaluation_date,
        reporting_lag_days=config.screening.data_quality.financial_reporting_lag_calendar_days,
    )
    return FinancialFreshnessAssessment(result, period_end)

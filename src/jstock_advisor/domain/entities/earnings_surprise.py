"""判定精度向上機能Phase C: Earnings Surprise Score(決算サプライズスコア)の
評価結果スナップショット。

実装前調査(調査結果は計画書参照)により、当初検討した4要素
(Analyst Consensus Surprise / Historical Progress Surprise / Guidance
Revision / Dividend Surprise)のうち、以下の理由でAnalyst Consensus Surprise
のみを対象とする。

- Historical Progress Surprise(今回の進捗率 vs 過去3〜5年の同一四半期平均
  進捗率)は、現在のProvider(yfinance quarterly_income_stmt)が直近5四半期
  (約1.25年)分しか四半期実績を提供できず、3〜5年分(12〜20四半期)という
  当初定義を満たすデータが根本的に存在しないため実装しない。
- Guidance Revision(会社予想の上方修正/下方修正)は、この開示情報自体が
  TDnet専用であり、本システムの開示Provider(EDINET臨時報告書のみ対応)
  からは構造的に取得できないため実装しない。
- Dividend Surprise/Revision(コードレビュー対応でv2にて除外): 既存の
  DividendComparisonOutcomeは「前年度の完了済み年間配当実績 vs 現在の
  年間予想配当」の比較結果であり、「今回の決算で配当予想が上方修正/
  下方修正されたか」という意味のデータではない。これを今回の決算に対する
  サプライズとして扱うのは意味が異なるため、Earnings Surprise Scoreからは
  除外した(配当方向としての評価はEarnings Trend Score(domain/entities/
  earnings_trend.py)のdividend_directionへ引き続き残す。こちらは「配当の
  方向性」という意味であり整合する)。

取得できない要素を代替実装で埋めることはせず、実現可能な要素のみで
v2を構成する(取得できるデータに合わせてEarnings Surpriseの意味を
変えない、という方針)。

Analyst Consensus Surpriseは、Yahoo Financeの決算サプライズ履歴
(EarningsSurpriseRecord、interfaces/types.py参照)を用いるが、この
データはライブ評価時点で取得・保存した値をその後のDecisionOutcome分析に
使うことのみ安全であり(LIVE_SHADOW_ONLY)、過去の評価日を指定して
再構成する用途(バックテスト)には使えない(既知の制約)。そのため
コードレビュー対応(v2)として、判定当時に実際に取得・使用した生値
(突合した四半期末日・実績EPS・コンセンサス予想EPS・データ出所等)を
本Result自体に保持し、後から入力を再現できるようにした。

HistoricalValuationResult/TimingScoreResultと同じ設計: 単なる
`float | None`ではなく、後から「なぜこの点数だったか」を再現・検証できる
よう、score/confidence/coverage/内訳(成分別スコア)を構造化して保持する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
)


class EarningsSurpriseResult(ImmutableSnapshot):
    state: EarningsSurpriseEvaluationState
    score: float | None = None
    category: EarningsSurpriseCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # 成分別スコア(-100〜+100、算出不可の場合はNone)。v2でAnalyst Consensus
    # Surpriseのみの単一成分構成となった(dividend_revision_componentは
    # 意味の異なるデータを流用していたため削除、モジュールdocstring参照)。
    analyst_consensus_component: float | None = None

    # コードレビュー対応(v2): LIVE_SHADOW_ONLYのため、判定当時に実際に
    # 取得・使用した生値を保持する(後から入力を再現できるようにするため)。
    # matched_*系は実際にearnings_surprise_historyとresolved_period_endを
    # 突合できた場合のみ設定される(未突合ならNoneのまま)。
    matched_quarter_end: dt.date | None = None
    resolved_financial_period_end: dt.date | None = None
    eps_actual: Decimal | None = None
    eps_estimate: Decimal | None = None
    surprise_pct: float | None = None
    earnings_surprise_source_provider: str | None = None
    earnings_surprise_source_fetched_at: dt.datetime | None = None
    # 評価時点で参照したEarningsReleaseConfirmationState(なぜNOT_APPLICABLE
    # だったか等を後から確認できるようにするための監査情報)。
    release_confirmation_state: EarningsReleaseConfirmationState | None = None
    # コードレビュー対応(v3): 古い決算予定日が現在の判断にまだ関連するか
    # (resolve_earnings_decision_relevance()の戻り値、domain/signals/
    # earnings_window.py参照)。何か月も前の過去日でNOT_APPLICABLEを無期限に
    # 継続しないための判定に使った値を後から確認できるようにする。
    earnings_decision_relevance: EarningsDecisionRelevance | None = None

    # 評価全体に関する注記コード(例: "ANALYST_CONSENSUS_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

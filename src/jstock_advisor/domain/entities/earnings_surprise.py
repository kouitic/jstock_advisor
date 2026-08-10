"""判定精度向上機能Phase C: Earnings Surprise Score(決算サプライズスコア)の
評価結果スナップショット。

実装前調査(調査結果は計画書参照)により、当初検討した4要素
(Analyst Consensus Surprise / Historical Progress Surprise / Guidance
Revision / Dividend Surprise)のうち、以下の理由でAnalyst Consensus Surprise
とDividend Surprise/Revisionの2要素のみを対象とする。

- Historical Progress Surprise(今回の進捗率 vs 過去3〜5年の同一四半期平均
  進捗率)は、現在のProvider(yfinance quarterly_income_stmt)が直近5四半期
  (約1.25年)分しか四半期実績を提供できず、3〜5年分(12〜20四半期)という
  当初定義を満たすデータが根本的に存在しないため実装しない。
- Guidance Revision(会社予想の上方修正/下方修正)は、この開示情報自体が
  TDnet専用であり、本システムの開示Provider(EDINET臨時報告書のみ対応)
  からは構造的に取得できないため実装しない。

取得できない要素を代替実装で埋めることはせず、実現可能な2要素のみで
v1を構成する(取得できるデータに合わせてEarnings Surpriseの意味を
変えない、という方針)。

Analyst Consensus Surpriseは、Yahoo Financeの決算サプライズ履歴
(EarningsSurpriseRecord、interfaces/types.py参照)を用いるが、この
データはライブ評価時点で取得・保存した値をその後のDecisionOutcome分析に
使うことのみ安全であり(LIVE_SHADOW_ONLY)、過去の評価日を指定して
再構成する用途(バックテスト)には使えない(既知の制約)。

HistoricalValuationResult/TimingScoreResultと同じ設計: 単なる
`float | None`ではなく、後から「なぜこの点数だったか」を再現・検証できる
よう、score/confidence/coverage/内訳(成分別スコア)を構造化して保持する。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
)


class EarningsSurpriseResult(ImmutableSnapshot):
    state: EarningsSurpriseEvaluationState
    score: float | None = None
    category: EarningsSurpriseCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # 成分別スコア(-100〜+100、算出不可の成分はNone)。
    analyst_consensus_component: float | None = None
    dividend_revision_component: float | None = None

    # 評価全体に関する注記コード(例: "ANALYST_CONSENSUS_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

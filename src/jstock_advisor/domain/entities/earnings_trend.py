"""判定精度向上機能Phase C: Earnings Trend Score(業績トレンドスコア)の
評価結果スナップショット。

Earnings Surprise Score(earnings_surprise.py)とは完全に独立した評価軸
であり、「直近の決算が期待に対して良かったか」ではなく「複数四半期に
わたって業績が改善傾向にあるか」を評価する。

実装前調査の結果、当初仕様の要素のうち以下は現在のProviderでは算出
できないため対象外とする(将来Provider拡張(売上・EPSの四半期抽出追加)
により対応可能になり次第、別途検討する)。

- 売上トレンド: QuarterlyFinancialsに売上高フィールドが無い(yfinance
  quarterly_income_stmtには"Total Revenue"行自体は存在するが、現行
  Providerは営業利益/営業CFのみ抽出している)。
- EPSトレンド: 同上("Diluted EPS"/"Basic EPS"行は存在するが未抽出)。
- 利益率改善: 売上高が無いため算出不可(売上トレンド対応に従属)。
- 会社予想方向(上方修正/下方修正の方向): forecast_epsは単一時点の
  スナップショットのみで、過去の予想値の履歴を保持する仕組みが無いため、
  「修正」の方向を判定できない。

今回は営業利益トレンド・営業CFトレンド・配当方向の3要素を中心に構成する。
いずれも既存の四半期実績(直近5四半期程度)・季節調整(TTM)ロジック
(domain/financial_series.py)や既存のDividendComparisonOutcomeを再利用する。

データ量が薄い(直近5四半期程度)ため、LEVEL(最新値)・DIRECTION(直近の
増減方向)までは無理なく算出できるが、ACCELERATION(増減の加速・減速)は
最低3四半期の差分比較が必要で信頼度が低い。そのためacceleration成分は
補助成分として軽い重みで扱う(config側で他成分より小さい重みを設定する)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
)


class EarningsTrendResult(ImmutableSnapshot):
    state: EarningsTrendEvaluationState
    score: float | None = None
    category: EarningsTrendCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # 成分別スコア(-100〜+100、算出不可の成分はNone)。
    operating_income_trend_component: float | None = None
    operating_cashflow_trend_component: float | None = None
    dividend_direction_component: float | None = None
    # 補助成分(直近5四半期程度の薄いデータのため信頼度が低く、他成分より
    # 軽い重みを持つ)。
    acceleration_component: float | None = None

    # 評価全体に関する注記コード(例: "OPERATING_INCOME_TREND_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

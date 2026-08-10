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

コードレビュー対応(v2): 変化率計算の符号跨ぎ(赤字・マイナスCF時の改善/
悪化逆転)バグを修正し、成分算出に使った生値(latest/previous・変化率・
四半期実績の由来)を監査用に保持するようにした
(domain/signals/earnings_trend.py参照)。

コードレビュー対応(v3): NOT_APPLICABLE判定へ既存のEarningsDecisionRelevance
を組み合わせ(古い決算予定日で無期限に評価停止しないため)、成分算出に
使った値がどの期間のものか(period_end/period_type)を監査情報として
追加保持するようにした。

コードレビュー対応(第3回): NOT_APPLICABLE判定に使うもう一方の入力である
release_confirmation_stateも(EarningsSurpriseResultと同様)監査情報として
保持するようにした。スコア算出式・NOT_APPLICABLE判定条件自体の変更は無く、
model_version(earnings_trend_v3)も据え置きとした(監査情報の追加のみで
スコアリング方式自体は変わっていないため)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
    PeriodType,
    RecentPeriodsSource,
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

    # コードレビュー対応(v2): LIVE_SHADOW_ONLYではないが、後から「なぜこの
    # 点数だったか」を再現できるよう、成分算出に使った生値(判定当時の四半期
    # 実績値そのもの)を保持する。算出不可の場合はNone。
    latest_operating_income: Decimal | None = None
    previous_operating_income: Decimal | None = None
    operating_income_change_pct: float | None = None
    latest_operating_cashflow: Decimal | None = None
    previous_operating_cashflow: Decimal | None = None
    operating_cashflow_change_pct: float | None = None
    # acceleration成分の2階差分の生値(%ポイント、クランプ前)。
    acceleration_raw_pct: float | None = None
    # コードレビュー対応(v2): 四半期実績由来か年次決算へのフォールバック
    # 由来か(FinancialSummary.recent_periods_source)。confidence算出に
    # 反映する(domain/signals/earnings_trend.py参照)。
    recent_periods_source: RecentPeriodsSource | None = None

    # コードレビュー対応(v3): 「その値がどの期間の値なのか」をvalueと
    # 分離せず後から復元できるよう、成分算出に使ったFinancialPeriodValueの
    # period_end/period_typeを保持する(index対応ではなく、値と期間の対応が
    # 直接分かる形にするため)。算出不可の場合はNone(evaluated_at等の
    # 現在日時で代替しない)。
    latest_operating_income_period_end: dt.date | None = None
    previous_operating_income_period_end: dt.date | None = None
    operating_income_period_type: PeriodType | None = None
    latest_operating_cashflow_period_end: dt.date | None = None
    previous_operating_cashflow_period_end: dt.date | None = None
    operating_cashflow_period_type: PeriodType | None = None
    # acceleration成分に使った3四半期分(prev2, prev1, curr)のperiod_end。
    # 営業利益系列由来(acceleration自体が営業利益系列のみを使うため)。
    acceleration_period_ends: tuple[dt.date, dt.date, dt.date] | None = None

    # コードレビュー対応(v3): 古い決算予定日が現在の判断にまだ関連するか
    # (resolve_earnings_decision_relevance()の戻り値、domain/signals/
    # earnings_window.py参照)。EarningsSurpriseResultと同じ監査目的。
    earnings_decision_relevance: EarningsDecisionRelevance | None = None
    # コードレビュー対応(第3回): NOT_APPLICABLE判定は
    # release_confirmation_state+earnings_decision_relevanceの組み合わせで
    # 決まるため、evaluate_earnings_trend()が実際に受け取った
    # release_confirmation_stateも(補完・再計算せず)そのまま保持する
    # (EarningsSurpriseResultと同じ2値セットを監査可能にする)。
    release_confirmation_state: EarningsReleaseConfirmationState | None = None

    # 評価全体に関する注記コード(例: "OPERATING_INCOME_TREND_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

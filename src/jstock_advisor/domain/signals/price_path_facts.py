"""価格の経路の事実(素の型)と、Shadow 評価用の暫定のトレンドの写像(Issue #877 PR-2b-i。dormant)。

利確判定(`ProfitTakingService.analyze`)が手元に持つ利益保全の指標(`ProfitProtectionMetrics`)と
トレンドの分類を、**素の型 5 つ**へ分解して運ぶ入れ物。L3 PRICE_REGIME の入力
(`exit_architecture.price_facts.build_price_facts`。PR-2a)と 1 対 1 に対応する。

## 置き場の理由(#606 の網)

★ 本 module は `exit_architecture` を import しない ★。`ProfitTakingService` は handler からも
CLI(`cli/analyze.py`)からも到達するため、サービスが `exit_architecture` を import すると CLI の根が
未接続の module へ到達してしまう(#606 の網は CLI の根に免除を適用しない)。サービスは本 module の
素の型を詰めるだけで、`exit_architecture` へ繋がる受け手は別の module(PR-2b-ii)に置く。
設計: #877 PR-2 設計 rev3(…6182483078)・設計レビュー PASS(…6182584823)。

★ 注意: 本 module は #606 の網の対象外(名前の token・prefix に当たらない)★。到達の保証は
#606 ではなく、
契約テスト(全 src の走査で import するのは利確判定サービスだけ・サービスの import の閉包に
exit_architecture が無い)である。`exit_architecture` の中に置くと token で網に掛かり、CLI の根が
到達して落ちるため、素の型の module として外に置いている。

## 守ること(契約テストで固定する)

* ★ 値を写すだけ ★: 指標の値(下落率・peak 時点の含み益率・現在の含み益率・データ不足か)を
  そのまま運ぶ。再計算・補完・丸め・範囲の検査をしない(検査と UNDETERMINED の扱いは受け手側 =
  PR-2a の `build_price_facts`)
* ★ DOWN の暫定写像 ★: DOWNTREND と STRONG_DOWNTREND を DOWN とする(USER 承認済みの Shadow 評価用の
  暫定仕様。#122 …6174293468 の 2)。★ Production の判定には使わない ★(判定側の module は
  この写像を import しない)
* 新しい数(閾値・日数・係数)を持たない。時計・I/O・乱数なし。判定側の module(利確・売却・購入の
  判定)は本 module を import しない(import するのは `ProfitTakingService` の値の詰めと
  契約テストだけ)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from jstock_advisor.domain.entities.enums import TrendClassification
from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics

#: 暫定写像の版(Shadow 評価用。変更は意図した変更として、契約テストと一緒に更新する)
TREND_IS_DOWN_MAP_VERSION: Final = "shadow-trend-map-v0"

#: トレンドの分類 → 『下落トレンドか』。★ Shadow 評価用の暫定仕様 = Production の判定には使わない ★
TREND_IS_DOWN: Final[Mapping[TrendClassification, bool]] = {
    TrendClassification.STRONG_UPTREND: False,
    TrendClassification.UPTREND: False,
    TrendClassification.NEUTRAL: False,
    TrendClassification.DOWNTREND: True,
    TrendClassification.STRONG_DOWNTREND: True,
}


@dataclass(frozen=True)
class PricePathFacts:
    """価格の経路の事実(素の型 5 つ)。`build_price_facts` の引数と同じ名前・同じ意味。

    data_sufficient          指標の `insufficient_data_reason` が None(データが足りている)
    drawdown_from_peak_pct   peak からの下落率(%)。データ不足なら None
    peak_gain_pct            peak 時点の含み益率(%)。データ不足なら None
    current_gain_pct         現在の含み益率(%)。データ不足なら None
    trend_is_down            トレンドが下落か(暫定写像)。分類が無ければ None
    """

    data_sufficient: bool
    drawdown_from_peak_pct: float | None
    peak_gain_pct: float | None
    current_gain_pct: float | None
    trend_is_down: bool | None


def trend_is_down(trend: TrendClassification | None) -> bool | None:
    """トレンドの分類を『下落か』へ写す(暫定写像)。None は None(分類が無い = 値を作らない)。"""
    if trend is None:
        return None
    if not isinstance(trend, TrendClassification):
        raise TypeError(f"trend は TrendClassification または None: {type(trend).__name__}")
    return TREND_IS_DOWN[trend]


def build_price_path_facts(
    metrics: ProfitProtectionMetrics, trend: TrendClassification | None
) -> PricePathFacts:
    """利益保全の指標とトレンドの分類から、素の型 5 つを作る(純粋・再計算なし)。"""
    return PricePathFacts(
        data_sufficient=metrics.insufficient_data_reason is None,
        drawdown_from_peak_pct=metrics.drawdown_from_peak_pct,
        peak_gain_pct=metrics.peak_gain_pct,
        current_gain_pct=metrics.current_gain_pct,
        trend_is_down=trend_is_down(trend),
    )

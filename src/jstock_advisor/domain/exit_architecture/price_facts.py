"""既存の利益保全の指標から、価格由来の facts(PriceFacts)を作る純粋関数(Issue #877 PR-2a。dormant)。

L3 PRICE_REGIME の入力(`PriceFacts`)の**供給元は 1 つ**にする。peak からの下落率・peak 時点の
含み益率・現在の含み益率は、既存の利益保全の指標(`ProfitProtectionMetrics`)が既に持つ値であり、
本 module はそれを**そのまま写す**(再計算しない)。peak の定義(basis_date の翌日以降の日次高値の
最大・同値なら最新日)は指標の側にだけあり、本 module には peak を探す処理を置かない。
別に計算すると、#879 の anchor(basis_date・peak_date・peak_price)と静かに食い違う二重の定義になる。

引数は素の型で受ける
  `ProfitProtectionMetrics` も `MomentumSnapshot` も import しない(C0 の import の守りを変えない)。
  呼び出し側が指標とモメンタムを 5 つの値へ分解して渡す。

規則(契約テストで固定する)
  ・`data_sufficient` が偽(指標の `insufficient_data_reason` が非 None)のとき、数値 3 つは全て
    UNDETERMINED(理由コード = INPUT_MISSING を一律に使う)。数値が渡されても使わない(不足が優先)。
    理由は自由文であり、文字列から分類しない・元の文章を持ち出さない(detail は固定の短い符号)
  ・`data_sufficient` が真で数値が None の組は、あり得ない組 = 契約違反として例外
    (黙って UNDETERMINED にしない)
  ・`trend_is_down`: 真 -> DOWN / 偽 -> NOT_DOWN / None(モメンタムが無い)-> UNDETERMINED
  ・範囲・有限性は `PriceFacts` の既存の検査に任せる(NaN・inf・範囲外は ValueError)。
    bool は数値として受けない
  ・閾値を引数にも持たない(閾値に依らず facts だけを返す)。時計・I/O・乱数なし

本 module に置かないもの
  閾値・分類(`price_regime.py`)・shadow の計算の受け口(PR-2b)・記録(PR-2c)・トレンドの分類の名前
  (DOWNTREND と STRONG_DOWNTREND をどう DOWN に写すかは呼び出し側。Shadow 評価用の暫定仕様)。
"""

from __future__ import annotations

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.price_regime import PriceFacts, TrendReading
from jstock_advisor.domain.exit_architecture.vocabulary import UndeterminedReason

#: UNDETERMINED の detail(固定の短い符号。元の文章・自由文は持ち出さない)
INSUFFICIENT_DETAIL = "PROFIT_PROTECTION_INSUFFICIENT"
TREND_MISSING_DETAIL = "TREND_MISSING"


def _undetermined_number(detail: str) -> Determination[float]:
    return Determination.undetermined(UndeterminedReason.INPUT_MISSING, detail)


def _required_number(name: str, value: float | None) -> Determination[float]:
    """data_sufficient が真のときの数値。None・bool・数値でないものは契約違反。"""
    if value is None:
        raise ValueError(f"data_sufficient が真のとき {name} は None であってはならない")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} は数値でなければならない(bool は数値として受けない): {value!r}")
    return Determination.of(float(value))


def _trend(trend_is_down: bool | None) -> Determination[TrendReading]:
    if trend_is_down is None:
        return Determination.undetermined(UndeterminedReason.INPUT_MISSING, TREND_MISSING_DETAIL)
    if not isinstance(trend_is_down, bool):
        raise TypeError(f"trend_is_down は bool または None: {trend_is_down!r}")
    return Determination.of(TrendReading.DOWN if trend_is_down else TrendReading.NOT_DOWN)


def build_price_facts(
    *,
    data_sufficient: bool,
    drawdown_from_peak_pct: float | None,
    peak_gain_pct: float | None,
    current_gain_pct: float | None,
    trend_is_down: bool | None,
) -> PriceFacts:
    """利益保全の指標の値を、そのまま PriceFacts へ写す(再計算しない・閾値を使わない)。"""
    if not isinstance(data_sufficient, bool):
        raise TypeError(f"data_sufficient は bool: {data_sufficient!r}")
    trend = _trend(trend_is_down)
    if not data_sufficient:
        # 不足が優先。渡された数値は(不正な値であっても)使わない・検査しない
        return PriceFacts(
            drawdown_from_peak_pct=_undetermined_number(INSUFFICIENT_DETAIL),
            peak_gain_pct=_undetermined_number(INSUFFICIENT_DETAIL),
            current_gain_pct=_undetermined_number(INSUFFICIENT_DETAIL),
            trend=trend,
        )
    return PriceFacts(
        drawdown_from_peak_pct=_required_number("drawdown_from_peak_pct", drawdown_from_peak_pct),
        peak_gain_pct=_required_number("peak_gain_pct", peak_gain_pct),
        current_gain_pct=_required_number("current_gain_pct", current_gain_pct),
        trend=trend,
    )

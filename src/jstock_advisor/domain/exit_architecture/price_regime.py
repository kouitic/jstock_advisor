"""L3 PRICE_REGIME の純粋関数(Issue #877 PR-1。#846 の N1。dormant)。

価格由来の facts(peak からの下落・トレンドの向き)を 1 つの正規化された状態
(RegimeState)にまとめる。**型と純粋関数だけ**であり、現行のエンジンからは参照されない
(配線なし・保存なし・flag なし・AWS なし)。

性質(契約テストで固定する)
  ・入口 gate が無い: 含み益(cushion)の水準で状態が決まらない。cushion は値を運ぶだけの修飾子
    (OP-4・UJ-2)。現行の「含み益の下限で信号が消える」区間(窓が空になる)を持たない
  ・単調: 他の入力と信頼性の状態を固定して peak からの下落を増やしたとき、状態は軽くならない(UJ-2)
  ・1 票: 価格由来の facts は regime の 1 票(root = PRICE_PATH)に集約する(R-A・D-01 / D-02)。
    吐き出し率は peak からの下落と peak の含み益の従属量であり、独立な票にせず導出値として運ぶ
  ・三値: 値が作れない facts は UNDETERMINED。HEALTHY は『全ての次元が健全と確定』したときだけ
    (確定していない次元が残るなら UNDETERMINED)。UNDETERMINED は悪化と数えない(fail-safe)
  ・正当な抑制は別に扱う: データの信頼性が使えない(UNUSABLE)ときの抑制は、抑えた候補を
    理由つきで suppressed に残す(UJ-2 の条件)
  ・遷移(以前 -> 現在)は、同じ価格履歴から再計算できる(永続を要しない)。『悪化したか』は
    N3(再通知の ratchet)が入力にできる形で固定する(UJ-3・AC-8)

値は『非決定(案)』
  閾値は引数で受け取り、**既定値を置かない**(事前登録 -> shadow -> replay / backtest -> USER 承認で
  確定する)。状態の個数・名称・トレンドとの対応も『案』であり、契約テストで固定する(変更は
  意図した変更として、テストと一緒に更新する)。

本 module に置かないもの
  arbiter・FULL の判定(N2。regime 単独では FULL の独立根拠にならない = decision.py)・
  再通知の ratchet(N3)・売却量(N5)・日次の audit 記録(#877 の PR-2)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import Evidence, EvidenceStatus
from jstock_advisor.domain.exit_architecture.verdicts import RegimeVerdict
from jstock_advisor.domain.exit_architecture.vocabulary import (
    RegimeState,
    ReliabilityClass,
    RootFactor,
    SuppressionReason,
    UndeterminedReason,
)

#: 状態の重さの順(軽い -> 重い)。N3 の ratchet(severity の悪化で再通知)の入力として固定する。
REGIME_ORDER: tuple[RegimeState, ...] = (
    RegimeState.HEALTHY,
    RegimeState.PEAK_WARNING,
    RegimeState.DOWNTREND_CONFIRMED,
    RegimeState.BREAKDOWN,
)

#: regime の 1 票の出典と root。価格由来の facts は、いくつ成立しても root は PRICE_PATH の 1 つ。
REGIME_VOTE_SOURCE = "L3_PRICE_REGIME"


class TrendReading(IntEnum):
    """トレンドの向き(価格系列の移動平均との位置・傾きから呼び出し側が分類した結果)。

    DOWNTREND と STRONG_DOWNTREND の区別は、必要になった時点で見直す(非決定)。
    """

    NOT_DOWN = 0
    DOWN = 1


#: トレンドが示す状態(案)。下落側の最上位の BREAKDOWN は peak からの下落でのみ到達する。
TREND_STATE: dict[TrendReading, RegimeState] = {
    TrendReading.NOT_DOWN: RegimeState.HEALTHY,
    TrendReading.DOWN: RegimeState.DOWNTREND_CONFIRMED,
}


def severity_rank(state: RegimeState) -> int:
    """状態の重さ(0 = HEALTHY が最も軽い)。"""
    return REGIME_ORDER.index(state)


def _check_drawdown(value: float) -> None:
    # NaN は比較が常に偽になるため、この範囲の検査が NaN と inf も拒否する
    if not 0 <= value <= 100:
        raise ValueError(f"drawdown_from_peak_pct は 0 以上 100 以下: {value}")


def _check_gain(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} は有限の数値でなければならない: {value!r}")
    if value < -100:
        raise ValueError(f"{name} は -100 以上(価格 0 が下限): {value}")


@dataclass(frozen=True)
class RegimeThresholds:
    """peak からの下落率(%)に対する状態の境界。**既定値は無い**(値は事前登録で確定する)。

    peak_warning < downtrend_confirmed < breakdown の厳密な昇順で、すべて 0 より大きく
    100 以下。下落率がちょうど境界のときは、重い側の状態にする。
    """

    peak_warning_drawdown_pct: float
    downtrend_confirmed_drawdown_pct: float
    breakdown_drawdown_pct: float

    def __post_init__(self) -> None:
        ordered = (
            self.peak_warning_drawdown_pct,
            self.downtrend_confirmed_drawdown_pct,
            self.breakdown_drawdown_pct,
        )
        if not 0 < ordered[0] < ordered[1] < ordered[2] <= 100:
            raise ValueError(
                f"閾値は 0 < 警戒 < 下降確認 < 崩れ <= 100 の昇順でなければならない: {ordered}"
            )


@dataclass(frozen=True)
class PriceFacts:
    """価格系列由来の facts。値が作れないものは UNDETERMINED(値を捏造しない)。

    drawdown_from_peak_pct  peak からの下落率(%)。0 以上 100 以下
    peak_gain_pct           peak 時点の含み益率(%)。cushion の材料(状態の入口 gate にしない)
    current_gain_pct        現在の含み益率(%)。cushion の材料(同上)
    trend                   トレンドの向き
    """

    drawdown_from_peak_pct: Determination[float]
    peak_gain_pct: Determination[float]
    current_gain_pct: Determination[float]
    trend: Determination[TrendReading]

    def __post_init__(self) -> None:
        if self.drawdown_from_peak_pct.is_determined:
            _check_drawdown(self.drawdown_from_peak_pct.unwrap())
        for name, gain in (
            ("peak_gain_pct", self.peak_gain_pct),
            ("current_gain_pct", self.current_gain_pct),
        ):
            if gain.is_determined:
                _check_gain(name, gain.unwrap())


@dataclass(frozen=True)
class SuppressedRegime:
    """抑えた候補と理由(正当な抑制を、黙って消さずに残す)。"""

    candidate: RegimeState
    reason: SuppressionReason


@dataclass(frozen=True)
class PriceRegimeResult:
    """regime の状態と、その内訳。

    drawdown_state / trend_state は次元ごとの状態(監査用)で、票ではない。票は regime_votes()
    の 1 件だけ。giveback_ratio_pct は導出値(従属量)で、状態の入力ではない。
    """

    state: Determination[RegimeState]
    drawdown_state: Determination[RegimeState]
    trend_state: Determination[RegimeState]
    giveback_ratio_pct: Determination[float]
    current_gain_pct: Determination[float]
    peak_gain_pct: Determination[float]
    suppressed: tuple[SuppressedRegime, ...] = ()


def giveback_ratio_pct(
    peak_gain_pct: Determination[float], drawdown_from_peak_pct: Determination[float]
) -> Determination[float]:
    """peak の含み益のうち、失った割合(%)。peak からの下落と peak の含み益の従属量。

    d = 下落率、g = peak の含み益率(いずれも比率)のとき d * (1 + g) / g。peak に含み益が
    無い(g <= 0)ときは『失う利益が無い』ので定義できず、UNDETERMINED とする。結果が有限の
    数値にならないとき(g が極小の正の値)も UNDETERMINED。非有限・範囲外の入力は拒否する
    (PriceFacts と同じ範囲。確定した値を捏造しない)。
    """
    if not peak_gain_pct.is_determined or not drawdown_from_peak_pct.is_determined:
        return Determination.undetermined(UndeterminedReason.INPUT_MISSING, "入力が確定していない")
    # 公開関数なので、PriceFacts を通らない入力も検証する(非有限・範囲外から値を作らない)
    _check_gain("peak_gain_pct", peak_gain_pct.unwrap())
    _check_drawdown(drawdown_from_peak_pct.unwrap())
    gain = peak_gain_pct.unwrap() / 100
    drawdown = drawdown_from_peak_pct.unwrap() / 100
    if gain <= 0:
        return Determination.undetermined(
            UndeterminedReason.GUARD_NOT_MET, "peak に含み益が無く、吐き出し率は定義できない"
        )
    ratio = drawdown * (1 + gain) / gain * 100
    if not math.isfinite(ratio):
        # peak の含み益が極小の正の値だと桁があふれる。表せない値を『確定した値』にしない
        return Determination.undetermined(
            UndeterminedReason.GUARD_NOT_MET, "吐き出し率が有限の数値として表せない"
        )
    return Determination.of(ratio)


def _drawdown_state(
    drawdown: Determination[float], thresholds: RegimeThresholds
) -> Determination[RegimeState]:
    if not drawdown.is_determined:
        return Determination.undetermined(
            UndeterminedReason.INPUT_MISSING, "下落率が確定していない"
        )
    value = drawdown.unwrap()
    if value >= thresholds.breakdown_drawdown_pct:
        return Determination.of(RegimeState.BREAKDOWN)
    if value >= thresholds.downtrend_confirmed_drawdown_pct:
        return Determination.of(RegimeState.DOWNTREND_CONFIRMED)
    if value >= thresholds.peak_warning_drawdown_pct:
        return Determination.of(RegimeState.PEAK_WARNING)
    return Determination.of(RegimeState.HEALTHY)


def _trend_state(trend: Determination[TrendReading]) -> Determination[RegimeState]:
    if not trend.is_determined:
        return Determination.undetermined(
            UndeterminedReason.COVERAGE_INSUFFICIENT, "トレンドが確定していない"
        )
    return Determination.of(TREND_STATE[trend.unwrap()])


def _combine(parts: tuple[Determination[RegimeState], ...]) -> Determination[RegimeState]:
    """次元ごとの状態を 1 つにする。確定した次元の最も重い状態を採る。

    確定していない次元が残るなら、HEALTHY とは言えない(悪化がまだ見えていないだけかもしれない)。
    その場合は UNDETERMINED にする。悪化がすでに確定している(HEALTHY より重い)なら、
    確定していない次元が残っていても、それを下回ることはないのでその状態を返す。
    """
    determined = [part.unwrap() for part in parts if part.is_determined]
    if not determined:
        return Determination.undetermined(UndeterminedReason.NOT_EVALUATED, "確定した次元が無い")
    worst = max(determined, key=severity_rank)
    if len(determined) < len(parts) and worst is RegimeState.HEALTHY:
        return Determination.undetermined(
            UndeterminedReason.COVERAGE_INSUFFICIENT, "確定していない次元が残る"
        )
    return Determination.of(worst)


def classify_price_regime(
    facts: PriceFacts, thresholds: RegimeThresholds, reliability: ReliabilityClass
) -> PriceRegimeResult:
    """価格由来の facts から regime の状態を決める(純粋関数)。

    含み益(cushion)は状態に使わない。信頼性が UNUSABLE のときは、状態を UNDETERMINED にし、
    抑えた候補を suppressed に残す。DEGRADED は強い action の上限(L0 の cap)の話であり、
    regime の状態は変えない。
    """
    drawdown_state = _drawdown_state(facts.drawdown_from_peak_pct, thresholds)
    trend_state = _trend_state(facts.trend)
    candidate = _combine((drawdown_state, trend_state))
    suppressed: tuple[SuppressedRegime, ...] = ()
    state = candidate
    if reliability is ReliabilityClass.UNUSABLE:
        state = Determination.undetermined(
            UndeterminedReason.RELIABILITY_UNUSABLE, "データの信頼性が使えない"
        )
        if candidate.is_determined and candidate.unwrap() is not RegimeState.HEALTHY:
            suppressed = (SuppressedRegime(candidate.unwrap(), SuppressionReason.RELIABILITY_CAP),)
    return PriceRegimeResult(
        state=state,
        drawdown_state=drawdown_state,
        trend_state=trend_state,
        giveback_ratio_pct=giveback_ratio_pct(facts.peak_gain_pct, facts.drawdown_from_peak_pct),
        current_gain_pct=facts.current_gain_pct,
        peak_gain_pct=facts.peak_gain_pct,
        suppressed=suppressed,
    )


def regime_votes(result: PriceRegimeResult) -> tuple[Evidence, ...]:
    """regime の票。価格由来の facts がいくつ成立しても、票は高々 1 件(root = PRICE_PATH)。

    HEALTHY・UNDETERMINED は票にしない(UNDETERMINED は根拠にも否定にも数えない)。
    PRICE_PATH は FULL の独立根拠に使えない(decision.py)。regime 単独では FULL にならない。
    """
    if not result.state.is_determined:
        return ()
    state = result.state.unwrap()
    if state is RegimeState.HEALTHY:
        return ()
    return (
        Evidence(
            root_factor=RootFactor.PRICE_PATH,
            source=REGIME_VOTE_SOURCE,
            fact_key=f"{REGIME_VOTE_SOURCE}:{state.value}",
            status=EvidenceStatus.TRIGGERED,
        ),
    )


def is_worsened(previous: Determination[RegimeState], current: Determination[RegimeState]) -> bool:
    """以前より状態が重くなったか(N3 の ratchet の入力)。

    どちらかが UNDETERMINED(履歴が足りない等)なら False(悪化と判定しない = fail-safe)。
    同じ重さの継続・軽くなった場合も False。
    """
    if not previous.is_determined or not current.is_determined:
        return False
    return severity_rank(current.unwrap()) > severity_rank(previous.unwrap())


def to_regime_verdict(
    current: PriceRegimeResult, previous: Determination[RegimeState]
) -> RegimeVerdict:
    """Arbiter の入力(L3 の verdict)へ写す。cushion は値を運ぶだけ。"""
    return RegimeVerdict(
        state=current.state,
        previous_state=previous,
        current_gain_pct=current.current_gain_pct,
        peak_gain_pct=current.peak_gain_pct,
    )

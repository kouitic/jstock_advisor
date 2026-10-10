"""保有判断(新方式)の再通知条件 R1〜R5 の純粋関数(Issue #890 PR-1。dormant)。

通知対象の状態が続いているとき、**前回『配信された』状態**と今日の状態を比べ、再通知条件
(R1〜R5)のどれが成立したかを返す。**型と純粋関数だけ**(config の読込なし・flag なし・AWS なし)。
判定への配線は PR-3 で、PR-2 では通知の記録を作る builder が保存形式(`serialize_hd_state`)で
書く側として参照する(読む側・判定の側は、まだどこからも参照されない)。

条件(config/holding_decision_rules.yaml の `renotification` の 5 項目に対応)
  R1 renotify_score_deterioration     スコアが前回の配信時点より閾値以上悪化した(前日比ではない累積)
  R2 renotify_on_decision_change      判定(種別 + category)が前回の配信時点から変わった
  R3 renotify_on_new_hard_gate        新たに確認済みの hard gate の理由コードが現れた
  R4 renotify_after_earnings          前回の配信より後の決算(財務期間末)を反映した
  R5 renotify_on_sell_price_change_pct 売却目安価格が前回の配信時点から閾値(%)以上動いた

性質(契約テストで固定する)
  ・純粋・決定的・時計なし: 同じ入力は同じ出力。入力の並び順に依存しない。日付は値として受け取る
  ・『成立』と『送信』は別: 本関数は条件の成立を返すだけで、実際に送るか(クールダウン・データ品質・
    同日の優先度・claim)は既存のゲートの責務。前回の状態は『配信された』通知のものだけを使い、
    遮られて配信されなかった日は前回の状態が進まないため、翌日も同じ条件が成立する
  ・比べられないときは『成立しない』: 前回が無い・旧形式・モデル版が違う・値が作れない場合は
    NOT_EVALUABLE(理由つき)で、成立にも不成立にも数えない(点や価格を捏造しない)。既存の
    判定(種別の変化・日数など)に任せる
  ・境界は厳密: 閾値ちょうどは成立(≧)。浮動小数点の誤差で境界が動かないよう、比較は
    Decimal(各値の repr)で行う

値は『非決定(案)』
  閾値は呼び出し側が渡し、**module に数値の既定を置かない**。まだ決まっていない選択(D-1〜D-6)は
  RenotificationPolicy の引数で受け取り、**既定値を持たせない**(呼び出す側が必ず明示する)。
  D-3(R4 の方式)は『決算を反映した最初の評価で 1 回』と『使わない』だけを実装する(『評価結果の
  変化を要する』『最小間隔つき』は新しい数が要るため、USER が数を決めた後に追加する)。

本 module に置かないもの
  通知の流れへのつなぎ込み(PR-3)・前回の状態の保存と builder(PR-2)・恒久記録(PR-4)・
  config の読込・文面・売却数量・既存の共通の再送判定(価格 3.0%・日数)。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

#: 保存形式(`hd_renotify_state`)の版。未知の版は NOT_EVALUABLE(STATE_VERSION_UNKNOWN)。
STATE_VERSION = 1

#: 前回の状態を置くキー(保有判断の Recommendation の config_values_used の追加キー。PR-2 が書く)。
HD_RENOTIFY_STATE_KEY = "hd_renotify_state"


class Condition(StrEnum):
    """再通知条件。値は config の項目名と対応させる。"""

    R1_SCORE_DETERIORATION = "renotify_score_deterioration"
    R2_DECISION_CHANGE = "renotify_on_decision_change"
    R3_NEW_HARD_GATE = "renotify_on_new_hard_gate"
    R4_AFTER_EARNINGS = "renotify_after_earnings"
    R5_SELL_PRICE_CHANGE = "renotify_on_sell_price_change_pct"


#: 評価と出力の順序(固定)。
CONDITION_ORDER: tuple[Condition, ...] = (
    Condition.R1_SCORE_DETERIORATION,
    Condition.R2_DECISION_CHANGE,
    Condition.R3_NEW_HARD_GATE,
    Condition.R4_AFTER_EARNINGS,
    Condition.R5_SELL_PRICE_CHANGE,
)


class ConditionStatus(StrEnum):
    MET = "MET"
    NOT_MET = "NOT_MET"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class Reason(StrEnum):
    """NOT_EVALUABLE / NOT_MET に添える理由。NOT_EVALUABLE は成立ではない。"""

    # 前回の状態が使えない
    NO_PREVIOUS_HD_STATE = "NO_PREVIOUS_HD_STATE"
    PREVIOUS_IS_LEGACY = "PREVIOUS_IS_LEGACY"
    STATE_VERSION_UNKNOWN = "STATE_VERSION_UNKNOWN"
    STATE_MALFORMED = "STATE_MALFORMED"
    # 比べる値が作れない
    MODEL_VERSION_MISMATCH = "MODEL_VERSION_MISMATCH"
    DECISION_SEVERITY_UNAVAILABLE = "DECISION_SEVERITY_UNAVAILABLE"
    HARD_GATE_UNCONFIRMED_ONLY = "HARD_GATE_UNCONFIRMED_ONLY"
    EARNINGS_KEY_UNAVAILABLE = "EARNINGS_KEY_UNAVAILABLE"
    PRICE_NOT_COMPARABLE = "PRICE_NOT_COMPARABLE"
    # 成立にしない(NOT_MET)
    DISABLED = "DISABLED"
    EARNINGS_DATA_STALE = "EARNINGS_DATA_STALE"


class GateConfirmation(StrEnum):
    """hard gate の理由コードごとの確認状態(評価時に保有判断のサービスが作る)。"""

    CONFIRMED = "CONFIRMED"  # 重大事象まで確認できた、または公式・一次情報の旗に基づく
    BASELINE_CONFIRMED = "BASELINE_CONFIRMED"  # 人が承認した baseline と投資ストーリーの点による
    KEYWORD_ONLY = "KEYWORD_ONLY"  # リスクキーワードの一致のみ
    UNVERIFIED = "UNVERIFIED"  # 旗の出所が確認できない


#: 『確認済み』として数える確認状態(D-5 の方針に関わらず常に数える)。
_CONFIRMED_STATES = frozenset({GateConfirmation.CONFIRMED, GateConfirmation.BASELINE_CONFIRMED})


class EarningsDataFreshness(StrEnum):
    """決算(財務)データの鮮度。R4 は STALE を成立にしない。"""

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


# --- 未決の選択(D-1〜D-6)。既定値を持たせない ---------------------------------------------


class PeriodicPolicy(StrEnum):
    """D-1: 共通の周期の再送(JST 暦日差 ≧ N 日)を、保有判断にも残すか。"""

    KEEP = "KEEP"
    STOP = "STOP"


class DecisionChangeScope(StrEnum):
    """D-2: R2 を変化すべてにするか、悪化のみにするか。"""

    ANY_CHANGE = "ANY_CHANGE"
    WORSENING_ONLY = "WORSENING_ONLY"


class EarningsMode(StrEnum):
    """D-3: R4 の方式。案 B(評価結果の変化を要する)・案 C(最小間隔つき)は未実装。

    案 B・C は新しい数(割合・日数)が要るため、USER が数を決めた後に追加する。
    """

    FIRST_EVALUATION_AFTER_EARNINGS = "FIRST_EVALUATION_AFTER_EARNINGS"  # 案 A
    DISABLED = "DISABLED"


class ScoreBasis(StrEnum):
    """D-4: R1 の比較に使う点数。"""

    BASE_SCORE = "BASE_SCORE"
    FINAL_SCORE = "FINAL_SCORE"


class KeywordOnlyHandling(StrEnum):
    """D-5: R3 が『キーワード一致のみ』の hard gate を数えるか。"""

    NOT_COUNTED = "NOT_COUNTED"
    COUNTED = "COUNTED"


class SellPriceReference(StrEnum):
    """D-6: R5 の参照価格。"""

    TARGET_PRICE_ONLY = "TARGET_PRICE_ONLY"
    INCLUDE_CURRENT_PRICE = "INCLUDE_CURRENT_PRICE"


@dataclass(frozen=True)
class RenotificationPolicy:
    """D-1〜D-6 の選択。**どの項目にも既定値を持たせない**(PR-1 が暗黙に一つへ決めないため)。"""

    periodic: PeriodicPolicy
    decision_change_scope: DecisionChangeScope
    earnings_mode: EarningsMode
    score_basis: ScoreBasis
    keyword_only: KeywordOnlyHandling
    sell_price_reference: SellPriceReference


@dataclass(frozen=True)
class RenotificationConfig:
    """config の `renotification` の 5 項目。**既定値を持たせない**(値は呼び出し側が渡す)。

    score_deterioration / sell_price_change_pct は 0 より大きい有限な数、または None。None のとき
    は、その条件を使わない(無効)。**0 は受け付けない**(差が 0 でも常に成立してしまい、意図しない
    毎日の再通知になるため。無効にしたいときは None を渡す)。bool の項目が False のときも同じ
    (評価しても成立にしない)。
    """

    score_deterioration: float | None
    on_decision_change: bool
    on_new_hard_gate: bool
    after_earnings: bool
    sell_price_change_pct: float | None

    def __post_init__(self) -> None:
        for name in ("score_deterioration", "sell_price_change_pct"):
            value = getattr(self, name)
            if value is not None and not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} は 0 より大きい有限な数または None(無効): {value!r}")


# --- 状態 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SellReference:
    """売却目安価格。kind は『何の価格か』(例: 投資前提再確認の目安・全部売却検討価格)。

    適正価格の弱気水準に由来する価格だけを渡す(監視用・即時執行目安は渡さない)。
    kind が違う価格どうしは比べない。
    """

    kind: str
    price: float

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("kind は空にできない")
        if not (math.isfinite(self.price) and self.price > 0):
            raise ValueError(f"price は 0 より大きい有限な数: {self.price!r}")


@dataclass(frozen=True)
class HdNotifyState:
    """ある評価日の、再通知の比較に必要な最小の状態(値だけ。銘柄・金額・株数は持たない)。

    recommendation_type / category は判定キー(R2)。decision_severity は『重いほど大きい』
    整数で、R2 を悪化のみにする場合(D-2)にだけ使う(None なら悪化の判定不能)。
    gate_confirmations は hard gate の理由コードごとの確認状態(同じコードに 2 つの状態は持てない)。
    earnings_key は評価に反映された最新の財務期間末で、earnings_freshness は今回の鮮度判定。
    market_price は D-6 で現在値も含める場合にだけ使う。
    """

    scoring_model_version: str
    base_score: float
    final_score: float
    recommendation_type: str
    category: str
    decision_severity: int | None = None
    gate_confirmations: frozenset[tuple[str, GateConfirmation]] = frozenset()
    earnings_key: date | None = None
    earnings_freshness: EarningsDataFreshness = EarningsDataFreshness.UNKNOWN
    sell_reference: SellReference | None = None
    market_price: float | None = None
    # 確認状態の分類の規則の版(holding_decision_gate_confirmation.CONFIRMATION_RULE_VERSION)。
    # 記録を読む側が『是正前の弱い確認』と『是正後の確認』を区別するための任意の項目
    # (Issue #890 PR-3。#897 の独立 review の SHOULD-1)。無い記録(#897 で作った記録を含む)は
    # None(版不明)。
    confirmation_rule_version: int | None = None

    def __post_init__(self) -> None:
        if not self.scoring_model_version:
            raise ValueError("scoring_model_version は空にできない")
        for name in ("base_score", "final_score"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} は有限な数: {getattr(self, name)!r}")
        if not self.recommendation_type or not self.category:
            raise ValueError("recommendation_type / category は空にできない")
        codes = [code for code, _ in self.gate_confirmations]
        if len(codes) != len(set(codes)):
            raise ValueError("同じ理由コードに 2 つの確認状態は持てない")
        if self.market_price is not None and not (
            math.isfinite(self.market_price) and self.market_price > 0
        ):
            raise ValueError(f"market_price は 0 より大きい有限な数: {self.market_price!r}")
        version = self.confirmation_rule_version
        if version is not None and (isinstance(version, bool) or version < 1):
            raise ValueError(f"confirmation_rule_version は 1 以上の整数または None: {version!r}")

    @property
    def decision_key(self) -> tuple[str, str]:
        return (self.recommendation_type, self.category)


@dataclass(frozen=True)
class StateUnavailable:
    """前回の状態が使えない(比較不能)。理由つき。"""

    reason: Reason


PreviousState = HdNotifyState | StateUnavailable


# --- 保存形式との変換(書く側は PR-2。本 PR は形式の定義と読む側だけ) ---------------------


def serialize_hd_state(state: HdNotifyState) -> dict[str, object]:
    """HdNotifyState を保存形式(JSON 互換の dict)にする。出力の並びは決定的。"""
    out: dict[str, object] = {
        "state_version": STATE_VERSION,
        "scoring_model_version": state.scoring_model_version,
        "base_score": state.base_score,
        "final_score": state.final_score,
        "recommendation_type": state.recommendation_type,
        "category": state.category,
        "decision_severity": state.decision_severity,
        "gate_confirmations": {
            code: confirmation.value for code, confirmation in sorted(state.gate_confirmations)
        },
        "earnings_key": state.earnings_key.isoformat() if state.earnings_key else None,
        "earnings_freshness": state.earnings_freshness.value,
        "sell_reference": (
            {"kind": state.sell_reference.kind, "price": state.sell_reference.price}
            if state.sell_reference
            else None
        ),
        "market_price": state.market_price,
        "confirmation_rule_version": state.confirmation_rule_version,
    }
    return out


def extract_hd_state(config_values_used: Mapping[str, object] | None) -> PreviousState:
    """前回『配信された』保有判断の通知の config_values_used から、前回の状態を読む。

    読めない場合は例外ではなく StateUnavailable(理由つき)を返す(NOT_EVALUABLE に使う)。
      None                      -> NO_PREVIOUS_HD_STATE(前回の配信が無い・保有判断の通知でない)
      キーが無い                -> PREVIOUS_IS_LEGACY(旧形式)
      版が未知                  -> STATE_VERSION_UNKNOWN
      形が不正・非有限・型違い  -> STATE_MALFORMED
    """
    if config_values_used is None:
        return StateUnavailable(Reason.NO_PREVIOUS_HD_STATE)
    raw = config_values_used.get(HD_RENOTIFY_STATE_KEY)
    if raw is None:
        return StateUnavailable(Reason.PREVIOUS_IS_LEGACY)
    if not isinstance(raw, Mapping):
        return StateUnavailable(Reason.STATE_MALFORMED)
    version = raw.get("state_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return StateUnavailable(Reason.STATE_MALFORMED)
    if version != STATE_VERSION:
        return StateUnavailable(Reason.STATE_VERSION_UNKNOWN)
    try:
        return _state_from_mapping(raw)
    except (KeyError, TypeError, ValueError):
        return StateUnavailable(Reason.STATE_MALFORMED)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("数値ではない")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("整数ではない")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("文字列ではない")
    return value


def _state_from_mapping(raw: Mapping[str, object]) -> HdNotifyState:
    severity = raw["decision_severity"]
    if severity is not None and (isinstance(severity, bool) or not isinstance(severity, int)):
        raise TypeError("decision_severity が整数ではない")
    confirmations_raw = raw["gate_confirmations"]
    if not isinstance(confirmations_raw, Mapping):
        raise TypeError("gate_confirmations が mapping ではない")
    confirmations = frozenset(
        (_text(code), GateConfirmation(_text(value))) for code, value in confirmations_raw.items()
    )
    earnings_raw = raw["earnings_key"]
    earnings_key = None if earnings_raw is None else date.fromisoformat(_text(earnings_raw))
    sell_raw = raw["sell_reference"]
    sell_reference: SellReference | None = None
    if sell_raw is not None:
        if not isinstance(sell_raw, Mapping):
            raise TypeError("sell_reference が mapping ではない")
        sell_reference = SellReference(
            kind=_text(sell_raw["kind"]), price=_number(sell_raw["price"])
        )
    return HdNotifyState(
        scoring_model_version=_text(raw["scoring_model_version"]),
        base_score=_number(raw["base_score"]),
        final_score=_number(raw["final_score"]),
        recommendation_type=_text(raw["recommendation_type"]),
        category=_text(raw["category"]),
        decision_severity=severity,
        gate_confirmations=confirmations,
        earnings_key=earnings_key,
        earnings_freshness=EarningsDataFreshness(_text(raw["earnings_freshness"])),
        sell_reference=sell_reference,
        market_price=_optional_number(raw["market_price"]),
        confirmation_rule_version=_optional_int(raw.get("confirmation_rule_version")),
    )


# --- 判定 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionResult:
    status: ConditionStatus
    reason: Reason | None = None


@dataclass(frozen=True)
class HdRenotifyDecision:
    """再通知の決定。

    evaluations は CONDITION_ORDER の順に 5 条件すべてを持つ。conditions_met は MET の集合。
    send_by_policy = (conditions_met が空でない) または (periodic_due)。periodic_due は、呼び出し側
    が渡した周期の成立に、方針(D-1)が KEEP のときだけ真になる。
    send_by_policy は『この方針が再通知を求めるか』であり、実際に送るかではない(クールダウン・
    データ品質・同日の優先度・claim は既存のゲートが後段で決める)。前回の状態が使えず全条件が
    NOT_EVALUABLE でも、periodic_due が真(KEEP)なら send_by_policy は真になりうる。
    条件が何も成立せず周期もないときは False で、既存の判定(種別の変化・日数など)に任せる。
    """

    evaluations: tuple[tuple[Condition, ConditionResult], ...]
    conditions_met: frozenset[Condition]
    periodic_due: bool
    send_by_policy: bool

    def result_of(self, condition: Condition) -> ConditionResult:
        for name, result in self.evaluations:
            if name is condition:
                return result
        raise KeyError(condition)


_NOT_MET = ConditionResult(ConditionStatus.NOT_MET)
_MET = ConditionResult(ConditionStatus.MET)


def _disabled() -> ConditionResult:
    return ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


def _not_evaluable(reason: Reason) -> ConditionResult:
    return ConditionResult(ConditionStatus.NOT_EVALUABLE, reason)


def _dec(value: float) -> Decimal:
    # 浮動小数点の 2 進誤差で境界(閾値ちょうど)が動かないよう、最短 repr の十進で比べる。
    return Decimal(repr(value))


def _evaluate_r1(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
) -> ConditionResult:
    if config.score_deterioration is None:
        return _disabled()
    if isinstance(previous, StateUnavailable):
        return _not_evaluable(previous.reason)
    if previous.scoring_model_version != current.scoring_model_version:
        return _not_evaluable(Reason.MODEL_VERSION_MISMATCH)
    if policy.score_basis is ScoreBasis.BASE_SCORE:
        before, after = previous.base_score, current.base_score
    else:
        before, after = previous.final_score, current.final_score
    deterioration = _dec(before) - _dec(after)
    return _MET if deterioration >= _dec(config.score_deterioration) else _NOT_MET


def _evaluate_r2(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
) -> ConditionResult:
    if not config.on_decision_change:
        return _disabled()
    if isinstance(previous, StateUnavailable):
        return _not_evaluable(previous.reason)
    if policy.decision_change_scope is DecisionChangeScope.ANY_CHANGE:
        return _MET if previous.decision_key != current.decision_key else _NOT_MET
    if previous.decision_severity is None or current.decision_severity is None:
        return _not_evaluable(Reason.DECISION_SEVERITY_UNAVAILABLE)
    return _MET if current.decision_severity > previous.decision_severity else _NOT_MET


def _counted_codes(state: HdNotifyState, policy: RenotificationPolicy) -> frozenset[str]:
    counted = set(_CONFIRMED_STATES)
    if policy.keyword_only is KeywordOnlyHandling.COUNTED:
        counted.add(GateConfirmation.KEYWORD_ONLY)
    return frozenset(
        code for code, confirmation in state.gate_confirmations if confirmation in counted
    )


def _evaluate_r3(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
) -> ConditionResult:
    if not config.on_new_hard_gate:
        return _disabled()
    if isinstance(previous, StateUnavailable):
        return _not_evaluable(previous.reason)
    current_counted = _counted_codes(current, policy)
    if current.gate_confirmations and not current_counted:
        # 発動している hard gate が確認済みでない(キーワード一致のみ等)。
        # 最初の通知は現行の判定のまま(本関数は変えない)。
        return _not_evaluable(Reason.HARD_GATE_UNCONFIRMED_ONLY)
    new_codes = current_counted - _counted_codes(previous, policy)
    return _MET if new_codes else _NOT_MET


def _evaluate_r4(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
) -> ConditionResult:
    if not config.after_earnings or policy.earnings_mode is EarningsMode.DISABLED:
        return _disabled()
    if isinstance(previous, StateUnavailable):
        return _not_evaluable(previous.reason)
    if current.earnings_key is None or previous.earnings_key is None:
        return _not_evaluable(Reason.EARNINGS_KEY_UNAVAILABLE)
    if current.earnings_key <= previous.earnings_key:
        return _NOT_MET
    if current.earnings_freshness is EarningsDataFreshness.STALE:
        return ConditionResult(ConditionStatus.NOT_MET, Reason.EARNINGS_DATA_STALE)
    return _MET


def _price_change_met(before: float, after: float, pct: float) -> bool:
    # |after / before - 1| >= pct / 100  <=>  |after - before| * 100 >= pct * before(before > 0)
    return abs(_dec(after) - _dec(before)) * 100 >= _dec(pct) * _dec(before)


def _evaluate_r5(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
) -> ConditionResult:
    pct = config.sell_price_change_pct
    if pct is None:
        return _disabled()
    if isinstance(previous, StateUnavailable):
        return _not_evaluable(previous.reason)
    before_ref, after_ref = previous.sell_reference, current.sell_reference
    if before_ref is not None and after_ref is not None and before_ref.kind == after_ref.kind:
        return _MET if _price_change_met(before_ref.price, after_ref.price, pct) else _NOT_MET
    if (
        policy.sell_price_reference is SellPriceReference.INCLUDE_CURRENT_PRICE
        and previous.market_price is not None
        and current.market_price is not None
    ):
        return (
            _MET
            if _price_change_met(previous.market_price, current.market_price, pct)
            else _NOT_MET
        )
    return _not_evaluable(Reason.PRICE_NOT_COMPARABLE)


def decide_hd_renotification(
    current: HdNotifyState,
    previous: PreviousState,
    config: RenotificationConfig,
    policy: RenotificationPolicy,
    *,
    periodic_due: bool,
) -> HdRenotifyDecision:
    """今日の状態と前回『配信された』状態から、R1〜R5 の成立を決める。

    periodic_due は、共通の周期の再送(JST 暦日差 ≧ N 日)が成立しているかを、呼び出し側が計算して
    渡す(本関数は日数も時計も扱わない)。policy.periodic が STOP のときは無視する。
    """
    evaluators = (
        (Condition.R1_SCORE_DETERIORATION, _evaluate_r1),
        (Condition.R2_DECISION_CHANGE, _evaluate_r2),
        (Condition.R3_NEW_HARD_GATE, _evaluate_r3),
        (Condition.R4_AFTER_EARNINGS, _evaluate_r4),
        (Condition.R5_SELL_PRICE_CHANGE, _evaluate_r5),
    )
    evaluations = tuple(
        (condition, evaluate(current, previous, config, policy))
        for condition, evaluate in evaluators
    )
    met = frozenset(
        condition for condition, result in evaluations if result.status is ConditionStatus.MET
    )
    periodic = periodic_due and policy.periodic is PeriodicPolicy.KEEP
    return HdRenotifyDecision(
        evaluations=evaluations,
        conditions_met=met,
        periodic_due=periodic,
        send_by_policy=bool(met) or periodic,
    )

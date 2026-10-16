"""転帰(シグナル後の値動き)の計算と、売却の効果の比較(Issue #880 R1a の a-4)。

純粋関数。時計・営業日カレンダーを使わない: ★ 営業日の索引は呼び出し側が渡す ★(価格の列の添字 =
営業日の索引。休場日・連休を跨ぐ数え方は呼び出し側の責務)。窓・起点・税と手数料はすべて引数
(事前登録の値。既定なし)。価格は `Decimal`(丸めの誤差を持ち込まない)。

## 転帰(窓ごと)

  forward_return  窓の最後の終値 / 起点の価格 − 1
  max_drawdown    窓の中の最安値 / 起点の価格 − 1(0 以下。上回る場合は 0)
  rebound         最安値の後の最高値 / 最安値 − 1(0 以上)
  new_high        窓の中の最高値が、判定前の高値(引数)を上回ったか

起点: DECISION_DAY_CLOSE = 判定日の終値 / NEXT_DAY_OPEN = 翌営業日の始値(実行可能な最初の価格。
どちらかは登録された選択)。価格の列が窓に足りないときは `INSUFFICIENT_SERIES`(値を作らない)。

## 売却の効果(HOLD 100% との比較。税・手数料の概算控除は引数)

  早売り  = 売った割合 × max(0, 窓の後の上昇)(売って逃した上昇・直後の反発)
  売り遅れ = 売れ残った割合 × max(0, 窓の後の下落)(売った後・残りのさらなる下落)
  回避した下落 = 売った割合 × max(0, 窓の後の下落)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from jstock_advisor.domain.backtest.errors import ReplayInputError, require_int

_ZERO = Decimal(0)
_ONE = Decimal(1)


class OriginRule(StrEnum):
    """転帰の起点(登録された選択)。"""

    DECISION_DAY_CLOSE = "DECISION_DAY_CLOSE"
    NEXT_DAY_OPEN = "NEXT_DAY_OPEN"


class OutcomeStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INSUFFICIENT_SERIES = "INSUFFICIENT_SERIES"


@dataclass(frozen=True)
class Bar:
    """1 営業日の価格(始値・高値・安値・終値)。"""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= _ZERO:
                raise ReplayInputError(f"{name} must be a positive finite Decimal")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise ReplayInputError("bar is inconsistent (low <= open <= high)")
        if not self.low <= self.close <= self.high:
            raise ReplayInputError("bar is inconsistent (low <= close <= high)")


@dataclass(frozen=True)
class OutcomeSpec:
    """登録された窓と起点(既定なし)。windows は営業日数の狭義の増加列。"""

    windows: tuple[int, ...]
    origin: OriginRule

    def __post_init__(self) -> None:
        if not isinstance(self.windows, tuple) or not self.windows:
            raise ReplayInputError("windows must be a non-empty tuple")
        previous = 0
        for window in self.windows:
            previous = require_int(window, "window", minimum=previous + 1)
        if not isinstance(self.origin, OriginRule):
            raise ReplayInputError("origin must be an OriginRule")


@dataclass(frozen=True)
class WindowOutcome:
    window: int
    status: OutcomeStatus
    forward_return: Decimal | None = None
    max_drawdown: Decimal | None = None
    rebound: Decimal | None = None
    new_high: bool | None = None


def _origin_price(bars: Sequence[Bar], decision_index: int, rule: OriginRule) -> Decimal | None:
    if rule is OriginRule.DECISION_DAY_CLOSE:
        return bars[decision_index].close
    next_index = decision_index + 1
    return bars[next_index].open if next_index < len(bars) else None


def compute_outcomes(
    bars: Sequence[Bar], decision_index: int, spec: OutcomeSpec, *, prior_high: Decimal
) -> tuple[WindowOutcome, ...]:
    """判定日(`decision_index`)の後の転帰を、窓ごとに計算する。

    bars         営業日の索引で並んだ価格の列(呼び出し側が渡す)
    prior_high   判定前の高値(新高値更新の基準。引数)
    """
    if not isinstance(spec, OutcomeSpec):
        raise ReplayInputError("spec must be an OutcomeSpec")
    if not all(isinstance(bar, Bar) for bar in bars):
        raise ReplayInputError("bars must be Bar")
    require_int(decision_index, "decision_index", minimum=0)
    if decision_index >= len(bars):
        raise ReplayInputError("decision_index is outside the series")
    if not isinstance(prior_high, Decimal) or not prior_high.is_finite() or prior_high <= _ZERO:
        raise ReplayInputError("prior_high must be a positive finite Decimal")
    origin = _origin_price(bars, decision_index, spec.origin)
    first_bar = decision_index + 1  # 窓の最初の価格は、どちらの起点でも判定日の翌営業日
    outcomes: list[WindowOutcome] = []
    for window in spec.windows:
        last = decision_index + window
        if origin is None or last >= len(bars):
            outcomes.append(WindowOutcome(window, OutcomeStatus.INSUFFICIENT_SERIES))
            continue
        span = bars[first_bar : last + 1]
        lows = [bar.low for bar in span]
        lowest = min(lows)
        lowest_at = lows.index(lowest)
        highest_after = max(bar.high for bar in span[lowest_at:])
        outcomes.append(
            WindowOutcome(
                window=window,
                status=OutcomeStatus.COMPLETE,
                forward_return=bars[last].close / origin - _ONE,
                max_drawdown=min(_ZERO, lowest / origin - _ONE),
                rebound=highest_after / lowest - _ONE,
                new_high=max(bar.high for bar in span) > prior_high,
            )
        )
    return tuple(outcomes)


@dataclass(frozen=True)
class SaleEffect:
    """売却の効果(起点の評価額を 1 とした相対値)。"""

    hold_value: Decimal
    after_sale_value: Decimal
    missed_upside: Decimal  # 早売り
    avoided_loss: Decimal  # 回避した下落
    further_loss_on_remaining: Decimal  # 売り遅れ


def sale_effect(
    outcome: WindowOutcome, *, sold_ratio: Decimal, sale_cost_rate: Decimal
) -> SaleEffect | None:
    """売った割合(0〜1)と、税・手数料の概算控除率(引数)から、HOLD 100% との差を計算する。

    転帰が COMPLETE でないときは None(値を作らない)。
    """
    if not isinstance(sold_ratio, Decimal) or not _ZERO <= sold_ratio <= _ONE:
        raise ReplayInputError("sold_ratio must be a Decimal in [0, 1]")
    if not isinstance(sale_cost_rate, Decimal) or not _ZERO <= sale_cost_rate < _ONE:
        raise ReplayInputError("sale_cost_rate must be a Decimal in [0, 1)")
    if outcome.status is not OutcomeStatus.COMPLETE or outcome.forward_return is None:
        return None
    change = outcome.forward_return
    remaining = _ONE - sold_ratio
    return SaleEffect(
        hold_value=_ONE + change,
        after_sale_value=sold_ratio * (_ONE - sale_cost_rate) + remaining * (_ONE + change),
        missed_upside=sold_ratio * max(_ZERO, change),
        avoided_loss=sold_ratio * max(_ZERO, -change),
        further_loss_on_remaining=remaining * max(_ZERO, -change),
    )

"""Fair Value Revision の規則(Issue #601 PR-2 案 A。dormant)。

Fair Value の履歴から『企業価値の見方が上方・下方に修正されたか』を、**水準の持続**で確認して
判定する純粋関数。どこからも import されない(配線なし・保存なし・flag なし・履歴の取得なし)。
USER 承認: #122 issuecomment-6110016281(#601 PR-2 = 案 A)。

## 規則(考え方 4 点は USER 承認済み。数値は未決)

  1 水準の持続: 参照点(窓 W 日以内の最も古い適格な点)に対して、最新の点が θ% 以上離れ、
    その水準が最新の M 点続いているとき『確認』する(1 点だけの変動で方向を確定しない)
  2 上下は対称: UP にも DOWN と同じ確認を課す。確認点数は M_up / M_down の別の引数
  3 未確認は期待リターンに算入しない: 方向が *_UNCONFIRMED のとき、revision_pct は参考値で、
    `counts_toward_expected_return` は False(`confirmed_revision_pct` は None)
  4 UNDETERMINED の維持: 履歴なし・適格な点なし・点数不足・手法の集合の変更・鮮度不足・
    ★パラメータ未決★ は、0 や STABLE や直前の値で埋めず UNDETERMINED(理由つき)

## 境界の定義

  変化率 = (最新 / 参照 − 1) × 100(符号つき)。方向あり = 変化率の大きさが θ **以上**
  (+θ 以上で上、−θ 以下で下)。θ 未満は STABLE。Fair Value は Decimal で、閾値の比較は割り算を
  使わない(v × 100 と ref × (100 ± θ))ため、±θ ちょうどが浮動小数点の誤差で外れない。
  『確認』= 最新の M 点がすべて、最新の点と同じ側に θ 以上離れている。

## 未決のパラメータ

  W(窓)・K(最低点数)・θ・M_up・M_down・鮮度基準は未決。引数 `parameters` が None の間は常に
  UNDETERMINED(REVISION_PARAMETERS_NOT_DECIDED)。出荷する既定値は置かない(config にも書かない)。
  テストで使う数値は規則の動きを確かめる例示であり、決定値ではない。
  K は M_up・M_down のどちらよりも大きくなければならない(K ≤ M だと、最低点数の履歴では
  参照点自身を含めずに M 点をそろえられず、確認が構造的にできない)。違反は構築時に拒否する。

## 持たないもの(契約テストで固定)

  時計(evaluation_date は引数。日付の差の日数計算のみ)・営業日・timezone / 履歴の取得 /
  ExpectedReturnResult.revision への接続 / 取得価格・available_cash / services・infrastructure・
  lambda_handlers・providers・cli・config への依存 / jstock_advisor 内の他 module への依存
  (本 module は標準ライブラリだけを import する。接続は別 PR)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

#: 規則の版。数値(W・K・θ・M・鮮度基準)が未決の構造だけの版であることを示す。
MODEL_VERSION = "fv-revision-v1-params-undecided"

_HUNDRED = Decimal(100)


class RevisionDirection(StrEnum):
    """判定の方向。*_UNCONFIRMED は『方向の兆候はあるが、水準の持続を確認できていない』。"""

    UP = "UP"
    STABLE = "STABLE"
    DOWN = "DOWN"
    UP_UNCONFIRMED = "UP_UNCONFIRMED"
    DOWN_UNCONFIRMED = "DOWN_UNCONFIRMED"
    UNDETERMINED = "UNDETERMINED"


class RevisionReason(StrEnum):
    """方向を確定できない理由(UNDETERMINED の理由と、未確認の理由)。"""

    REVISION_PARAMETERS_NOT_DECIDED = "REVISION_PARAMETERS_NOT_DECIDED"  # パラメータが未決
    NO_HISTORY = "NO_HISTORY"  # 評価日までに点が無い
    ONLY_UNUSABLE_POINTS = "ONLY_UNUSABLE_POINTS"  # 適格な点が無い
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # 窓内の適格な点が K 点未満
    STALE_HISTORY = "STALE_HISTORY"  # 最新の適格な点が古すぎる
    METHOD_SET_CHANGED = "METHOD_SET_CHANGED"  # 比べる点の手法の集合が違う
    REVISION_UNCONFIRMED = "REVISION_UNCONFIRMED"  # 方向の兆候はあるが水準の持続が未確認


#: UNDETERMINED の理由(REVISION_UNCONFIRMED を除く)。
UNDETERMINED_REASONS: frozenset[RevisionReason] = frozenset(
    reason for reason in RevisionReason if reason is not RevisionReason.REVISION_UNCONFIRMED
)

_CONFIRMED_DIRECTIONS = frozenset(
    {RevisionDirection.UP, RevisionDirection.STABLE, RevisionDirection.DOWN}
)
_UNCONFIRMED_DIRECTIONS = frozenset(
    {RevisionDirection.UP_UNCONFIRMED, RevisionDirection.DOWN_UNCONFIRMED}
)


def _require_plain_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{name} は date(datetime ではない)でなければならない")
    return value


@dataclass(frozen=True)
class RevisionPoint:
    """Fair Value の履歴の 1 点。取得は呼び出し側(本 module は履歴を取得しない)。"""

    evaluation_date: date
    fair_value: Decimal | None
    usable_for_trading_judgment: bool
    method_set: frozenset[str]

    def __post_init__(self) -> None:
        _require_plain_date(self.evaluation_date, "evaluation_date")
        if self.fair_value is not None and not isinstance(self.fair_value, Decimal):
            raise ValueError("fair_value は Decimal または None でなければならない")
        if not isinstance(self.method_set, frozenset) or not all(
            isinstance(method, str) for method in self.method_set
        ):
            raise ValueError("method_set は文字列の frozenset でなければならない")

    @property
    def is_eligible(self) -> bool:
        """方向の根拠にできる点: 利用可能で、Fair Value が有限の正の値(P2)。"""
        value = self.fair_value
        return (
            self.usable_for_trading_judgment
            and value is not None
            and value.is_finite()
            and value > 0
        )


@dataclass(frozen=True)
class RevisionParameters:
    """未決の数値。USER が決めるまで構築しない(呼び出し側は None を渡す)。

    window_days        窓 W(日)。参照点は窓内の最も古い適格な点
    min_points         最低点数 K。窓内の適格な点がこれ未満なら判定しない
    theta_pct          STABLE の幅 θ(%)。0 < θ < 100
    confirm_up         UP の確認点数 M_up
    confirm_down       DOWN の確認点数 M_down
    max_staleness_days 鮮度基準(日)。最新の適格な点が評価日からこれより古ければ判定しない
    """

    window_days: int
    min_points: int
    theta_pct: Decimal
    confirm_up: int
    confirm_down: int
    max_staleness_days: int

    def __post_init__(self) -> None:
        for name in ("window_days", "min_points", "confirm_up", "confirm_down"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} は 1 以上の整数でなければならない")
        if (
            isinstance(self.max_staleness_days, bool)
            or not isinstance(self.max_staleness_days, int)
            or self.max_staleness_days < 0
        ):
            raise ValueError("max_staleness_days は 0 以上の整数でなければならない")
        if not isinstance(self.theta_pct, Decimal) or not self.theta_pct.is_finite():
            raise ValueError("theta_pct は有限の Decimal でなければならない")
        if not (0 < self.theta_pct < _HUNDRED):
            raise ValueError("theta_pct は 0 より大きく 100 未満でなければならない")
        if self.min_points <= max(self.confirm_up, self.confirm_down):
            raise ValueError(
                "min_points は confirm_up・confirm_down のどちらよりも大きくなければならない"
            )


@dataclass(frozen=True)
class FairValueRevisionResult:
    """Fair Value Revision の判定結果。

    revision_pct は (最新 / 参照 − 1) × 100(符号つき)。*_UNCONFIRMED では**参考値**で、
    期待リターンには算入しない。UNDETERMINED では None。
    """

    model_version: str
    evaluation_date: date
    direction: RevisionDirection
    revision_pct: float | None
    reason: RevisionReason | None
    n_points: int = 0
    reference_date: date | None = None
    latest_date: date | None = None
    latest_points_disagree: bool = False

    def __post_init__(self) -> None:
        _require_plain_date(self.evaluation_date, "evaluation_date")
        if self.direction in _CONFIRMED_DIRECTIONS:
            if self.reason is not None or self.revision_pct is None:
                raise ValueError("確認済みの方向は、理由なし・revision_pct ありでなければならない")
        elif self.direction in _UNCONFIRMED_DIRECTIONS:
            if self.reason is not RevisionReason.REVISION_UNCONFIRMED or self.revision_pct is None:
                raise ValueError("未確認の方向は、REVISION_UNCONFIRMED・revision_pct(参考値)が要る")
        else:
            if self.reason not in UNDETERMINED_REASONS:
                raise ValueError("UNDETERMINED には UNDETERMINED の理由が要る")
            if self.revision_pct is not None:
                raise ValueError("UNDETERMINED は revision_pct を持たない(0 で埋めない)")
        if self.latest_points_disagree and self.direction not in _UNCONFIRMED_DIRECTIONS:
            raise ValueError("両側に振れた印は未確認の方向にだけ付く")

    @property
    def counts_toward_expected_return(self) -> bool:
        """期待リターンに算入してよいか。確認済み(UP / STABLE / DOWN)のときだけ True。"""
        return self.direction in _CONFIRMED_DIRECTIONS

    @property
    def confirmed_revision_pct(self) -> float | None:
        """算入してよい revision_pct。未確認・UNDETERMINED では None(参考値は返さない)。"""
        return self.revision_pct if self.counts_toward_expected_return else None


def _undetermined(
    evaluation_date: date,
    reason: RevisionReason,
    *,
    n_points: int = 0,
    reference_date: date | None = None,
    latest_date: date | None = None,
) -> FairValueRevisionResult:
    return FairValueRevisionResult(
        model_version=MODEL_VERSION,
        evaluation_date=evaluation_date,
        direction=RevisionDirection.UNDETERMINED,
        revision_pct=None,
        reason=reason,
        n_points=n_points,
        reference_date=reference_date,
        latest_date=latest_date,
    )


def _value(point: RevisionPoint) -> Decimal:
    """適格な点の Fair Value。適格でない点(None)が渡るのは呼び出しの誤りで、値を作らず拒否する。"""
    if point.fair_value is None:
        raise ValueError("適格でない点の Fair Value は読めない")
    return point.fair_value


def _side(value: Decimal, reference: Decimal, theta_pct: Decimal) -> int:
    """参照点に対する側: +1(+θ 以上)/ −1(−θ 以下)/ 0(θ 未満)。割り算を使わない。"""
    scaled = value * _HUNDRED
    if scaled >= reference * (_HUNDRED + theta_pct):
        return 1
    if scaled <= reference * (_HUNDRED - theta_pct):
        return -1
    return 0


def evaluate_fair_value_revision(
    history: Sequence[RevisionPoint],
    *,
    evaluation_date: date,
    parameters: RevisionParameters | None,
) -> FairValueRevisionResult:
    """Fair Value Revision を判定する(純粋関数。時計・I/O なし)。

    history は評価日の順でなくてよい。同じ評価日が複数あるときは**入力順で後のものを採る**
    (P1。呼び出し側が作成順に渡すと『評価日ごとに最新の 1 件』になる)。評価日より後の点は
    その時点で知り得ないため除く。
    """
    _require_plain_date(evaluation_date, "evaluation_date")
    if parameters is None:
        return _undetermined(evaluation_date, RevisionReason.REVISION_PARAMETERS_NOT_DECIDED)

    known = [point for point in history if point.evaluation_date <= evaluation_date]
    if not known:
        return _undetermined(evaluation_date, RevisionReason.NO_HISTORY)

    by_date: dict[date, RevisionPoint] = {}
    for point in known:
        by_date[point.evaluation_date] = point
    eligible = sorted(
        (point for point in by_date.values() if point.is_eligible),
        key=lambda point: point.evaluation_date,
    )
    if not eligible:
        return _undetermined(evaluation_date, RevisionReason.ONLY_UNUSABLE_POINTS)

    latest = eligible[-1]
    if (evaluation_date - latest.evaluation_date).days > parameters.max_staleness_days:
        return _undetermined(
            evaluation_date, RevisionReason.STALE_HISTORY, latest_date=latest.evaluation_date
        )

    window_start = evaluation_date - timedelta(days=parameters.window_days)
    window = [point for point in eligible if point.evaluation_date >= window_start]
    if len(window) < parameters.min_points:
        return _undetermined(
            evaluation_date,
            RevisionReason.INSUFFICIENT_HISTORY,
            n_points=len(window),
            latest_date=latest.evaluation_date,
        )

    reference = window[0]
    n_points = len(window)

    def undetermined(reason: RevisionReason) -> FairValueRevisionResult:
        return _undetermined(
            evaluation_date,
            reason,
            n_points=n_points,
            reference_date=reference.evaluation_date,
            latest_date=latest.evaluation_date,
        )

    def judged(
        direction: RevisionDirection,
        revision_pct: float,
        reason: RevisionReason | None = None,
        *,
        disagree: bool = False,
    ) -> FairValueRevisionResult:
        return FairValueRevisionResult(
            model_version=MODEL_VERSION,
            evaluation_date=evaluation_date,
            direction=direction,
            revision_pct=revision_pct,
            reason=reason,
            n_points=n_points,
            reference_date=reference.evaluation_date,
            latest_date=latest.evaluation_date,
            latest_points_disagree=disagree,
        )

    if reference.method_set != latest.method_set:
        return undetermined(RevisionReason.METHOD_SET_CHANGED)

    ref_value = _value(reference)
    latest_value = _value(latest)
    theta = parameters.theta_pct
    revision_pct = float((latest_value - ref_value) / ref_value * _HUNDRED)

    latest_side = _side(latest_value, ref_value, theta)
    if latest_side == 0:
        return judged(RevisionDirection.STABLE, revision_pct)

    confirm_points = parameters.confirm_up if latest_side > 0 else parameters.confirm_down
    tail = window[-confirm_points:]
    if any(point.method_set != reference.method_set for point in tail):
        return undetermined(RevisionReason.METHOD_SET_CHANGED)

    sides = [_side(_value(point), ref_value, theta) for point in tail]
    if all(side == latest_side for side in sides):
        direction = RevisionDirection.UP if latest_side > 0 else RevisionDirection.DOWN
        return judged(direction, revision_pct)
    unconfirmed = (
        RevisionDirection.UP_UNCONFIRMED if latest_side > 0 else RevisionDirection.DOWN_UNCONFIRMED
    )
    return judged(
        unconfirmed,
        revision_pct,
        RevisionReason.REVISION_UNCONFIRMED,
        disagree=(1 in sides and -1 in sides),
    )

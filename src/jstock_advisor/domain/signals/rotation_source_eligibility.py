"""資金入替の入替元の適格性の導出(Issue #604 P-a。#128 R2。dormant)。

`domain/signals/capital_rotation.py` の `evaluate_capital_rotation` は、入替元(保有)の適格性
(`SourceEligibility`)の導出を、呼び出し側(入力の組み立て役)に委ねている。この module はその導出の
**純粋関数**と、入力の型(保有バッチの結果の記録から読んだ事実の dataclass)を持つ。

設計: #604 rev2(…6137374981)の 4 節 + rev3(…6137850710)の R2・R3・R4(設計レビュー PASS
…6137908828)。実装順の案 = …6138096876 の P-a。**時計・I/O・永続化・AWS・LINE に触れない**。
どこからも import されない(dormant。P の Lambda = P-b は USER の確認後)。

## 守ること(契約テストで固定する)

* ★ fail-closed ★: 迷ったら入替元にしない。ELIGIBLE は『評価が完了し、出口の証拠が無い』ときだけ
* 出口を出している保有は必ず BLOCKED_BY_EXIT(通知が抑止されていても。抑止は通知の都合)
* ★ 分類の自動変換をしない ★: 既存の出口・要確認・注意を CAPITAL_ROTATION に読み替えない
* 出口・緊急確認の判定は正本 `resolve_holding_summary_action(...) is not None` を呼ぶ
  この表は『None を返す型』だけを『要確認 / 注意 / HOLD / 保有側に現れない』へ分類する差分
* `RecommendationType` の全メンバーを明示的に分類する(新メンバーが増えたらテストが落ちる)
* `review_flag` は使わない(Arbiter の配線 = #878 PR-3b 以降に、Decision の exit_class・
  review_class へ置き換える。そのとき同じ結論になることを契約テストで固定する)

## 導出(上から順。最初に当たったもの。理由コードを必ず返す)

```
UNKNOWN  RECORD_MISSING            その保有の記録が無い
UNKNOWN  OUTSIDE_WINDOW            記録の評価時刻が保有バッチの時間窓の外(同日の手動実行を拾わない)
UNKNOWN  CATEGORY_UNKNOWN          outcome category が語彙(下)に無い(未知は ELIGIBLE に倒さない)
UNKNOWN  EVALUATION_NOT_COMPLETED  category = review / data_insufficient / failed
UNKNOWN  ENGINE_NOT_AUTHORITATIVE  担当エンジン = HOLDING_DECISION_SCORE(SHADOW は根拠にしない)
UNKNOWN  ENGINE_UNKNOWN            担当エンジンが None・未知の値(fail-closed)
UNKNOWN  EVALUATION_NOT_COMPLETED  旧 SELL・利確のどちらかが ran = False
UNKNOWN  RECOMMENDATION_MISSING    記録が指す recommendation_id を引けない
UNKNOWN  TYPE_UNCLASSIFIED         保有側に現れない型(BUY 系)・保有単位でない型・未知の値
BLOCKED  EXIT_RECOMMENDATION       出口(一部・全部売却・売却・緊急確認)
BLOCKED  REVIEW_RECOMMENDATION     要確認(L1 相当 = 投資前提の悪化の確認)
BLOCKED  ATTENTION                 注意・保留(WATCH 系)・attention_origin がある
BLOCKED  NOTIFIED_OR_SUPPRESSED    category = sent / suppressed(通知済み・再通知抑止)
ELIGIBLE NO_EXIT_EVIDENCE          上のどれにも当たらず、category = hold で、指す推奨は HOLD だけ
```

★ `profit_taking_recommendation_id is None` は『出口なし』でも『判定が走らなかった』でもありうる ★
(純粋 HOLD の利確は Recommendation を作らない)。そのため `ran` を併用する。
ran = False の判定は『評価されていない』= UNKNOWN、ran = True かつ id なし = 出口なし。
★ 旧 SELL・利確の**両方**が ran = True であることを要求する ★
(設計 rev2 の『判定のどれかが評価されていない』を fail-closed に読む)。
実データでの ELIGIBLE の到達割合・UNKNOWN の理由別の件数は P-b の観測で測る。
緩めるなら、設計の変更として明示する。

★ この module が持たないもの ★
Recommendation / HoldingEvaluationRecord を引く I/O・時間窓の決め方(= P の入力の組み立て。P-b)。
attention_origin の正本 `resolve_attention_origin_for_recommendation` は
`services/line_notification_service.py`(LINE 送信の module を import する連鎖)にある。
この module はそれを import せず、`recommendation_facts` が**注入された resolver** を呼ぶだけ
にして、P の根から LINE の module へ到達する経路をここでは作らない(正本を domain へ切り出すか、
注入のままにするかは P-b の設計。同値であることは契約テストで固定する)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.entities.enums import RecommendationType, resolve_holding_summary_action
from jstock_advisor.domain.entities.holding_evaluation_record import HoldingEvaluationRecord
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.capital_rotation import SourceEligibility

# --- 語彙(1 か所に固定する) -----------------------------------------------------------------

#: 保有バッチの `authoritative_outcome_category`(`summary_category()` の保有側の戻り値)の語彙
CATEGORY_SENT: Final = "sent"
CATEGORY_HOLD: Final = "hold"
CATEGORY_REVIEW: Final = "review"
CATEGORY_DATA_INSUFFICIENT: Final = "data_insufficient"
CATEGORY_SUPPRESSED: Final = "suppressed"
CATEGORY_FAILED: Final = "failed"
HOLDING_CATEGORIES: Final = frozenset(
    {
        CATEGORY_SENT,
        CATEGORY_HOLD,
        CATEGORY_REVIEW,
        CATEGORY_DATA_INSUFFICIENT,
        CATEGORY_SUPPRESSED,
        CATEGORY_FAILED,
    }
)
_NOT_COMPLETED_CATEGORIES: Final = frozenset(
    {CATEGORY_REVIEW, CATEGORY_DATA_INSUFFICIENT, CATEGORY_FAILED}
)
_EXIT_NOTIFIED_CATEGORIES: Final = frozenset({CATEGORY_SENT, CATEGORY_SUPPRESSED})

#: 担当エンジン(`HoldingEvaluationRecord.authoritative_engine`)
ENGINE_LEGACY_SELL: Final = "LEGACY_SELL"
ENGINE_PROFIT_TAKING: Final = "PROFIT_TAKING"
ENGINE_HOLDING_DECISION_SCORE: Final = "HOLDING_DECISION_SCORE"
_AUTHORITATIVE_ENGINES: Final = frozenset({ENGINE_LEGACY_SELL, ENGINE_PROFIT_TAKING})


class EligibilityReason(StrEnum):
    # UNKNOWN(観測で理由別の件数を数える)
    RECORD_MISSING = "RECORD_MISSING"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    CATEGORY_UNKNOWN = "CATEGORY_UNKNOWN"
    EVALUATION_NOT_COMPLETED = "EVALUATION_NOT_COMPLETED"
    ENGINE_UNKNOWN = "ENGINE_UNKNOWN"
    ENGINE_NOT_AUTHORITATIVE = "ENGINE_NOT_AUTHORITATIVE"
    RECOMMENDATION_MISSING = "RECOMMENDATION_MISSING"
    TYPE_UNCLASSIFIED = "TYPE_UNCLASSIFIED"
    # BLOCKED_BY_EXIT
    EXIT_RECOMMENDATION = "EXIT_RECOMMENDATION"
    REVIEW_RECOMMENDATION = "REVIEW_RECOMMENDATION"
    ATTENTION = "ATTENTION"
    NOTIFIED_OR_SUPPRESSED = "NOTIFIED_OR_SUPPRESSED"
    # ELIGIBLE
    NO_EXIT_EVIDENCE = "NO_EXIT_EVIDENCE"


class RecommendationClass(StrEnum):
    """`RecommendationType` を入替元の適格性の観点で分類した語。"""

    EXIT = "EXIT"  # 出口・緊急確認(正本 resolve_holding_summary_action が None でない)
    REVIEW = "REVIEW"  # 要確認(L1 相当)
    ATTENTION = "ATTENTION"  # 注意・保留
    HOLD = "HOLD"  # 出口なし
    NOT_HOLDING_SIDE = "NOT_HOLDING_SIDE"  # BUY 系。保有側の記録には現れない想定
    NOT_PER_HOLDING = "NOT_PER_HOLDING"  # 保有単位の判定ではない(集中度など)


#: 正本が None を返す型の分類(出口は正本を呼ぶので、ここには持たない)。
#: 全メンバーを明示する契約テストがある
NON_EXIT_CLASSIFICATION: Final[Mapping[RecommendationType, RecommendationClass]] = {
    RecommendationType.HOLD: RecommendationClass.HOLD,
    RecommendationType.REVIEW: RecommendationClass.REVIEW,
    RecommendationType.MANUAL_REVIEW_REQUIRED: RecommendationClass.REVIEW,
    RecommendationType.REVIEW_AFTER_EARNINGS: RecommendationClass.REVIEW,
    RecommendationType.REVIEW_BEFORE_EARNINGS: RecommendationClass.REVIEW,
    RecommendationType.WATCH: RecommendationClass.ATTENTION,
    RecommendationType.WATCH_BEFORE_EARNINGS: RecommendationClass.ATTENTION,
    RecommendationType.BUY: RecommendationClass.NOT_HOLDING_SIDE,
    RecommendationType.WATCH_BUY: RecommendationClass.NOT_HOLDING_SIDE,
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: RecommendationClass.NOT_PER_HOLDING,
}


class EligibilityContractError(ValueError):
    """入力の型が契約に反する(組み立て側の誤り)。値を含めない。"""


def _fail(problem: str) -> EligibilityContractError:
    return EligibilityContractError(problem)


def classify_recommendation_type(recommendation_type: RecommendationType) -> RecommendationClass:
    """RecommendationType の分類。出口は正本を呼ぶ。表に無いメンバーは KeyError(分類漏れの検出)。"""
    if resolve_holding_summary_action(recommendation_type) is not None:
        return RecommendationClass.EXIT
    return NON_EXIT_CLASSIFICATION[recommendation_type]


# --- 入力の型(記録から読んだ事実) -------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityWindow:
    """保有バッチの時間窓(両端を含む)。記録の評価時刻がこの外なら対象外。"""

    start: dt.datetime
    end: dt.datetime

    def __post_init__(self) -> None:
        for value in (self.start, self.end):
            if not isinstance(value, dt.datetime) or value.tzinfo is None:
                raise _fail("window bounds must be timezone-aware datetimes")
        if self.start > self.end:
            raise _fail("window start must not be after its end")

    def contains(self, moment: dt.datetime) -> bool:
        return self.start <= moment <= self.end


@dataclass(frozen=True)
class RecommendationFacts:
    """Recommendation から読んだ事実。attention_origin は呼び出し側が正本で再計算して渡す。"""

    recommendation_type: str
    attention_origin: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.recommendation_type, str):
            raise _fail("recommendation_type must be a string")
        if self.attention_origin is not None and not isinstance(self.attention_origin, str):
            raise _fail("attention_origin must be a string or None")


@dataclass(frozen=True)
class HoldingEvaluationFacts:
    """HoldingEvaluationRecord から読んだ事実(適格性の導出に要る項目だけ)。"""

    evaluated_at: dt.datetime
    authoritative_engine: str | None
    authoritative_outcome_category: str
    authoritative_recommendation_id: str | None
    legacy_sell_ran: bool
    legacy_sell_recommendation_id: str | None
    profit_taking_ran: bool
    profit_taking_recommendation_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.evaluated_at, dt.datetime) or self.evaluated_at.tzinfo is None:
            raise _fail("evaluated_at must be a timezone-aware datetime")
        if self.authoritative_engine is not None and not isinstance(self.authoritative_engine, str):
            raise _fail("authoritative_engine must be a string or None")
        if not isinstance(self.authoritative_outcome_category, str):
            raise _fail("authoritative_outcome_category must be a string")
        for flag in (self.legacy_sell_ran, self.profit_taking_ran):
            if not isinstance(flag, bool):
                raise _fail("ran flags must be bool")
        for identifier in (
            self.authoritative_recommendation_id,
            self.legacy_sell_recommendation_id,
            self.profit_taking_recommendation_id,
        ):
            if identifier is not None and (not isinstance(identifier, str) or not identifier):
                raise _fail("recommendation ids must be non-empty strings or None")

    def recommendation_ids(self) -> tuple[str, ...]:
        """記録が指す recommendation_id(重複なし・昇順)。"""
        ids = {
            identifier
            for identifier in (
                self.authoritative_recommendation_id,
                self.legacy_sell_recommendation_id,
                self.profit_taking_recommendation_id,
            )
            if identifier is not None
        }
        return tuple(sorted(ids))


def facts_from_record(record: HoldingEvaluationRecord) -> HoldingEvaluationFacts:
    """永続された記録 → 事実。項目を写すだけ(値を補わない・変えない)。"""
    return HoldingEvaluationFacts(
        evaluated_at=record.evaluated_at,
        authoritative_engine=record.authoritative_engine,
        authoritative_outcome_category=record.authoritative_outcome_category,
        authoritative_recommendation_id=record.authoritative_recommendation_id,
        legacy_sell_ran=record.legacy_sell_ran,
        legacy_sell_recommendation_id=record.legacy_sell_recommendation_id,
        profit_taking_ran=record.profit_taking_ran,
        profit_taking_recommendation_id=record.profit_taking_recommendation_id,
    )


def recommendation_facts(
    recommendation: Recommendation,
    resolve_attention_origin: Callable[[Recommendation], str | None],
) -> RecommendationFacts:
    """永続された Recommendation → 事実。attention_origin は正本の resolver(注入)で再計算する。"""
    return RecommendationFacts(
        recommendation_type=recommendation.recommendation_type.value,
        attention_origin=resolve_attention_origin(recommendation),
    )


@dataclass(frozen=True)
class EligibilityResult:
    eligibility: SourceEligibility
    reason: EligibilityReason


def _unknown(reason: EligibilityReason) -> EligibilityResult:
    return EligibilityResult(SourceEligibility.UNKNOWN, reason)


def _blocked(reason: EligibilityReason) -> EligibilityResult:
    return EligibilityResult(SourceEligibility.BLOCKED_BY_EXIT, reason)


_BLOCKING_CLASSES: Final[Mapping[RecommendationClass, EligibilityReason]] = {
    RecommendationClass.EXIT: EligibilityReason.EXIT_RECOMMENDATION,
    RecommendationClass.REVIEW: EligibilityReason.REVIEW_RECOMMENDATION,
    RecommendationClass.ATTENTION: EligibilityReason.ATTENTION,
}
#: BLOCKED の理由が複数あるときの優先(出口 > 要確認 > 注意 > 通知済み・抑止。決定的)
_BLOCK_PRIORITY: Final = (
    EligibilityReason.EXIT_RECOMMENDATION,
    EligibilityReason.REVIEW_RECOMMENDATION,
    EligibilityReason.ATTENTION,
    EligibilityReason.NOTIFIED_OR_SUPPRESSED,
)


def derive_source_eligibility(
    record: HoldingEvaluationFacts | None,
    recommendations: Mapping[str, RecommendationFacts],
    window: EligibilityWindow,
) -> EligibilityResult:
    """保有 1 件の入替元の適格性(上の『導出』の順)。純粋関数。★ fail-closed ★

    `recommendations` は、記録が指す recommendation_id から引いた事実(引けなかった id は含めない)。
    """
    if record is None:
        return _unknown(EligibilityReason.RECORD_MISSING)
    if not window.contains(record.evaluated_at):
        return _unknown(EligibilityReason.OUTSIDE_WINDOW)
    category = record.authoritative_outcome_category
    if category not in HOLDING_CATEGORIES:
        return _unknown(EligibilityReason.CATEGORY_UNKNOWN)
    if category in _NOT_COMPLETED_CATEGORIES:
        return _unknown(EligibilityReason.EVALUATION_NOT_COMPLETED)
    engine = record.authoritative_engine
    if engine == ENGINE_HOLDING_DECISION_SCORE:
        return _unknown(EligibilityReason.ENGINE_NOT_AUTHORITATIVE)
    if engine not in _AUTHORITATIVE_ENGINES:  # None・未知の値
        return _unknown(EligibilityReason.ENGINE_UNKNOWN)
    if not (record.legacy_sell_ran and record.profit_taking_ran):
        return _unknown(EligibilityReason.EVALUATION_NOT_COMPLETED)

    blocking: list[EligibilityReason] = []
    for recommendation_id in record.recommendation_ids():
        facts = recommendations.get(recommendation_id)
        if facts is None:
            return _unknown(EligibilityReason.RECOMMENDATION_MISSING)
        try:
            kind = classify_recommendation_type(RecommendationType(facts.recommendation_type))
        except (ValueError, KeyError):  # 未知の値・表に無いメンバー
            return _unknown(EligibilityReason.TYPE_UNCLASSIFIED)
        if kind in (RecommendationClass.NOT_HOLDING_SIDE, RecommendationClass.NOT_PER_HOLDING):
            return _unknown(EligibilityReason.TYPE_UNCLASSIFIED)
        if kind in _BLOCKING_CLASSES:
            blocking.append(_BLOCKING_CLASSES[kind])
        elif facts.attention_origin is not None:
            blocking.append(EligibilityReason.ATTENTION)
    if category in _EXIT_NOTIFIED_CATEGORIES:
        blocking.append(EligibilityReason.NOTIFIED_OR_SUPPRESSED)
    if blocking:
        return _blocked(min(blocking, key=_BLOCK_PRIORITY.index))
    return EligibilityResult(SourceEligibility.ELIGIBLE, EligibilityReason.NO_EXIT_EVIDENCE)

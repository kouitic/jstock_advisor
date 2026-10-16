"""購入側 Shadow の観測の集計(Issue #934 BT-A。純粋・LENIENT・件数と割合だけ)。

CLI(`jstock allocation today`)が読んだ日ごとの画面モデル(`AllocationShadowScreen`)を入力にして、
次の 3 つを作る。**I/O・時計・AWS・config・鍵に触れない**(日付・日の一覧は引数で受ける)。

* `classify_day(screen)` … その日を『10 営業日に数える日』として数えるか(USER 承認済みの暫定ルール)
* `summarize(records)` … 件数・割合・分布(版が変わった日で期間を分ける)
* `build_ten_day_report(summary, expected_days)` … 10 営業日の観測レポート

## 守ること

* ★ 入力は CLI と同じ view model だけ ★: 記録の解釈(6 状態・owner の状態・索引の要約・日単位 skip)を
  集計が別に持たない。集計が CLI と食い違う読み方をするのを構造的に防ぐ(設計 U-1)
* ★ 数える日の規則(#128 の USER 承認済みの暫定ルール)★: 計算できた owner が 1 件以上ある日だけ
  数える。PARTIAL も、計算できた owner が 1 件以上かつ解析不能 0 なら数える。全 owner が
  SKIPPED / FAILED の日・解析不能が 1 件以上の日・EMPTY・NO_INDEX・NO_INDEX_DAY_SKIP・
  UNSUPPORTED・読めなかった日は数えない。PARTIAL は欠落件数
  (= attempted − recorded + 索引にあるが記録の無い符号の数)と母数を必ず残す
* ★ 母数を必ず隣に出す・分母 0 は『測れない』★(0 % と書かない)。10 営業日で『判断可能』と言える
  件数の基準は USER 未承認 = `JUDGEABLE_IN_10_DAYS` はこの module では**付けない**
  (件数と母数だけを出す)
* ★ 版をまたいだ合算をしない ★: 日の版(model_version・policy_id・contract_version・shadow_mode)が
  変わった日で期間を分け、期間ごとに指標を出す
* ★ 出さないもの ★: 銘柄コード・株数・金額・owner の実名・符号の全文・鍵。owner 別の表は
  `include_by_owner=True` のときだけ(公開面へは貼らない。表示名は display_ref = 符号の桁の先頭 6 桁)
* ★ この module が測れないもの ★ は `NOT_MEASURABLE` に理由つきで列挙する(入力の view model に無い・
  gate が到達を禁じている・USER 未承認)。測れないものを 0 や『問題なし』にしない
* 係数・閾値・配分ロジックを参照しない・変更しない(入力は画面モデルだけ)
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    UNKNOWN_TEXT,
    AllocationShadowScreen,
    CandidateRow,
    DayState,
    OwnerSection,
    OwnerStatus,
    Reason,
)

#: 10 営業日の観測の目標日数(USER 承認済み = #128 …6165459615 / #122 …6165408499)
TARGET_COUNTED_DAYS: Final = 10
#: 『数える日』の規則の出所(暫定。規則が変わったらこの行が変わる = 版と同じ扱い)
COUNTING_RULE_SOURCE: Final = (
    "kouitic/jstock_advisor#128 issuecomment-6165459615 の 2 節(USER 承認済みの暫定ルール)"
)
SUMMARY_SCHEMA_VERSION: Final = 1


class NotCounted(StrEnum):
    """数えない日の理由(1 日につき 1 つ。先に当てはまったものを採る)。"""

    READ_FAILED = "READ_FAILED"  # その日の読取に失敗した(種別は DayVerdict.read_failure)
    NOT_READ = "NOT_READ"  # 読んでいない(期待される営業日なのに入力が無い)
    UNSUPPORTED = "UNSUPPORTED"  # 対応していない版・読めない索引
    NO_INDEX_DAY_SKIP = "NO_INDEX_DAY_SKIP"  # 索引なし + 日単位の skip(記録できなかった)
    NO_INDEX = "NO_INDEX"  # 索引なし(未実行 / 途中で止まった)
    EMPTY = "EMPTY"  # 索引あり・記録 0 件
    UNPARSED = "UNPARSED"  # 解釈できない項目が 1 件以上
    NO_COMPUTED_OWNER = "NO_COMPUTED_OWNER"  # 計算できた owner が 0 件(全て SKIPPED / FAILED 等)


class OwnerKind(StrEnum):
    """owner 1 件(1 日分)の結果の種別。画面モデルの owner の状態と outcome から決める。"""

    COMPUTED = "COMPUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"  # 記録はあるが outcome が無い・未知
    MISSING = "MISSING"  # 索引にあるが記録が無い
    UNREADABLE = "UNREADABLE"
    UNSUPPORTED = "UNSUPPORTED"
    REFUSED = "REFUSED"


class Classification(StrEnum):
    """指標の分類(USER 指示 #934 …6168329925)。"""

    #: ★ 付けない ★: 『10 営業日で判断可能』の件数の基準値は USER 未承認(この module は決めない)
    JUDGEABLE_IN_10_DAYS = "JUDGEABLE_IN_10_DAYS"
    NEEDS_BACKTEST = "NEEDS_BACKTEST"  # Shadow の記録だけでは測れない(事後リターン・閾値の妥当性)
    NEEDS_MORE_OBSERVATION = "NEEDS_MORE_OBSERVATION"  # 母数が 0(観測が無い)= 測れない
    UNCLASSIFIED = "UNCLASSIFIED"  # 件数と母数だけを出す(基準が決まるまで分類しない)


@dataclass(frozen=True)
class NotMeasurable:
    deliverable: int  # USER の 10 成果物の番号
    title: str
    reason: str
    classification: Classification


#: この module(PR-1)が測らないもの。★ 0 や『問題なし』と書かない ★(理由つきで列挙する)
NOT_MEASURABLE: Final[tuple[NotMeasurable, ...]] = (
    # ★ U2(PR-1b)で測れるようになった項目 = 3(軸別の欠落・比較キー)・4(寄与度の分布)・
    #   5(業種の偏り)・8(価格の鮮度)は、記録に既にある値から測る(指標 unapplied_axes ほか。
    #   出所のパスは allocation_shadow_cli_view.U2_RECORD_PATHS)。残るのは次の項目 ★
    NotMeasurable(
        4,
        "係数を変えた再計算(感度分析)",
        "★ USER 判断 ★(評価用の係数の組が未承認)。記録された寄与の分布は指標 "
        "deduction_top_axis ほかで測る。#960 の対象",
        Classification.NEEDS_BACKTEST,
    ),
    NotMeasurable(
        7,
        "判断の閾値の近傍にある候補数の分布",
        "config の参照(読取専用)と、記録の config_hash との突合が要る。import の経路を増やす"
        "ため gate の確認後に PR-1b で扱う(参照した config の出所を記録に添える)",
        Classification.UNCLASSIFIED,
    ),
    NotMeasurable(
        7,
        "閾値・係数の妥当性の判定",
        "★ USER 判断 ★(判定基準・評価用の係数の組・事後の評価が未承認)。#960 の対象",
        Classification.NEEDS_BACKTEST,
    ),
    NotMeasurable(
        9,
        "記録の再計算の一致率(replay_items_record)",
        "再計算の module は CLI から到達できない(#606 の gate が禁じている)。gate の変更は"
        " USER 承認の記録を要する",
        Classification.UNCLASSIFIED,
    ),
    NotMeasurable(
        9,
        "事後の値動き(forward return)との比較",
        "★ USER 判断 ★(評価期間・比較基準・コスト控除・外部価格データが未承認)",
        Classification.NEEDS_BACKTEST,
    ),
)


# --- 1 日の判定 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class DayRecord:
    """1 日分の入力。画面モデル(読めた日)か、読めなかった種別(固定の語)のどちらか。"""

    business_date: str
    screen: AllocationShadowScreen | None = None
    read_failure: str | None = None


@dataclass(frozen=True)
class DayVerdict:
    business_date: str
    counted: bool
    not_counted: NotCounted | None
    state: DayState | None
    computed_owners: int
    skipped_owners: int
    failed_owners: int
    other_owners: int
    unparsed: int
    attempted: int | None
    recorded: int | None
    #: 欠落件数 = attempted − recorded + 索引にあるが記録の無い符号の数(索引が無い日は None)
    missing_count: int | None
    read_failure: str | None = None


def owner_kind(owner: OwnerSection) -> OwnerKind:
    """owner の画面モデル → 種別。記録が読めた owner は outcome で決める。"""
    if owner.status is OwnerStatus.MISSING:
        return OwnerKind.MISSING
    if owner.status is OwnerStatus.UNREADABLE:
        return OwnerKind.UNREADABLE
    if owner.status is OwnerStatus.UNSUPPORTED:
        return OwnerKind.UNSUPPORTED
    if owner.status is OwnerStatus.REFUSED:
        return OwnerKind.REFUSED
    code = owner.outcome.code if owner.outcome is not None and owner.outcome.recognized else None
    if code == "COMPUTED":
        return OwnerKind.COMPUTED
    if code == "SKIPPED":
        return OwnerKind.SKIPPED
    if code == "FAILED":
        return OwnerKind.FAILED
    return OwnerKind.OUTCOME_UNKNOWN


def _int_or_none(text: str | None) -> int | None:
    return int(text) if text is not None and text.isascii() and text.isdigit() else None


def classify_day(screen: AllocationShadowScreen) -> DayVerdict:
    """画面モデル → その日の判定。どんな画面でも例外を送出しない。"""
    kinds = Counter(owner_kind(owner) for owner in screen.owners)
    computed = kinds[OwnerKind.COMPUTED]
    skipped = kinds[OwnerKind.SKIPPED]
    failed = kinds[OwnerKind.FAILED]
    other = sum(kinds.values()) - computed - skipped - failed
    attempted = _int_or_none(screen.index.attempted) if screen.index is not None else None
    recorded = _int_or_none(screen.index.recorded) if screen.index is not None else None
    missing = (
        attempted - recorded + len(screen.missing_refs)
        if attempted is not None and recorded is not None
        else None
    )
    not_counted: NotCounted | None
    if screen.state is DayState.UNSUPPORTED:
        not_counted = NotCounted.UNSUPPORTED
    elif screen.state is DayState.NO_INDEX_DAY_SKIP:
        not_counted = NotCounted.NO_INDEX_DAY_SKIP
    elif screen.state is DayState.NO_INDEX:
        not_counted = NotCounted.NO_INDEX
    elif screen.state is DayState.EMPTY:
        not_counted = NotCounted.EMPTY
    elif screen.unparsed >= 1:
        not_counted = NotCounted.UNPARSED
    elif computed == 0:
        not_counted = NotCounted.NO_COMPUTED_OWNER
    else:
        not_counted = None
    return DayVerdict(
        business_date=screen.business_date,
        counted=not_counted is None,
        not_counted=not_counted,
        state=screen.state,
        computed_owners=computed,
        skipped_owners=skipped,
        failed_owners=failed,
        other_owners=other,
        unparsed=screen.unparsed,
        attempted=attempted,
        recorded=recorded,
        missing_count=missing,
    )


def verdict_for_record(record: DayRecord) -> DayVerdict:
    """入力 1 日分 → 判定。画面が無い日(読取の失敗・未読)は数えない。"""
    if record.screen is not None:
        return classify_day(record.screen)
    failed = record.read_failure is not None
    return DayVerdict(
        business_date=record.business_date,
        counted=False,
        not_counted=NotCounted.READ_FAILED if failed else NotCounted.NOT_READ,
        state=None,
        computed_owners=0,
        skipped_owners=0,
        failed_owners=0,
        other_owners=0,
        unparsed=0,
        attempted=None,
        recorded=None,
        missing_count=None,
        read_failure=record.read_failure,
    )


# --- 指標 -----------------------------------------------------------------------------------------

Buckets = tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Measure:
    """1 つの指標。分母(母数)を必ず持つ。分母 0 = 測れない(rate は None)。"""

    key: str
    title: str
    deliverable: int  # USER の 10 成果物の番号
    unit: str  # 母数の単位(owner-日 / 候補 / 日)
    denominator: int
    count: int | None  # 割合の指標の分子(分布の指標は None)
    buckets: Buckets
    classification: Classification


def rate(measure: Measure) -> Decimal | None:
    """割合(0〜1)。分母 0 または割合の指標でなければ None(『測れない』と表示する)。"""
    if measure.count is None or measure.denominator <= 0:
        return None
    return Decimal(measure.count) / Decimal(measure.denominator)


def _buckets(counter: Mapping[str, int]) -> Buckets:
    return tuple(sorted((key, value) for key, value in counter.items() if value > 0))


def _measure(
    key: str,
    title: str,
    deliverable: int,
    unit: str,
    denominator: int,
    *,
    count: int | None = None,
    buckets: Buckets = (),
) -> Measure:
    classification = (
        Classification.NEEDS_MORE_OBSERVATION if denominator <= 0 else Classification.UNCLASSIFIED
    )
    return Measure(key, title, deliverable, unit, denominator, count, buckets, classification)


@dataclass(frozen=True)
class VersionKey:
    """日の版。版が変わった日で期間を分ける(版をまたいだ合算をしない)。"""

    model_version: str | None
    policy_id: str | None
    contract_version: str | None
    shadow_mode: str | None


MIXED_VERSION: Final = VersionKey("MIXED", "MIXED", "MIXED", "MIXED")


def day_version(screen: AllocationShadowScreen) -> VersionKey | None:
    """日の版。記録が読めた owner が無ければ None、owner 間で版が違えば MIXED_VERSION。"""
    keys = {
        VersionKey(o.model_version, o.policy_id, o.contract_version, o.shadow_mode)
        for o in screen.owners
        if o.status is OwnerStatus.OK
    }
    if not keys:
        return None
    return next(iter(keys)) if len(keys) == 1 else MIXED_VERSION


def _code(reason: Reason | None) -> str | None:
    return reason.code if reason is not None else None


def _count_in(counter: Counter[str], reasons: Iterable[Reason | None]) -> None:
    for reason in reasons:
        code = _code(reason)
        if code is not None:
            counter[code] += 1


def _staleness_days(owner: OwnerSection, business_date: str) -> int | None:
    """買付余力の最終照合日 → 対象日までの経過日数(日付が読めなければ None)。"""
    text = owner.last_reconciled_at
    if text is None:
        return None
    try:
        reconciled = dt.date.fromisoformat(text[:10])
        day = dt.date.fromisoformat(business_date)
    except ValueError:
        return None
    return (day - reconciled).days


def _remaining_cash_bucket(text: str | None) -> str:
    """残余資金の目安 → 段階(金額は出さない)。"""
    if text is None:
        return "UNKNOWN"
    body = text.removesuffix("円").replace(",", "")
    if body.startswith("-"):
        return "NEGATIVE"
    return "ZERO" if body.strip("0.") == "" else "POSITIVE"


@dataclass(frozen=True)
class PeriodMetrics:
    """同じ版の連続した期間の指標。版が変われば別の期間(合算しない)。"""

    version: VersionKey
    first_date: str
    last_date: str
    day_count: int  # この期間に属する日数(版が決まった日)
    measures: tuple[Measure, ...]


@dataclass(frozen=True)
class OwnerTotals:
    """owner 別の日数(★ 公開面へ貼らない。include_by_owner=True のときだけ出力する)。"""

    display_ref: str
    days_present: int
    kinds: Buckets


@dataclass(frozen=True)
class ShadowSummary:
    verdicts: tuple[DayVerdict, ...]  # 日付順
    periods: tuple[PeriodMetrics, ...]
    state_counts: Buckets  # 日の状態(読めなかった日は READ_FAILED / NOT_READ)
    not_counted_counts: Buckets
    counted_days: int
    total_days: int
    partial_days: tuple[DayVerdict, ...]  # 欠落件数と母数を必ず残す日
    by_owner: tuple[OwnerTotals, ...]


def _candidate_rows(owners: Iterable[OwnerSection]) -> list[CandidateRow]:
    return [row for owner in owners for row in owner.rows if row.readable]


def _period_measures_base(screens: Sequence[AllocationShadowScreen]) -> tuple[Measure, ...]:
    owners = [o for s in screens for o in s.owners if o.status is OwnerStatus.OK]
    owner_days = [(s.business_date, o) for s in screens for o in s.owners]
    kinds = Counter(owner_kind(o).value for _, o in owner_days)
    ok_pairs = [(d, o) for d, o in owner_days if o.status is OwnerStatus.OK]
    rows = _candidate_rows(owners)

    skip_reasons: Counter[str] = Counter()
    run_statuses: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    incomplete: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    for o in owners:
        _count_in(skip_reasons, [o.skip_reason])
        _count_in(run_statuses, [o.run_status])
        _count_in(unavailable, [o.unavailable_reason])
        _count_in(incomplete, o.incomplete_reasons)
        for reason, number in o.exclusions:
            exclusions[reason.code] += number

    raer_states: Counter[str] = Counter()
    buy_actions: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    ranks: Counter[str] = Counter()
    by_action_available: Counter[str] = Counter()
    by_action_unavailable: Counter[str] = Counter()
    value_present = 0
    for row in rows:
        _count_in(raer_states, [row.raer_state])
        _count_in(buy_actions, [row.buy_action])
        _count_in(decisions, [row.decision])
        _count_in(reasons, [row.reason])
        if row.rank is not None:
            ranks[row.rank] += 1
        if row.raer_value is not None:
            value_present += 1
        action = _code(row.buy_action) or "NO_ACTION"
        state = _code(row.raer_state)
        if state in ("AVAILABLE", "PROVISIONAL"):
            by_action_available[action] += 1
        elif state == "UNAVAILABLE":
            by_action_unavailable[action] += 1

    allocated_per: Counter[str] = Counter()
    remaining: Counter[str] = Counter()
    staleness: Counter[str] = Counter()
    staleness_unavailable: Counter[str] = Counter()
    for date, o in ok_pairs:
        allocated_per[str(o.allocated_count) if o.allocated_count is not None else "NO_ROWS"] += 1
        remaining[_remaining_cash_bucket(o.expected_remaining_cash)] += 1
        days = _staleness_days(o, date)
        label = "UNKNOWN" if days is None else str(days)
        staleness[label] += 1
        if _code(o.run_status) == "UNAVAILABLE":
            staleness_unavailable[label] += 1

    owner_total = len(owner_days)
    ok_total = len(ok_pairs)
    row_total = len(rows)
    available_like = raer_states["AVAILABLE"] + raer_states["PROVISIONAL"]
    truncated = sum(o.candidates_truncated or 0 for o in owners)
    skipped = kinds[OwnerKind.SKIPPED.value]
    action_matrix = Counter[str]()
    for action in set(by_action_available) | set(by_action_unavailable):
        action_matrix[f"{action}:AVAILABLE"] = by_action_available[action]
        action_matrix[f"{action}:UNAVAILABLE"] = by_action_unavailable[action]
    return (
        _measure(
            "owner_kinds",
            "owner-日の結果の種別",
            1,
            "owner-日",
            owner_total,
            buckets=_buckets(kinds),
        ),
        _measure(
            "owner_computed_rate",
            "計算できた owner-日の割合",
            2,
            "owner-日",
            owner_total,
            count=kinds[OwnerKind.COMPUTED.value],
        ),
        _measure(
            "skip_reasons",
            "owner の見送り(SKIPPED)の理由別の件数",
            3,
            "owner-日(SKIPPED)",
            skipped,
            buckets=_buckets(skip_reasons),
        ),
        _measure(
            "run_status",
            "owner-日の配分の実行結果",
            2,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(run_statuses),
        ),
        _measure(
            "unavailable_reasons",
            "算出不能の理由コード別の件数(欠損理由)",
            3,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(unavailable),
        ),
        _measure(
            "raer_states", "候補の RAER の状態", 2, "候補", row_total, buckets=_buckets(raer_states)
        ),
        _measure(
            "raer_available_rate",
            "RAER が算出できた候補の割合(算出 + 暫定)",
            2,
            "候補",
            row_total,
            count=available_like,
        ),
        _measure(
            "raer_unavailable_rate",
            "RAER が算出できなかった候補の割合",
            2,
            "候補",
            row_total,
            count=raer_states["UNAVAILABLE"],
        ),
        _measure(
            "raer_value_present_rate",
            "RAER の値が記録された候補の割合",
            2,
            "候補",
            row_total,
            count=value_present,
        ),
        _measure(
            "buy_actions",
            "候補の購入判定の分布",
            5,
            "候補",
            row_total,
            buckets=_buckets(buy_actions),
        ),
        _measure(
            "decisions",
            "候補の配分結果(配分 / 見送り)",
            5,
            "候補",
            row_total,
            buckets=_buckets(decisions),
        ),
        _measure(
            "allocation_reasons",
            "候補の配分・見送りの理由コード別の件数",
            6,
            "候補",
            row_total,
            buckets=_buckets(reasons),
        ),
        _measure("ranks", "順位別の候補数", 5, "候補", row_total, buckets=_buckets(ranks)),
        _measure(
            "allocated_per_owner_day",
            "owner-日あたりの配分された候補の数の分布",
            5,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(allocated_per),
        ),
        _measure(
            "remaining_cash_stage",
            "残余資金の段階(目安。金額は出さない)",
            5,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(remaining),
        ),
        _measure(
            "exclusions",
            "候補の除外理由別の件数",
            6,
            "候補の除外(件数の合計)",
            sum(exclusions.values()),
            buckets=_buckets(exclusions),
        ),
        _measure(
            "incomplete_reasons",
            "入力の不足の理由別の件数(データ品質)",
            8,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(incomplete),
        ),
        _measure(
            "raer_by_buy_action",
            "購入判定別の RAER の算出可否(属性の偏り)",
            8,
            "候補",
            available_like + raer_states["UNAVAILABLE"],
            buckets=_buckets(action_matrix),
        ),
        _measure(
            "cash_staleness_days",
            "買付余力の最終照合日からの経過日数の分布(データの鮮度)",
            8,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(staleness),
        ),
        _measure(
            "unavailable_by_staleness",
            "経過日数別の算出不能(UNAVAILABLE)の owner-日",
            8,
            "owner-日(記録あり)",
            ok_total,
            buckets=_buckets(staleness_unavailable),
        ),
        _measure(
            "candidates_truncated",
            "記録の上限で切り詰められた候補の件数",
            3,
            "候補",
            row_total + truncated,
            count=truncated,
        ),
    )


_AVAILABLE_STATES: Final = ("AVAILABLE", "PROVISIONAL")


def _price_age_days(business_date: str, price_as_of: str | None) -> int | None:
    """対象日 − 価格の時点(暦日の差)。読めなければ None。"""
    if price_as_of is None:
        return None
    try:
        return (dt.date.fromisoformat(business_date) - dt.date.fromisoformat(price_as_of[:10])).days
    except ValueError:
        return None


def _u2_measures(screens: Sequence[AllocationShadowScreen]) -> tuple[Measure, ...]:
    """#934 U2(PR-1b): 記録に既にある項目の指標。

    軸別の欠落・寄与度・業種の偏り・価格の鮮度・候補の重なり。
    ★ 読むのは画面モデルの候補の行だけ(`readable` な行だけを数える。
    読めなかった行の件数は別に出す)★。
    所有者別には出さない(集約のみ。公開面へは集約の件数だけ)。銘柄コードは数えるだけで出力しない。
    """
    ok_owners = [
        (s.business_date, o) for s in screens for o in s.owners if o.status is OwnerStatus.OK
    ]
    all_rows = [(date, row) for date, o in ok_owners for row in o.rows]
    rows = [(date, row) for date, row in all_rows if row.readable]
    unreadable = len(all_rows) - len(rows)
    row_total = len(rows)
    value_rows = [(date, row) for date, row in rows if row.raer_value is not None]

    unapplied: Counter[str] = Counter()
    keys: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    top_axis: Counter[str] = Counter()
    nonzero_axes: Counter[str] = Counter()
    risk_words: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    verdict_by_state: Counter[str] = Counter()
    ages: Counter[str] = Counter()
    for date, row in rows:
        unapplied.update(set(row.unapplied_axes))
        keys[row.comparison_key or "NONE"] += 1
        reason_codes.update(set(row.raer_reason_codes))
        verdict = row.freshness_verdict or "UNKNOWN"
        verdicts[verdict] += 1
        state = _code(row.raer_state)
        if state in _AVAILABLE_STATES:
            verdict_by_state[f"{verdict}:AVAILABLE"] += 1
        elif state == "UNAVAILABLE":
            verdict_by_state[f"{verdict}:UNAVAILABLE"] += 1
        age = _price_age_days(date, row.price_as_of)
        ages["UNKNOWN" if age is None else str(age)] += 1
    for _, row in value_rows:
        top_axis[row.top_deduction_axis or "NONE"] += 1
        nonzero_axes.update(axis for axis, nonzero in row.contribution_axes if nonzero)
        risk_words.update(set(row.risk_flags))

    # 配分された候補の業種の偏り(owner-日ごとの業種の数。業種が不明な候補は別に数える)
    sector_spread: Counter[str] = Counter()
    allocated_total = allocated_unknown = allocated_days = 0
    for _, owner in ok_owners:
        allocated = [r for r in owner.rows if r.readable and _code(r.decision) == "ALLOCATED"]
        if not allocated:
            continue
        allocated_days += 1
        allocated_total += len(allocated)
        allocated_unknown += sum(1 for r in allocated if r.sector is None)
        sector_spread[f"sectors={len({r.sector for r in allocated if r.sector is not None})}"] += 1

    # 候補の重なり(★ 数えるだけ。銘柄コードは出さない ★)
    # ① 候補 × 日 = 読めた行の件数(所有者の重複を含む)
    # ② 実銘柄数 = 所有者・日をまたいで重複を除く
    # ③ 2 日以上現れた銘柄の数(異なる営業日の数で数える。同じ日に複数の所有者に出ても 1 日)
    days_by_code: dict[str, set[str]] = {}
    for date, row in rows:
        if row.stock_code != UNKNOWN_TEXT:
            days_by_code.setdefault(row.stock_code, set()).add(date)
    overlap = (
        ("candidate_days", row_total),
        ("distinct_stocks", len(days_by_code)),
        ("stocks_on_2plus_days", sum(1 for days in days_by_code.values() if len(days) >= 2)),
    )
    return (
        _measure(
            "unapplied_axes",
            "候補ごとの『適用されなかった軸』の件数",
            3,
            "候補",
            row_total,
            buckets=_buckets(unapplied),
        ),
        _measure(
            "comparison_keys",
            "比較キー(モデル版 + 適用軸)の種類別の候補数",
            3,
            "候補",
            row_total,
            buckets=_buckets(keys),
        ),
        _measure(
            "raer_reason_codes",
            "RAER の理由コード別の候補数(軸ごとの理由を含む)",
            3,
            "候補",
            row_total,
            buckets=_buckets(reason_codes),
        ),
        _measure(
            "deduction_top_axis",
            "減点の主因の軸(寄与が最大の軸)の頻度",
            4,
            "候補(RAER の値あり)",
            len(value_rows),
            buckets=_buckets(top_axis),
        ),
        _measure(
            "contribution_nonzero_axes",
            "軸ごとの寄与が 0 でない候補数",
            4,
            "候補(RAER の値あり)",
            len(value_rows),
            buckets=_buckets(nonzero_axes),
        ),
        _measure(
            "risk_flag_words",
            "RAER で観測されたリスクの語別の候補数",
            4,
            "候補(RAER の値あり)",
            len(value_rows),
            buckets=_buckets(risk_words),
        ),
        _measure(
            "allocated_sector_spread",
            "配分された候補の業種の数の分布(owner-日ごと)",
            5,
            "owner-日(配分あり)",
            allocated_days,
            buckets=_buckets(sector_spread),
        ),
        _measure(
            "allocated_sector_unknown",
            "配分された候補のうち業種が不明な割合",
            5,
            "配分された候補",
            allocated_total,
            count=allocated_unknown,
        ),
        _measure(
            "price_freshness_verdicts",
            "価格の鮮度の語別の候補数",
            8,
            "候補",
            row_total,
            buckets=_buckets(verdicts),
        ),
        _measure(
            "price_freshness_by_raer_state",
            "価格の鮮度の語 × RAER の算出可否",
            8,
            "候補(算出可否が分かるもの)",
            sum(verdict_by_state.values()),
            buckets=_buckets(verdict_by_state),
        ),
        _measure(
            "price_age_days",
            "価格の時点から対象日までの暦日の差の分布",
            8,
            "候補",
            row_total,
            buckets=_buckets(ages),
        ),
        _measure(
            "candidate_overlap",
            "候補の重なり(① 候補×日 ② 実銘柄数 ③ 2 日以上現れた銘柄数)",
            2,
            "候補(読めた行)",
            row_total,
            buckets=overlap,
        ),
        _measure(
            "unreadable_candidate_rows",
            "読めなかった候補の行の件数(上の指標には含めない)",
            3,
            "候補の行(読めた + 読めなかった)",
            len(all_rows),
            count=unreadable,
        ),
    )


def _period_measures(screens: Sequence[AllocationShadowScreen]) -> tuple[Measure, ...]:
    return _period_measures_base(screens) + _u2_measures(screens)


def _periods(screens: Sequence[AllocationShadowScreen]) -> tuple[PeriodMetrics, ...]:
    keyed = [(s, day_version(s)) for s in sorted(screens, key=lambda s: s.business_date)]
    groups: list[tuple[VersionKey, list[AllocationShadowScreen]]] = []
    for screen, version in keyed:
        if version is None:
            continue  # 版が決まらない日(記録の無い日)は指標の期間に属さない(日の集計には入る)
        if groups and groups[-1][0] == version:
            groups[-1][1].append(screen)
        else:
            groups.append((version, [screen]))
    return tuple(
        PeriodMetrics(
            version=version,
            first_date=members[0].business_date,
            last_date=members[-1].business_date,
            day_count=len(members),
            measures=_period_measures(members),
        )
        for version, members in groups
    )


def _owner_totals(screens: Sequence[AllocationShadowScreen]) -> tuple[OwnerTotals, ...]:
    per: dict[str, tuple[str, Counter[str]]] = {}
    for screen in screens:
        for owner in screen.owners:
            _, counter = per.setdefault(owner.ref, (owner.display_ref, Counter()))
            counter[owner_kind(owner).value] += 1
    rows = [
        OwnerTotals(shown, sum(counter.values()), _buckets(counter))
        for shown, counter in per.values()
    ]
    return tuple(sorted(rows, key=lambda r: (r.display_ref, r.kinds)))


def _state_label(verdict: DayVerdict) -> str:
    """日の状態の語。読めなかった日・読んでいない日は、数えない理由の語(READ_FAILED / NOT_READ)。"""
    if verdict.state is not None:
        return verdict.state.value
    return verdict.not_counted.value if verdict.not_counted is not None else "UNKNOWN"


def summarize(records: Sequence[DayRecord]) -> ShadowSummary:
    """日ごとの入力 → 集計。入力の並びに依らない(日付順に並べる)。日付の重複は ValueError。"""
    dates = [r.business_date for r in records]
    if len(set(dates)) != len(dates):
        raise ValueError("business_date が重複している(1 日につき 1 件)")
    ordered = sorted(records, key=lambda r: r.business_date)
    verdicts = tuple(verdict_for_record(r) for r in ordered)
    screens = [r.screen for r in ordered if r.screen is not None]
    states = Counter(_state_label(v) for v in verdicts)
    not_counted = Counter(v.not_counted.value for v in verdicts if v.not_counted is not None)
    return ShadowSummary(
        verdicts=verdicts,
        periods=_periods(screens),
        state_counts=_buckets(states),
        not_counted_counts=_buckets(not_counted),
        counted_days=sum(1 for v in verdicts if v.counted),
        total_days=len(verdicts),
        partial_days=tuple(v for v in verdicts if v.state is DayState.PARTIAL),
        by_owner=_owner_totals(screens),
    )


# --- 10 営業日の観測レポート ------------------------------------------------------------------


@dataclass(frozen=True)
class ImprovementCandidate:
    """改善候補 1 行(USER 指定の 9 項目)。★ 機械的に埋める列 = ①②③④⑨ ★。

    ⑤〜⑧(投資判断への影響・原因仮説・改善の方向性・優先度)は人が書く欄で、
    この module は『初案・未確定』と置くだけ(自動判定しない。分類の確定・採否は #960)。
    """

    summary: str  # ① 問題・改善候補の概要
    facts: str  # ② 観測された事実(件数と母数)
    logic: str  # ③ 対象ロジック(A RAER / B 資金配分 / C 判断品質・データ品質 / D バックテスト評価)
    frequency: str  # ④ 発生頻度・影響範囲
    related_issues: tuple[str, ...]  # ⑨ 関連 Issue
    interpretation: str = (
        "初案・未確定(⑤投資判断への影響 ⑥原因仮説 ⑦改善の方向性 ⑧優先度は人が書く)"
    )


@dataclass(frozen=True)
class TenDayReport:
    target_days: int
    counted_days: int
    reached: bool
    expected_days_total: int
    not_counted_counts: Buckets
    first_counted_date: str | None  # Day 1(最初に数えた日)
    tenth_counted_date: str | None  # 10 日目(数えた日が目標に達したときだけ)
    #: 以後の期待される営業日が全て数える日になる場合の 10 日目(仮定つきの見込み。確定ではない)
    projected_tenth_date: str | None
    partial_days: tuple[DayVerdict, ...]
    periods_count: int  # 版の期間の数(2 以上 = 途中で版が変わった)
    rule_source: str
    verdicts: tuple[DayVerdict, ...]
    candidates: tuple[ImprovementCandidate, ...]
    not_measurable: tuple[NotMeasurable, ...]


_ISSUES_BY_LOGIC: Final[Mapping[str, tuple[str, ...]]] = {
    "A": ("#602", "#934", "#960"),
    "B": ("#603", "#934", "#960"),
    "C": ("#603", "#934", "#960"),
    "D": ("#934", "#960"),
}


def _top(buckets: Buckets, limit: int = 5) -> str:
    ranked = sorted(buckets, key=lambda pair: (-pair[1], pair[0]))[:limit]
    return ", ".join(f"{key}={value}" for key, value in ranked)


def _find(summary: ShadowSummary, key: str) -> list[Measure]:
    return [m for p in summary.periods for m in p.measures if m.key == key]


def _candidates(summary: ShadowSummary) -> tuple[ImprovementCandidate, ...]:
    out: list[ImprovementCandidate] = []
    unavailable = _find(summary, "raer_unavailable_rate")
    un_n = sum(m.count or 0 for m in unavailable)
    un_d = sum(m.denominator for m in unavailable)
    if un_n > 0:
        out.append(
            ImprovementCandidate(
                "RAER が算出できない候補がある",
                f"算出不能 {un_n} / 候補 {un_d}(期間ごとの内訳はレポートの指標を参照)",
                "A",
                f"算出不能の候補 {un_n} 件(期間 {len(unavailable)})",
                _ISSUES_BY_LOGIC["A"],
            )
        )
    reasons = _find(summary, "unavailable_reasons")
    merged: Counter[str] = Counter()
    for m in reasons:
        merged.update(dict(m.buckets))
    if merged:
        out.append(
            ImprovementCandidate(
                "owner-日の算出不能の理由が記録されている",
                f"理由別 {_top(_buckets(merged))}",
                "A",
                f"{sum(merged.values())} owner-日",
                _ISSUES_BY_LOGIC["A"],
            )
        )
    allocation: Counter[str] = Counter()
    for m in _find(summary, "allocation_reasons"):
        allocation.update(dict(m.buckets))
    skipped_reasons = {k: v for k, v in allocation.items() if k != "ALLOCATED"}
    if skipped_reasons:
        out.append(
            ImprovementCandidate(
                "配分しなかった(見送り・制限)理由が記録されている",
                f"理由別 {_top(_buckets(skipped_reasons))}",
                "B",
                f"候補 {sum(skipped_reasons.values())} 件",
                _ISSUES_BY_LOGIC["B"],
            )
        )
    skips: Counter[str] = Counter()
    for m in _find(summary, "skip_reasons"):
        skips.update(dict(m.buckets))
    if skips:
        out.append(
            ImprovementCandidate(
                "owner の計算を見送った(SKIPPED)理由が記録されている",
                f"理由別 {_top(_buckets(skips))}",
                "C",
                f"{sum(skips.values())} owner-日",
                _ISSUES_BY_LOGIC["C"],
            )
        )
    incomplete: Counter[str] = Counter()
    for m in _find(summary, "incomplete_reasons"):
        incomplete.update(dict(m.buckets))
    if incomplete:
        out.append(
            ImprovementCandidate(
                "入力の不足(データ品質)が記録されている",
                f"理由別 {_top(_buckets(incomplete))}",
                "C",
                f"{sum(incomplete.values())} 件",
                _ISSUES_BY_LOGIC["C"],
            )
        )
    if summary.partial_days:
        missing = sum(v.missing_count or 0 for v in summary.partial_days)
        out.append(
            ImprovementCandidate(
                "一部の owner の記録が欠けた日がある(PARTIAL)",
                f"PARTIAL {len(summary.partial_days)} 日・欠落 {missing} 件",
                "C",
                f"{len(summary.partial_days)} / {summary.total_days} 日",
                _ISSUES_BY_LOGIC["C"],
            )
        )
    for reason, number in summary.not_counted_counts:
        out.append(
            ImprovementCandidate(
                "10 営業日に数えない日がある",
                f"{reason} = {number} 日 / 全 {summary.total_days} 日",
                "C",
                f"{number} / {summary.total_days} 日",
                _ISSUES_BY_LOGIC["C"],
            )
        )
    out.append(
        ImprovementCandidate(
            "事後の評価(バックテスト)は未実施",
            "NOT_MEASURABLE の項目(閾値・係数の妥当性・事後の値動きとの比較)",
            "D",
            "全期間",
            _ISSUES_BY_LOGIC["D"],
        )
    )
    return tuple(out)


def build_ten_day_report(
    summary: ShadowSummary,
    expected_days: Sequence[str],
    *,
    target_days: int = TARGET_COUNTED_DAYS,
) -> TenDayReport:
    """集計 → 10 営業日の観測レポート。`expected_days` = 期待される営業日(休日を含まない。日付順)。

    期待される営業日なのに入力の無い日は『未読(NOT_READ)』として数えない日に入れる。
    休日・未実行の日を『0 件』と混同しない(休日は expected_days に含めない)。
    """
    expected = sorted(set(expected_days))
    by_date = {v.business_date: v for v in summary.verdicts}
    verdicts: list[DayVerdict] = []
    for day in expected:
        verdicts.append(by_date[day] if day in by_date else verdict_for_record(DayRecord(day)))
    not_counted = Counter(v.not_counted.value for v in verdicts if v.not_counted is not None)
    counted_dates = [v.business_date for v in verdicts if v.counted]
    last_read = max(
        (v.business_date for v in verdicts if v.not_counted is not NotCounted.NOT_READ),
        default=None,
    )
    remaining_days = [d for d in expected if last_read is None or d > last_read]
    needed = target_days - len(counted_dates)
    projected = (
        None
        if len(counted_dates) >= target_days or needed > len(remaining_days)
        else remaining_days[needed - 1]
    )
    full = ShadowSummary(
        verdicts=tuple(verdicts),
        periods=summary.periods,
        state_counts=summary.state_counts,
        not_counted_counts=_buckets(not_counted),
        counted_days=len(counted_dates),
        total_days=len(verdicts),
        partial_days=tuple(v for v in verdicts if v.state is DayState.PARTIAL),
        by_owner=summary.by_owner,
    )
    return TenDayReport(
        target_days=target_days,
        counted_days=len(counted_dates),
        reached=len(counted_dates) >= target_days,
        expected_days_total=len(expected),
        not_counted_counts=full.not_counted_counts,
        first_counted_date=counted_dates[0] if counted_dates else None,
        tenth_counted_date=counted_dates[target_days - 1]
        if len(counted_dates) >= target_days
        else None,
        projected_tenth_date=projected,
        partial_days=full.partial_days,
        periods_count=len(summary.periods),
        rule_source=COUNTING_RULE_SOURCE,
        verdicts=full.verdicts,
        candidates=_candidates(full),
        not_measurable=NOT_MEASURABLE,
    )


# --- 出力(JSON 化できる辞書。件数と割合だけ)----------------------------------------------------


def _rate_text(measure: Measure) -> str:
    value = rate(measure)
    return "測れない" if value is None else f"{value:.4f}"


def measure_to_dict(measure: Measure) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": measure.key,
        "title": measure.title,
        "deliverable": measure.deliverable,
        "unit": measure.unit,
        "denominator": measure.denominator,
        "classification": measure.classification.value,
    }
    if measure.denominator <= 0:
        out["status"] = "測れない"
    if measure.count is not None:
        out["count"] = measure.count
        out["rate"] = _rate_text(measure)
    if measure.buckets:
        out["buckets"] = {key: value for key, value in measure.buckets}
    return out


def verdict_to_dict(verdict: DayVerdict) -> dict[str, Any]:
    return {
        "business_date": verdict.business_date,
        "counted": verdict.counted,
        "not_counted": verdict.not_counted.value if verdict.not_counted is not None else None,
        "state": verdict.state.value if verdict.state is not None else None,
        "computed_owners": verdict.computed_owners,
        "skipped_owners": verdict.skipped_owners,
        "failed_owners": verdict.failed_owners,
        "other_owners": verdict.other_owners,
        "unparsed": verdict.unparsed,
        "attempted": verdict.attempted,
        "recorded": verdict.recorded,
        "missing_count": verdict.missing_count,
        "read_failure": verdict.read_failure,
    }


def report_to_dict(
    report: TenDayReport, summary: ShadowSummary, *, include_by_owner: bool = False
) -> dict[str, Any]:
    """レポート + 集計 → 辞書。★ owner 別は include_by_owner=True のときだけ(公開面へ貼らない)★。"""
    out: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "counting_rule_source": report.rule_source,
        "target_days": report.target_days,
        "counted_days": report.counted_days,
        "expected_days_total": report.expected_days_total,
        "reached": report.reached,
        "first_counted_date": report.first_counted_date,
        "tenth_counted_date": report.tenth_counted_date,
        "projected_tenth_date_if_all_remaining_days_count": report.projected_tenth_date,
        "not_counted": {key: value for key, value in report.not_counted_counts},
        "versions_periods": report.periods_count,
        "days": [verdict_to_dict(v) for v in report.verdicts],
        "periods": [
            {
                "model_version": p.version.model_version,
                "policy_id": p.version.policy_id,
                "contract_version": p.version.contract_version,
                "shadow_mode": p.version.shadow_mode,
                "first_date": p.first_date,
                "last_date": p.last_date,
                "day_count": p.day_count,
                "measures": [measure_to_dict(m) for m in p.measures],
            }
            for p in summary.periods
        ],
        "improvement_candidates": [
            {
                "summary": c.summary,
                "facts": c.facts,
                "logic": c.logic,
                "frequency": c.frequency,
                "related_issues": list(c.related_issues),
                "interpretation": c.interpretation,
            }
            for c in report.candidates
        ],
        "not_measurable": [
            {
                "deliverable": n.deliverable,
                "title": n.title,
                "reason": n.reason,
                "classification": n.classification.value,
            }
            for n in report.not_measurable
        ],
    }
    if include_by_owner:
        out["by_owner"] = [
            {
                "owner": t.display_ref,
                "days_present": t.days_present,
                "kinds": {key: value for key, value in t.kinds},
            }
            for t in summary.by_owner
        ]
    return out

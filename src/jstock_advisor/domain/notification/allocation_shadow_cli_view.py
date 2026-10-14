"""購入側 Shadow の記録 → CLI の画面モデル(Issue #605 最小 CLI。純粋・LENIENT)。

`jstock allocation today` が読んだ記録(索引・日単位の skip・owner ごとの項目つきの記録)の辞書から、
表示用の不変モデル `AllocationShadowScreen` を作る。**I/O・時計・AWS・config に触れない**。

## 守ること

* ★ LENIENT に読む: どんな辞書(型・キーを崩した入力)でも**例外を送出しない**。解釈できない項目は
  『不明』として数え、黙って省かない(`unparsed`)。未知の schema_version は『対応していない記録の版』
* ★ 記録に無い値を作らない・補完しない・丸めない: 金額・RAER は記録の 10 進数の文字列をそのまま
  整形する(桁区切りだけ)。10 進数として読めない文字列は生の文字列を出し、文字列でも数値でもない値
  (dict / list 等)は『不明』へ畳んで数える。例外は記録された値から作る次の 2 つだけ:
  『残余資金』= 記録された金額を配分の順に差し引いた目安(引き算のみ。読めない値があれば出さない)/
  配分しなかった候補の『必要資金』= 記録された上限後の株数 × 現在価格(掛け算のみ。目安)
* ★ 鍵・符号の計算を持たない: 索引の符号(kv1)を検査して使うだけ(`require_keyed`)。owner の平文は
  扱わない
* ★ 索引の 6 状態を区別して、黙って 0 件にしない: 完全 / 一部未記録 / 記録 0 件 /
  索引なし + skip あり(鍵の取得失敗)/ 索引なし + skip なし / 対応していない版
* 理由コードは閉じた語彙の日本語(未知のコードはコード + 『未対応の理由コード』)。網羅は契約テスト

## 書込側との key の一致

CLI が引く audit_id の形は、この module の定数が持つ(`allocation_shadow_record` は配分・RAER の
module を推移的に import する重い module で、#606 の未接続の網に触れるため import しない)。
書込側(#943 の index_audit_id・day_skip_audit_id・items_audit_id)との一致は契約テストで固定する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from jstock_advisor.domain.entities.identifier_ref import (
    IdentifierRefError,
    parse_keyed_ref,
)

SCHEMA_VERSION: Final = 1  # 読める記録の版(索引・項目つき・skip 共通)
INDEX_KIND: Final = "allocation_shadow_index"
ITEMS_KIND: Final = "allocation_shadow_items"
DAY_SKIP_KIND: Final = "allocation_shadow_day_skip"
MAX_OWNERS: Final = 16  # 索引に載る符号の上限(書込側の MAX_INDEX_OWNERS と一致を契約テストで固定)
UNSUPPORTED_TEXT: Final = "未対応の理由コード"
UNKNOWN_TEXT: Final = "不明"


def index_key(business_date: str) -> str:
    return f"{INDEX_KIND}:{business_date}"


def day_skip_key(business_date: str) -> str:
    return f"{DAY_SKIP_KIND}:{business_date}"


def items_key(business_date: str, ref: str) -> str:
    return f"{ITEMS_KIND}:{business_date}:{ref}"


# --- 理由コードの日本語(閉じた語彙。網羅は契約テストが元の列挙と突き合わせる)------------------

ALLOCATION_REASONS: Final[Mapping[str, str]] = {
    "ALLOCATED": "配分しました",
    "RAER_UNAVAILABLE": "RAER が算出できないため見送り",
    "POLICY_INCOMPLETE": "配分の方針が未決定のため配分しない",
    "BELOW_HURDLE": "RAER が最低基準に届かないため見送り",
    "INSUFFICIENT_CASH_REMAINING": "買付余力が残っていないため見送り",
    "BELOW_MINIMUM_TRADING_UNIT": "売買単位に満たないため見送り",
    "POSITION_AMOUNT_CAP_APPLIED": "1 銘柄の金額上限で制限",
    "POSITION_RATIO_CAP_APPLIED": "1 銘柄の比率上限で制限",
    "SECTOR_RATIO_CAP_APPLIED": "業種の比率上限で制限",
    "SECTOR_UNKNOWN": "業種が不明のため配分しない",
    "HIGH_RISK_ALLOCATION_CAP_APPLIED": "high-risk の比率上限で制限",
    "HIGH_RISK_UNKNOWN": "high-risk の保有額が不明のため配分しない",
    "TIER_CAP_APPLIED": "価格帯の段階上限で制限",
    # 診断用(順位ではない)。日単位の理由(UNAVAILABLE_REASONS の同名)とは別の表で、文も別
    "RAER_NOT_COMPARABLE": "比較できない日のため評価していない(診断。順位ではない)",
}
UNAVAILABLE_REASONS: Final[Mapping[str, str]] = {
    "CASH_NOT_REGISTERED": "買付余力が未登録",
    "PORTFOLIO_VALUE_UNAVAILABLE": "ポートフォリオの評価額が算出できない",
    "RAER_NOT_COMPARABLE": "RAER が比較できない",
    "POLICY_INCOMPLETE": "配分の方針が未決定",
}
SKIP_REASONS: Final[Mapping[str, str]] = {
    "TIME_BUDGET": "Lambda の残り時間が足りない",
    "TIME_LIMIT": "期限内に終わらなかった",
    "CASH_NOT_REGISTERED": "買付余力が未登録",
    "IAM_MISSING": "買付余力を読む権限が未反映",
    "INPUT_INCONSISTENT": "入力が矛盾している",
    "COMPUTE_NOT_IMPLEMENTED": "計算が未実装",
    "COMPUTATION_FAILED": "計算に失敗した",
    "HOLDINGS_READ_FAILED": "保有の読取に失敗した",
    "RECOMMENDATIONS_READ_FAILED": "推奨の読取に失敗した",
    "INPUT_LIMIT_EXCEEDED": "候補と保有の銘柄数が上限を超えた",
    "COEFFICIENTS_UNAVAILABLE": "RAER の係数が読めない",
    "KEY_UNAVAILABLE": "owner の符号の鍵が取れない",
}
KEY_UNAVAILABLE_REASONS: Final[Mapping[str, str]] = {
    "NOT_CONFIGURED": "鍵の設定が無い",
    "ACCESS_DENIED": "鍵を読む権限が無い",
    "NOT_FOUND": "鍵が見つからない",
    "TIMEOUT": "鍵の取得が期限内に終わらなかった",
    "MALFORMED": "鍵の形が不正",
    "ERROR": "鍵の取得で障害",
}
UNRECORDED_REASONS: Final[Mapping[str, str]] = {
    "BUDGET_EXHAUSTED": "時間の予算が尽きた",
    "KEY_UNAVAILABLE": "鍵が取れなかった",
    "WRITE_FAILED": "書込に失敗した",
    "OWNER_CAP": "所有者数の上限を超えた",
    "REFUSED": "記録を拒否した",
}
INCOMPLETE_REASONS: Final[Mapping[str, str]] = {
    "CASH_NOT_REGISTERED": "買付余力が未登録",
    "NO_HOLDINGS": "保有がない",
    "HOLDING_PRICE_MISSING": "保有の価格が取れない銘柄がある",
    "HOLDING_SECTOR_UNKNOWN": "保有の業種が不明の銘柄がある",
    "HOLDING_RISK_UNKNOWN": "保有のリスクの印を観測できない銘柄がある",
}
EXCLUSION_REASONS: Final[Mapping[str, str]] = {
    "DUPLICATE_STOCK_CODE": "銘柄コードが重複",
    "ACTION_MISSING": "購入判定が無い",
    "NOT_BUY_FAMILY": "購入候補の判定ではない",
    "PRICE_UNAVAILABLE": "価格が使えない",
    "TRADING_UNIT_MISMATCH": "売買単位が方針と合わない",
    "HOLDING_VALUE_UNAVAILABLE": "保有の評価額が出せない",
}
RAER_STATES: Final[Mapping[str, str]] = {
    "AVAILABLE": "算出(5 軸)",
    "PROVISIONAL": "暫定",
    "UNAVAILABLE": "算出不能",
}
RUN_STATUSES: Final[Mapping[str, str]] = {
    "ALLOCATED_SOME": "配分あり",
    "NO_ACTION": "購入する候補なし",
    "UNAVAILABLE": "算出不能",
}
OUTCOMES: Final[Mapping[str, str]] = {
    "COMPUTED": "計算できた",
    "SKIPPED": "計算しなかった",
    "FAILED": "計算に失敗した",
}
DECISIONS: Final[Mapping[str, str]] = {"ALLOCATED": "配分", "NOT_ALLOCATED": "見送り"}
BUY_ACTIONS: Final[Mapping[str, str]] = {
    "STRONG_BUY": "積極購入候補",
    "BUY": "購入候補",
    "SMALL_ENTRY": "打診購入候補",
    "WATCH_FOR_PRICE": "監視継続(価格待ち)",
    "WATCH_BEFORE_EARNINGS": "監視継続(決算待ち)",
    "MANUAL_REVIEW": "要確認",
    "NOT_ATTRACTIVE": "購入見送り",
    "EXCLUDED": "対象外",
    "DATA_INSUFFICIENT": "データ不足",
}


@dataclass(frozen=True)
class Reason:
    """理由 = コード + 日本語(未知のコードは『未対応の理由コード』)。"""

    code: str
    text: str


def _reason(table: Mapping[str, str], code: object) -> Reason | None:
    if not isinstance(code, str) or not code:
        return None
    return Reason(code, table.get(code, UNSUPPORTED_TEXT))


# --- 入力(読取の結果。infrastructure の型には依存しない)------------------------------------------


class RecordState(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class RecordInput:
    state: RecordState
    record: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DayInput:
    business_date: str
    index: RecordInput
    skip: RecordInput
    items: Mapping[str, RecordInput] = field(default_factory=dict)  # 符号 → 項目つきの記録


# --- 出力(画面モデル)-----------------------------------------------------------------------------


class DayState(StrEnum):
    COMPLETE = "COMPLETE"  # 1 索引あり・未記録 0
    PARTIAL = "PARTIAL"  # 2 索引あり・未記録あり
    EMPTY = "EMPTY"  # 3 索引あり・記録 0 件
    NO_INDEX_KEY_FAILURE = "NO_INDEX_KEY_FAILURE"  # 4a 索引なし + skip あり(鍵の取得失敗)
    NO_INDEX = "NO_INDEX"  # 4b 索引なし + skip なし
    UNSUPPORTED = "UNSUPPORTED"  # 5 対応していない版・読めない索引


DAY_STATE_TEXT: Final[Mapping[DayState, str]] = {
    DayState.COMPLETE: "すべての所有者の記録があります",
    DayState.PARTIAL: "記録できなかった所有者があります",
    DayState.EMPTY: "Shadow は動きましたが、記録は 0 件です",
    DayState.NO_INDEX_KEY_FAILURE: "鍵の取得に失敗したため、記録がありません(運用の確認が要ります)",
    DayState.NO_INDEX: (
        "その日の索引がありません(Shadow が未実行、または記録の途中で止まった可能性。"
        "記録がある可能性を否定できません)"
    ),
    DayState.UNSUPPORTED: "対応していない記録の版、または読めない索引です",
}


@dataclass(frozen=True)
class CandidateRow:
    seq: str
    stock_code: str
    buy_action: Reason | None
    raer_value: str | None
    raer_state: Reason | None
    confidence: str | None
    rank: str | None
    decision: Reason | None
    shares: str | None
    required_funds: str | None
    shortfall: str | None
    remaining_cash: str | None
    reason: Reason | None
    readable: bool = True


class OwnerStatus(StrEnum):
    OK = "OK"
    MISSING = "MISSING"  # 索引にあるが記録が無い
    UNREADABLE = "UNREADABLE"  # 項目はあるが記録として読めない
    UNSUPPORTED = "UNSUPPORTED"  # 対応していない版
    REFUSED = "REFUSED"  # 符号が kv1 でない(読まない)


@dataclass(frozen=True)
class OwnerSection:
    ref: str
    display_ref: str
    label: str | None
    status: OwnerStatus
    outcome: Reason | None = None
    skip_reason: Reason | None = None
    run_status: Reason | None = None
    unavailable_reason: Reason | None = None
    available_cash: str | None = None
    last_reconciled_at: str | None = None
    portfolio_value: str | None = None
    expected_remaining_cash: str | None = None
    allocated_count: int | None = None
    not_allocated_count: int | None = None
    incomplete_reasons: tuple[Reason, ...] = ()
    exclusions: tuple[tuple[Reason, int], ...] = ()
    model_version: str | None = None
    policy_id: str | None = None
    contract_version: str | None = None
    shadow_mode: str | None = None
    rows: tuple[CandidateRow, ...] = ()
    candidates_truncated: int | None = None
    unparsed: int = 0


@dataclass(frozen=True)
class IndexSummary:
    key_version: str | None
    attempted: str | None
    recorded: str | None
    unrecorded: str | None
    unrecorded_reasons: tuple[tuple[Reason, str], ...]
    owner_refs_truncated: bool | None
    batch_id: str | None = None


@dataclass(frozen=True)
class SkipSummary:
    reason: Reason | None
    key_unavailable_reason: Reason | None
    unrecorded_owner_count: str | None


@dataclass(frozen=True)
class AllocationShadowScreen:
    business_date: str
    state: DayState
    complete: bool
    index: IndexSummary | None
    skip: SkipSummary | None
    owners: tuple[OwnerSection, ...]
    missing_refs: tuple[str, ...]
    schema_version_seen: str | None = None
    unparsed: int = 0
    over_cap_refs: int = 0


# --- LENIENT な読み取りの補助(どれも例外を送出しない)---------------------------------------------


def _dict(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return None


def _group(digits: str) -> str:
    parts: list[str] = []
    while len(digits) > 3:
        parts.append(digits[-3:])
        digits = digits[:-3]
    parts.append(digits)
    return ",".join(reversed(parts))


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def format_decimal(value: object) -> str | None:
    """10 進数の文字列 → 桁区切り(丸めない)。読めない文字列はそのまま、文字列でない値は『不明』。"""
    if value is None:
        return None
    number = _decimal(value)
    if number is None:
        if isinstance(value, str):
            return value
        # 数値(int / float)の repr は害がない。dict / list / その他の構造は記録の中身を
        # 表示面へ出さず『不明』へ畳む(数えるのは _opaque_values)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return repr(value)
        return UNKNOWN_TEXT
    text = format(number, "f")
    sign = "-" if text.startswith("-") else ""
    body = text.lstrip("-")
    whole, _, frac = body.partition(".")
    return f"{sign}{_group(whole)}{'.' + frac if frac else ''}"


def _opaque_values(*values: object) -> int:
    """金額・値の欄に入っていた『文字列でも数値でもない』値の数(dict / list / bool など)。

    表示面には出さず(format_decimal が『不明』へ畳む)、unparsed として数える(黙って省かない)。
    """
    return sum(
        1
        for value in values
        if value is not None
        and (isinstance(value, bool) or not isinstance(value, str | int | float))
    )


def format_yen(value: object) -> str | None:
    text = format_decimal(value)
    return None if text is None else f"{text}円"


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _digest_of(ref: str) -> str:
    return ref.rsplit(":", 1)[-1]


def display_ref(ref: str) -> str:
    """既定の表示名 = 『owner-』+ 符号の桁の先頭 6 桁。"""
    return f"owner-{_digest_of(ref)[:6]}"


def resolve_label(ref: str, labels: Mapping[str, str]) -> str | None:
    """符号の桁の先頭がラベルの key(6 桁以上)に一致すれば、その表示名。最長一致。"""
    digest = _digest_of(ref)
    best: str | None = None
    for prefix in labels:
        if digest.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return labels[best] if best is not None else None


def _supported(record: Mapping[str, Any], kind: str) -> bool:
    return record.get("schema_version") == SCHEMA_VERSION and record.get("record_kind") == kind


# --- 候補の行 ------------------------------------------------------------------------------------


def _candidate_row(raw: object, cash_left: Decimal | None) -> tuple[CandidateRow, Decimal | None]:
    """候補 1 件。cash_left = ここまでの残余(記録の金額の引き算。読めなければ None のまま)。"""
    candidate = _dict(raw)
    if not candidate:
        return (
            CandidateRow(
                "-",
                UNKNOWN_TEXT,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                readable=False,
            ),
            cash_left,
        )
    attrs = _dict(candidate.get("attrs"))
    price = _dict(candidate.get("price"))
    raer = _dict(candidate.get("raer_out"))
    allocation = _dict(candidate.get("allocation"))
    decision = _reason(DECISIONS, allocation.get("decision")) if allocation else None
    shares = _count(allocation.get("shares")) if allocation else None
    allocated = decision is not None and decision.code == "ALLOCATED"
    amount_raw = allocation.get("amount") if allocation else None
    required: str | None = None
    remaining: str | None = None
    left = cash_left
    if allocated:
        required = format_yen(amount_raw)
        amount = _decimal(amount_raw)
        if left is not None and amount is not None:
            left = left - amount
            remaining = format_yen(format(left, "f"))
        else:
            left = None
    elif allocation:
        capped = _count(allocation.get("cap_limited_shares"))
        unit_price = _decimal(price.get("current_price"))
        if capped is not None and unit_price is not None and capped > 0:
            required = format_yen(format(unit_price * capped, "f"))
    shortfall_raw = allocation.get("cash_shortfall") if allocation else None
    shortfall_number = _decimal(shortfall_raw)
    shortfall = (
        format_yen(shortfall_raw)
        if shortfall_raw is not None and (shortfall_number is None or shortfall_number > 0)
        else None
    )
    rank = _count(allocation.get("rank")) if allocation else None
    value = raer.get("value")
    row = CandidateRow(
        seq=_int_text(candidate.get("seq")) or "-",
        stock_code=_str(candidate.get("stock_code")) or UNKNOWN_TEXT,
        buy_action=_reason(BUY_ACTIONS, attrs.get("buy_action")),
        raer_value=format_decimal(value) if value is not None else None,
        raer_state=_reason(RAER_STATES, raer.get("state")),
        confidence=_str(raer.get("confidence")),
        rank=str(rank) if rank is not None else None,
        decision=decision,
        shares=str(shares) if shares is not None and allocated else None,
        required_funds=required,
        shortfall=shortfall,
        remaining_cash=remaining,
        reason=_reason(ALLOCATION_REASONS, allocation.get("reason_code")) if allocation else None,
    )
    return row, left


def _reasons(table: Mapping[str, str], codes: object) -> tuple[Reason, ...]:
    out = [r for code in _list(codes) if (r := _reason(table, code)) is not None]
    return tuple(out)


def _count_map(table: Mapping[str, str], raw: object) -> tuple[tuple[Reason, int], ...]:
    out: list[tuple[Reason, int]] = []
    for code, count in _dict(raw).items():
        reason = _reason(table, code)
        number = _count(count)
        if reason is not None and number is not None:
            out.append((reason, number))
    return tuple(sorted(out, key=lambda pair: pair[0].code))


def _owner(ref: str, record_input: RecordInput | None, labels: Mapping[str, str]) -> OwnerSection:
    label = resolve_label(ref, labels)
    shown = display_ref(ref)
    if record_input is None:
        return OwnerSection(ref, shown, label, OwnerStatus.REFUSED, unparsed=1)
    if record_input.state is RecordState.NOT_FOUND:
        return OwnerSection(ref, shown, label, OwnerStatus.MISSING)
    record = record_input.record
    if record_input.state is RecordState.UNREADABLE or not isinstance(record, dict):
        return OwnerSection(ref, shown, label, OwnerStatus.UNREADABLE, unparsed=1)
    if not _supported(record, ITEMS_KIND):
        return OwnerSection(ref, shown, label, OwnerStatus.UNSUPPORTED, unparsed=1)
    try:
        return _decode_owner(ref, shown, label, record)
    except Exception:  # noqa: BLE001 - どんな辞書でも例外を出さない(読めなければ『不明』)
        return OwnerSection(ref, shown, label, OwnerStatus.UNREADABLE, unparsed=1)


def _decode_owner(
    ref: str, shown: str, label: str | None, record: Mapping[str, Any]
) -> OwnerSection:
    run = _dict(record.get("run"))
    counts = _dict(record.get("counts"))
    cash = _dict(record.get("cash"))
    portfolio = _dict(record.get("portfolio"))
    model = _dict(record.get("model"))
    cash_value = _decimal(cash.get("available_cash"))
    rows: list[CandidateRow] = []
    unparsed = 0
    left = cash_value
    unparsed += _opaque_values(cash.get("available_cash"), portfolio.get("portfolio_value"))
    for raw in _list(record.get("candidates")):
        row, left = _candidate_row(raw, left)
        rows.append(row)
        if not row.readable:
            unparsed += 1
        else:
            candidate = _dict(raw)
            unparsed += _opaque_values(
                _dict(candidate.get("allocation")).get("amount"),
                _dict(candidate.get("allocation")).get("cash_shortfall"),
                _dict(candidate.get("raer_out")).get("value"),
            )
    allocated = sum(1 for r in rows if r.decision is not None and r.decision.code == "ALLOCATED")
    not_allocated = sum(
        1 for r in rows if r.decision is not None and r.decision.code == "NOT_ALLOCATED"
    )
    expected_left = (
        format_yen(format(left, "f"))
        if left is not None and cash_value is not None and rows
        else None
    )
    outcome = _reason(OUTCOMES, run.get("outcome"))
    return OwnerSection(
        ref=ref,
        display_ref=shown,
        label=label,
        status=OwnerStatus.OK,
        outcome=outcome,
        skip_reason=_reason(SKIP_REASONS, run.get("skip_reason")),
        run_status=_reason(RUN_STATUSES, run.get("status")),
        unavailable_reason=_reason(UNAVAILABLE_REASONS, run.get("unavailable_reason")),
        available_cash=format_yen(cash.get("available_cash")),
        last_reconciled_at=_str(cash.get("last_reconciled_at")),
        portfolio_value=format_yen(portfolio.get("portfolio_value")),
        expected_remaining_cash=expected_left,
        allocated_count=allocated if rows else None,
        not_allocated_count=not_allocated if rows else None,
        incomplete_reasons=_reasons(INCOMPLETE_REASONS, counts.get("incomplete_reasons")),
        exclusions=_count_map(EXCLUSION_REASONS, counts.get("exclusions")),
        model_version=_str(model.get("raer_model_version")),
        policy_id=_str(model.get("policy_id")),
        contract_version=_str(model.get("contract_version")),
        shadow_mode=_str(model.get("shadow_mode")),
        rows=tuple(rows),
        candidates_truncated=_count(counts.get("candidates_truncated")),
        unparsed=unparsed,
    )


# --- 索引・skip --------------------------------------------------------------------------------


def _index_summary(index: Mapping[str, Any]) -> IndexSummary:
    reasons: list[tuple[Reason, str]] = []
    for code, count in sorted(_dict(index.get("unrecorded_reasons")).items()):
        reason = _reason(UNRECORDED_REASONS, code)
        number = _int_text(count)
        if reason is not None:
            reasons.append((reason, number if number is not None else UNKNOWN_TEXT))
    truncated = index.get("owner_refs_truncated")
    key_version = index.get("key_version")
    return IndexSummary(
        key_version=_int_text(key_version),
        attempted=_int_text(index.get("attempted_owner_count")),
        recorded=_int_text(index.get("recorded_owner_count")),
        unrecorded=_int_text(index.get("unrecorded_owner_count")),
        unrecorded_reasons=tuple(reasons),
        owner_refs_truncated=truncated if isinstance(truncated, bool) else None,
        batch_id=_str(index.get("batch_id")),
    )


def _skip_summary(skip: Mapping[str, Any]) -> SkipSummary:
    return SkipSummary(
        reason=_reason(SKIP_REASONS, skip.get("reason")),
        key_unavailable_reason=_reason(KEY_UNAVAILABLE_REASONS, skip.get("key_unavailable_reason")),
        unrecorded_owner_count=_int_text(skip.get("unrecorded_owner_count")),
    )


def _decoded_skip(skip: RecordInput) -> SkipSummary | None:
    record = skip.record
    if skip.state is not RecordState.FOUND or not isinstance(record, dict):
        return None
    if not _supported(record, DAY_SKIP_KIND):
        return None
    try:
        return _skip_summary(record)
    except Exception:  # noqa: BLE001
        return None


def index_refs(index: Mapping[str, Any]) -> tuple[list[str], int]:
    """索引の符号のうち、kv1 の形のもの(最大 MAX_OWNERS)と、上限を超えて無視した件数。"""
    refs = [r for r in _list(index.get("owner_refs")) if isinstance(r, str)]
    return refs[:MAX_OWNERS], max(0, len(refs) - MAX_OWNERS)


def _is_keyed(ref: str) -> bool:
    try:
        parse_keyed_ref(ref)  # kv1 の形(kv0・壊れた形式・版なしは拒否)
    except IdentifierRefError:
        return False
    return True


def refs_to_read(index: RecordInput) -> list[str]:
    """CLI が項目つきの記録を引く符号(索引が読めて対応する版で、kv1 の形のもの)。例外を出さない。"""
    record = index.record
    if index.state is not RecordState.FOUND or not isinstance(record, dict):
        return []
    if not _supported(record, INDEX_KIND):
        return []
    try:
        refs, _ = index_refs(record)
    except Exception:  # noqa: BLE001
        return []
    return [ref for ref in refs if _is_keyed(ref)]


def build_screen(day: DayInput, labels: Mapping[str, str] | None = None) -> AllocationShadowScreen:
    """読んだ記録 → 画面。どんな入力でも例外を送出しない。"""
    try:
        return _build(day, labels or {})
    except Exception:  # noqa: BLE001 - 最後の砦。読めなかった日として『不明』を出す
        return AllocationShadowScreen(
            business_date=day.business_date if isinstance(day.business_date, str) else "-",
            state=DayState.UNSUPPORTED,
            complete=False,
            index=None,
            skip=None,
            owners=(),
            missing_refs=(),
            unparsed=1,
        )


def _build(day: DayInput, labels: Mapping[str, str]) -> AllocationShadowScreen:
    skip = _decoded_skip(day.skip)
    index = day.index
    record = index.record
    if index.state is RecordState.NOT_FOUND:
        state = DayState.NO_INDEX_KEY_FAILURE if skip is not None else DayState.NO_INDEX
        return AllocationShadowScreen(day.business_date, state, False, None, skip, (), ())
    if index.state is RecordState.UNREADABLE or not isinstance(record, dict):
        return AllocationShadowScreen(
            day.business_date, DayState.UNSUPPORTED, False, None, skip, (), (), unparsed=1
        )
    version = record.get("schema_version")
    seen = (
        str(version) if isinstance(version, int | str) and not isinstance(version, bool) else None
    )
    if not _supported(record, INDEX_KIND):
        return AllocationShadowScreen(
            day.business_date,
            DayState.UNSUPPORTED,
            False,
            None,
            skip,
            (),
            (),
            schema_version_seen=seen,
            unparsed=1,
        )
    summary = _index_summary(record)
    refs, over_cap = index_refs(record)
    owners: list[OwnerSection] = []
    missing: list[str] = []
    unparsed = 0
    for ref in refs:
        keyed = _is_keyed(ref)
        section = _owner(ref, day.items.get(ref) if keyed else None, labels)
        owners.append(section)
        unparsed += section.unparsed
        if section.status is OwnerStatus.MISSING:
            missing.append(ref)
    recorded = _count(record.get("recorded_owner_count"))
    unrecorded = _count(record.get("unrecorded_owner_count"))
    attempted = _count(record.get("attempted_owner_count"))
    # 索引の件数が互いに整合しているときだけ『完全』と言える(書込側は attempted = recorded +
    # unrecorded、recorded = 載せた符号の数を保証する。壊れた・別経路の索引はここで PARTIAL)
    consistent = (
        recorded is not None
        and unrecorded is not None
        and attempted is not None
        and attempted == recorded + unrecorded
        and recorded == len(_list(record.get("owner_refs")))
    )
    if recorded == 0 and not refs:
        state = DayState.EMPTY
    elif (unrecorded or 0) > 0 or over_cap > 0 or not consistent or missing:
        state = DayState.PARTIAL
    else:
        state = DayState.COMPLETE
    complete = state is DayState.COMPLETE and unparsed == 0
    return AllocationShadowScreen(
        business_date=day.business_date,
        state=state,
        complete=complete,
        index=summary,
        skip=skip,
        owners=tuple(owners),
        missing_refs=tuple(missing),
        schema_version_seen=seen,
        unparsed=unparsed,
        over_cap_refs=over_cap,
    )

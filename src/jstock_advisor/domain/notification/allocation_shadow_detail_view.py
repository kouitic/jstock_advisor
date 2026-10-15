"""購入側 Shadow の記録 → 『候補ごとの詳細』の画面モデル(Issue #605 追加 CLI。純粋・LENIENT)。

`jstock allocation detail` が読んだ記録(`today` と同じ索引・日単位の skip・owner ごとの項目つきの
記録)から、候補ごとの属性・価格・RAER の入力と出力の内訳・配分の判断・方針の値を、**記録された値の
まま**表示用の不変モデルへ写す。**I/O・時計・AWS・config に触れない**。
設計: #605 追加 CLI 設計 rev1〜rev3(…6185126921・…6187277235・…6188870271)・
サブちゃんの設計レビュー PASS(…6185799547・…6189256361)。

## 守ること(契約テストで固定する)

* ★ 記録に無い値を作らない・補完しない・丸めない・計算し直さない ★: 寄与度・土台・配分の金額は
  記録の 10 進数の文字列をそのまま(桁区切りだけ)整形する。寄与の合計・順位・残余を再計算しない。
  ★ `today` の行が持つ 2 つの導出値(『残余資金』の目安 = 記録の金額の引き算・配分しなかった候補の
  『必要資金』 = 株数 × 現在価格の掛け算)は、詳細では出さない ★(USER の指示 #122 …6195577047:
  記録されている値を表示するだけで、CLI 側の再計算・推定・補完は禁止)。必要資金は配分した候補の
  記録の金額だけ
* ★ LENIENT かつ『生の値が画面へ出ない』★: どんな辞書(型・キー・値の形を崩した入力)でも例外を
  送出しない。allowlist のキーだけを読み、値は形(10 進数・コード・語・識別子・真偽・日付)を
  検査して、形が合うものだけを出す。合わないもの(未知の型・自由文・構造・巨大な値)は『不明』へ畳み、
  件数(unparsed)に数える。★ 解釈できなかった値を `repr` / `str` で画面へ落とさない ★
  (未知のキーは読まない = 出ない)
* ★ 記録の組み立て・計算の module(書込側)を import しない ★(配分 / RAER 系・
  方針の config は #606 の網に掛かり、CLI の根には免除が無い)。
  方針の値は記録の `policy` の節から読む。キー名の一覧は本 module が自前で持ち、書込側の公開定数
  (`RAER_IN_KEYS`・`RAER_EXTRA_KEYS`)との一致は契約テストが固定する(書込側の module は変更しない
  = R1 の記録契約に触れない)
* 閉包は『自分自身 + `allocation_shadow_cli_view`(+ その既存の依存)』で閉じる
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    ALLOCATION_REASONS,
    ITEMS_KIND,
    UNKNOWN_TEXT,
    AllocationShadowScreen,
    CandidateRow,
    DayInput,
    OwnerSection,
    OwnerStatus,
    RecordState,
    _dict,
    _list,
    build_screen,
    format_decimal,
    format_yen,
)

# --- 記録のキー名(allowlist。書込側の公開定数との一致は契約テストが固定する)------------------

RAER_IN_KEYS: Final = (
    "upside_pct",
    "income_pct",
    "fair_value_confidence",
    "quality_score",
    "quality_coverage_ratio",
    "quality_min_coverage",
    "trend_score",
    "trend_partial_coverage",
    "trend_components_used",
    "trend_weights",
    "risk_flags_observed",
    "company_quality_model_version",
    "earnings_trend_model_version",
)
RAER_EXTRA_KEYS: Final = (
    "coefficient_set_id",
    "contributions",
    "risk_contributions",
    "base",
    "spec_flags",
)

#: 値の形(書込側の検査と同じ形。読取側でも検査し、合わないものは表示面へ出さない)
_IDENT: Final = re.compile(r"^[A-Za-z0-9_.:|+,\-]{1,200}$")
_CODE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_REASON: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}(:[A-Z][A-Z0-9_]{0,63})?$")
_WORD: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DEC: Final = re.compile(r"^-?[0-9]{1,30}(\.[0-9]{1,30})?$")
_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_LIST: Final = 64  # 画面に出す要素の上限(超えた分は件数だけ数える)
#: 『today』の行(CandidateRow)の文字列を、画面へ出す前に再検査する形(桁区切りの数・円・整数)
_SHOWN_NUMBER: Final = re.compile(r"^-?[0-9]{1,3}(,[0-9]{3})*(\.[0-9]+)?$|^-?[0-9]+(\.[0-9]+)?$")
_SHOWN_YEN: Final = re.compile(r"^-?[0-9]{1,3}(,[0-9]{3})*(\.[0-9]+)?円$|^-?[0-9]+(\.[0-9]+)?円$")
_SHOWN_INT: Final = re.compile(r"^[0-9]{1,18}$")
_SHOWN_AT: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?)?$"
)

#: 『配分した』を表す記録の判断コード(書込側の閉じた語彙。契約テストが CandidateDecision と結ぶ)
_ALLOCATED: Final = "ALLOCATED"

YES_TEXT: Final = "はい"
NO_TEXT: Final = "いいえ"

#: `--decision` の指定語 → 記録の判断コード(None = 絞り込まない)
DECISION_FILTERS: Final[Mapping[str, str | None]] = {
    "all": None,
    "allocated": "ALLOCATED",
    "not-allocated": "NOT_ALLOCATED",
}
#: `--reason` が受け付ける語彙 = 閉じた理由コードの表(`today` と同じ。手で写した一覧を持たない)
REASON_FILTER_CODES: Final = tuple(sorted(ALLOCATION_REASONS))


# --- 画面モデル --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DetailValue:
    """表示する 1 項目。text = 検査を通った記録の値。unknown = 解釈できなかった値。"""

    key: str
    text: str | None = None
    unknown: bool = False


@dataclass(frozen=True)
class DetailList:
    key: str
    items: tuple[str, ...] = ()
    unknown_count: int = 0  # 解釈できなかった要素(形が違う)・上限を超えた要素の数
    present: bool = False  # 記録に項目があった(空のリストと『無い』を区別する)


@dataclass(frozen=True)
class DetailMap:
    key: str
    pairs: tuple[tuple[str, str], ...] = ()
    unknown_count: int = 0
    present: bool = False


@dataclass(frozen=True)
class CandidateDetail:
    """候補 1 件の詳細。row = `today` と同じ行(連番・銘柄・RAER・順位・判断・株数・資金・理由)。"""

    row: CandidateRow
    attributes: tuple[
        DetailValue, ...
    ] = ()  # kind・価格帯・確信度・質の帯・業種・high-risk・保有評価額
    price: tuple[DetailValue, ...] = ()  # 現在値・価格日・鮮度の判定
    raer_in: tuple[DetailValue | DetailList | DetailMap, ...] = ()
    raer_out: tuple[DetailValue | DetailList | DetailMap, ...] = ()
    raer_extra: tuple[DetailValue | DetailList | DetailMap, ...] = ()
    allocation: tuple[DetailValue, ...] = ()  # 上限で制限された株数・価格帯の段階・現金不足
    unparsed: int = 0


@dataclass(frozen=True)
class PolicyDetail:
    """記録の `policy` の節(方針の値。config は読まない)。"""

    values: tuple[DetailValue | DetailList | DetailMap, ...] = ()
    unparsed: int = 0


@dataclass(frozen=True)
class OwnerDetail:
    ref: str
    display_ref: str
    label: str | None
    candidates: tuple[CandidateDetail, ...] = ()
    policy: PolicyDetail | None = None
    run: tuple[DetailValue | DetailMap, ...] = ()  # 経過時間・読取の状態
    unparsed: int = 0
    # 所有者の見出し: 記録された値だけ(買付余力・基準日時・ポートフォリオ評価額)。
    # 『見込み残高(目安)』『配分 / 見送りの件数』は導出(引き算・行から数えた値)なので出さない
    available_cash: str | None = None
    last_reconciled_at: str | None = None
    portfolio_value: str | None = None


@dataclass(frozen=True)
class DetailScreen:
    screen: AllocationShadowScreen
    owners: tuple[OwnerDetail, ...] = ()
    filter_text: str | None = None  # 絞り込みの説明(絞り込んだときだけ)
    total_candidates: int = 0
    shown_candidates: int = 0

    @property
    def unparsed(self) -> int:
        return self.screen.unparsed + sum(
            owner.unparsed + sum(c.unparsed for c in owner.candidates) for owner in self.owners
        )


@dataclass(frozen=True)
class DetailFilter:
    owner_prefix: str | None = None
    stock_code: str | None = None
    decision: str | None = None  # 記録の判断コード(ALLOCATED / NOT_ALLOCATED)。None = 絞り込まない
    reason: str | None = None  # 記録の理由コード
    description: tuple[str, ...] = field(default=())


# --- 値の読み取り(形を検査し、合うものだけを出す。どれも例外を送出しない)-------------------------


def _shaped(pattern: re.Pattern[str], raw: object) -> str | None:
    return raw if isinstance(raw, str) and pattern.fullmatch(raw) is not None else None


def _scalar(key: str, raw: object, kind: str) -> tuple[DetailValue, int]:
    """1 項目。kind = dec / code / reason / word / ident / bool / date / count / yen。"""
    if raw is None:
        return DetailValue(key), 0
    text: str | None
    if kind == "dec":
        text = format_decimal(raw) if _shaped(_DEC, raw) is not None else None
    elif kind == "yen":
        text = format_yen(raw) if _shaped(_DEC, raw) is not None else None
    elif kind == "code":
        text = _shaped(_CODE, raw)
    elif kind == "reason":
        text = _shaped(_REASON, raw)
    elif kind == "word":
        text = _shaped(_WORD, raw)
    elif kind == "ident":
        text = _shaped(_IDENT, raw)
    elif kind == "date":
        text = _shaped(_DATE, raw)
    elif kind == "bool":
        text = (YES_TEXT if raw else NO_TEXT) if isinstance(raw, bool) else None
    elif kind == "count":
        text = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
    else:  # pragma: no cover - 呼び出し側の誤り
        text = None
    if text is None:
        return DetailValue(key, UNKNOWN_TEXT, unknown=True), 1
    return DetailValue(key, text), 0


def _items(key: str, raw: object, kind: str) -> tuple[DetailList, int]:
    if raw is None:
        return DetailList(key), 0
    if not isinstance(raw, list):
        return DetailList(key, (UNKNOWN_TEXT,), 1, present=True), 1
    shown: list[str] = []
    unknown = 0
    pattern = {"word": _WORD, "code": _CODE, "reason": _REASON, "ident": _IDENT}[kind]
    for item in raw[:_MAX_LIST]:
        text = _shaped(pattern, item)
        if text is None:
            unknown += 1
            shown.append(UNKNOWN_TEXT)
        else:
            shown.append(text)
    over = max(0, len(raw) - _MAX_LIST)
    return DetailList(key, tuple(shown), unknown + over, present=True), unknown + over


def _decimal_map(key: str, raw: object) -> tuple[DetailMap, int]:
    if raw is None:
        return DetailMap(key), 0
    if not isinstance(raw, dict):
        return DetailMap(key, ((UNKNOWN_TEXT, UNKNOWN_TEXT),), 1, present=True), 1
    pairs: list[tuple[str, str]] = []
    unknown = 0
    for index, (name, value) in enumerate(sorted(raw.items(), key=lambda kv: str(kv[0]))):
        if index >= _MAX_LIST:
            unknown += 1
            continue
        key_text = _shaped(_IDENT, name)
        value_text = format_decimal(value) if _shaped(_DEC, value) is not None else None
        if key_text is None or value_text is None:
            unknown += 1
            pairs.append((key_text or UNKNOWN_TEXT, value_text or UNKNOWN_TEXT))
        else:
            pairs.append((key_text, value_text))
    return DetailMap(key, tuple(pairs), unknown, present=True), unknown


_RAER_IN_KINDS: Final[Mapping[str, str]] = {
    "upside_pct": "dec",
    "income_pct": "dec",
    "fair_value_confidence": "code",
    "quality_score": "dec",
    "quality_coverage_ratio": "dec",
    "quality_min_coverage": "dec",
    "trend_score": "dec",
    "trend_partial_coverage": "dec",
    "trend_components_used": "list:word",
    "trend_weights": "map",
    "risk_flags_observed": "bool",
    "company_quality_model_version": "ident",
    "earnings_trend_model_version": "ident",
}
_RAER_EXTRA_KINDS: Final[Mapping[str, str]] = {
    "coefficient_set_id": "ident",
    "contributions": "map",
    "risk_contributions": "map",
    "base": "map",
    "spec_flags": "list:code",
}


def _typed(key: str, raw: object, kind: str) -> tuple[DetailValue | DetailList | DetailMap, int]:
    if kind == "map":
        return _decimal_map(key, raw)
    if kind.startswith("list:"):
        return _items(key, raw, kind.split(":", 1)[1])
    return _scalar(key, raw, kind)


def _section(
    source: Mapping[str, Any], kinds: Mapping[str, str]
) -> tuple[tuple[DetailValue | DetailList | DetailMap, ...], int]:
    out: list[DetailValue | DetailList | DetailMap] = []
    unparsed = 0
    for key, kind in kinds.items():  # allowlist のキーだけを読む(未知のキーは読まない = 出ない)
        value, bad = _typed(key, source.get(key), kind)
        out.append(value)
        unparsed += bad
    return tuple(out), unparsed


def _shown(pattern: re.Pattern[str], text: str | None) -> tuple[str | None, int]:
    """『today』の行の文字列を再検査する。形が合わなければ『不明』(生の文字列を出さない)。"""
    if text is None:
        return None, 0
    if pattern.fullmatch(text) is not None:
        return text, 0
    return UNKNOWN_TEXT, 1


def _clean_row(row: CandidateRow) -> tuple[CandidateRow, int]:
    """行の自由な文字列(銘柄・数値・金額)を、画面へ出す前に形で検査する(詳細は『生の値が出ない』)。"""
    if not row.readable:
        return row, 0
    stock, bad_stock = (
        (row.stock_code, 0)
        if row.stock_code == UNKNOWN_TEXT or _IDENT.fullmatch(row.stock_code) is not None
        else (UNKNOWN_TEXT, 1)
    )
    seq, bad_seq = _shown(_SHOWN_INT, row.seq if row.seq != "-" else None)
    raer, bad_raer = _shown(_SHOWN_NUMBER, row.raer_value)
    allocated = row.decision is not None and row.decision.code == _ALLOCATED
    rank, bad_rank = _shown(_SHOWN_INT, row.rank)
    shares, bad_shares = _shown(_SHOWN_INT, row.shares)
    # 必要資金は『配分した候補の記録の金額』だけ(配分しなかった候補の値は today が計算した導出値)
    required, bad_required = _shown(_SHOWN_YEN, row.required_funds if allocated else None)
    shortfall, bad_shortfall = _shown(_SHOWN_YEN, row.shortfall)
    # 残余資金は today が記録の金額を引き算した目安 = 導出値。詳細では出さない
    remaining, bad_remaining = None, 0
    confidence = row.confidence
    bad_confidence = 0
    if (
        confidence is not None
        and confidence != UNKNOWN_TEXT
        and _CODE.fullmatch(confidence) is None
    ):
        confidence, bad_confidence = UNKNOWN_TEXT, 1
    cleaned = replace(
        row,
        seq=seq or "-",
        stock_code=stock,
        raer_value=raer,
        confidence=confidence,
        rank=rank,
        shares=shares,
        required_funds=required,
        shortfall=shortfall,
        remaining_cash=remaining,
    )
    bad = (
        bad_stock
        + bad_seq
        + bad_raer
        + bad_rank
        + bad_shares
        + bad_required
        + bad_shortfall
        + bad_remaining
        + bad_confidence
    )
    return cleaned, bad


def _candidate_detail(row: CandidateRow, raw: object) -> CandidateDetail:
    candidate = _dict(raw)
    if not candidate or not row.readable:
        return CandidateDetail(row=row, unparsed=0)
    row, row_bad = _clean_row(row)
    attrs = _dict(candidate.get("attrs"))
    price = _dict(candidate.get("price"))
    raer_in = _dict(candidate.get("raer_in"))
    raer_out = _dict(candidate.get("raer_out"))
    extra = _dict(raer_out.get("extra"))
    allocation = _dict(candidate.get("allocation"))
    unparsed = 0

    def _scalars(
        spec: tuple[tuple[str, object, str], ...],
    ) -> tuple[DetailValue, ...]:
        nonlocal unparsed
        values: list[DetailValue] = []
        for key, value, kind in spec:
            item, bad = _scalar(key, value, kind)
            values.append(item)
            unparsed += bad
        return tuple(values)

    attributes = _scalars(
        (
            ("kind", candidate.get("kind"), "code"),
            ("price_tier", attrs.get("price_tier"), "code"),
            ("fair_value_confidence", attrs.get("fair_value_confidence"), "code"),
            ("quality_band", attrs.get("quality_band"), "code"),
            ("sector", attrs.get("sector"), "code"),
            ("high_risk", attrs.get("high_risk"), "bool"),
            ("current_position_value", candidate.get("current_position_value"), "yen"),
        )
    )
    price_values = _scalars(
        (
            ("current_price", price.get("current_price"), "yen"),
            ("price_as_of_date", price.get("price_as_of_date"), "date"),
            ("freshness_verdict", price.get("freshness_verdict"), "code"),
        )
    )
    in_values, bad_in = _section(raer_in, _RAER_IN_KINDS)
    extra_values, bad_extra = _section(extra, _RAER_EXTRA_KINDS)
    out_scalars = _scalars(
        (
            ("model_version", raer_out.get("model_version"), "ident"),
            ("comparison_key", raer_out.get("comparison_key"), "ident"),
            ("risk_flags_available", raer_out.get("risk_flags_available"), "bool"),
        )
    )
    out_lists: list[DetailList] = []
    for key, kind in (
        ("applied_axes", "code"),
        ("unapplied_axes", "code"),
        ("risk_flags", "word"),
        ("reason_codes", "reason"),
    ):
        item, bad = _items(key, raer_out.get(key), kind)
        out_lists.append(item)
        unparsed += bad
    allocation_values = _scalars(
        (
            ("cap_limited_shares", allocation.get("cap_limited_shares"), "count"),
            ("price_tier_used", allocation.get("price_tier_used"), "code"),
            ("cash_shortfall", allocation.get("cash_shortfall"), "yen"),
        )
    )
    return CandidateDetail(
        row=row,
        attributes=attributes,
        price=price_values,
        raer_in=in_values,
        raer_out=(*out_scalars, *out_lists),
        raer_extra=extra_values,
        allocation=allocation_values,
        unparsed=unparsed + bad_in + bad_extra + row_bad,
    )


_POLICY_KINDS: Final[Mapping[str, str]] = {
    "min_raer_to_allocate": "dec",
    "comparison_key": "ident",
    "max_single_stock_amount": "dec",
    "max_single_stock_ratio": "dec",
    "max_sector_ratio": "dec",
    "max_high_risk_ratio": "dec",
    "high_risk_flags": "list:word",
    "tier_quantity_caps": "map",
    "minimum_trading_unit": "count",
}


def _policy(record: Mapping[str, Any]) -> PolicyDetail:
    raw_values, bad = _section(_dict(record.get("policy")), _POLICY_KINDS)
    # 方針の比較キーは、RAER の出力の比較キーと別の項目として扱う(キーの衝突を避ける)
    values = tuple(
        replace(v, key="policy_comparison_key") if v.key == "comparison_key" else v
        for v in raw_values
    )
    model = _dict(record.get("model"))
    policy_id, bad_id = _scalar("policy_id", model.get("policy_id"), "ident")
    return PolicyDetail((policy_id, *values), bad + bad_id)


def _run(record: Mapping[str, Any]) -> tuple[tuple[DetailValue | DetailMap, ...], int]:
    run = _dict(record.get("run"))
    elapsed, bad_elapsed = _scalar("elapsed_ms", run.get("elapsed_ms"), "count")
    states = _dict(run.get("read_states"))
    pairs: list[tuple[str, str]] = []
    unknown = 0
    for index, (name, value) in enumerate(sorted(states.items(), key=lambda kv: str(kv[0]))):
        key_text = _shaped(_WORD, name)
        value_text = _shaped(_CODE, value)
        if index >= _MAX_LIST or key_text is None or value_text is None:
            unknown += 1
            continue
        pairs.append((key_text, value_text))
    read_states = DetailMap("read_states", tuple(pairs), unknown, present=bool(states))
    return (elapsed, read_states), bad_elapsed + unknown


def _owner_detail(section: OwnerSection, items: Any) -> OwnerDetail:
    base = OwnerDetail(ref=section.ref, display_ref=section.display_ref, label=section.label)
    if section.status is not OwnerStatus.OK:
        return base
    # 買付余力・評価額の『読めない値』は画面モデル側(screen.unparsed)が数え済み = 二重に数えない
    cash, _ = _shown(_SHOWN_YEN, section.available_cash)
    reconciled, bad_at = _shown(_SHOWN_AT, section.last_reconciled_at)
    portfolio, _ = _shown(_SHOWN_YEN, section.portfolio_value)
    base = replace(
        base,
        available_cash=cash,
        last_reconciled_at=reconciled,
        portfolio_value=portfolio,
        unparsed=bad_at,
    )
    record = items.record if items is not None and items.state is RecordState.FOUND else None
    if not isinstance(record, dict) or record.get("record_kind") != ITEMS_KIND:
        return replace(base, unparsed=base.unparsed + 1)
    try:
        raws = _list(record.get("candidates"))
        if len(raws) != len(section.rows):
            return replace(base, unparsed=base.unparsed + 1)
        candidates = tuple(
            _candidate_detail(row, raw) for row, raw in zip(section.rows, raws, strict=True)
        )
        policy = _policy(record)
        run, bad_run = _run(record)
        return replace(
            base,
            candidates=candidates,
            policy=policy,
            run=run,
            unparsed=base.unparsed + policy.unparsed + bad_run,
        )
    except Exception:  # noqa: BLE001 - どんな辞書でも例外を出さない(読めなければ『不明』)
        return replace(base, unparsed=base.unparsed + 1)


def build_detail_screen(day: DayInput, labels: Mapping[str, str] | None = None) -> DetailScreen:
    """読んだ記録 → 詳細の画面。どんな入力でも例外を送出しない。"""
    screen = build_screen(day, labels)
    try:
        owners = tuple(
            _owner_detail(section, day.items.get(section.ref)) for section in screen.owners
        )
    except Exception:  # noqa: BLE001 - 最後の砦
        owners = ()
        screen = replace(screen, unparsed=screen.unparsed + 1)
    total = sum(len(owner.candidates) for owner in owners)
    return DetailScreen(
        screen=screen, owners=owners, total_candidates=total, shown_candidates=total
    )


# --- 絞り込み(表示だけ。読取の回数・順序・再計算に影響しない)--------------------------


def _owner_matches(owner: OwnerDetail, prefix: str | None) -> bool:
    if prefix is None:
        return True
    digest = owner.ref.rsplit(":", 1)[-1]
    return digest.startswith(prefix.lower())


def _candidate_matches(candidate: CandidateDetail, flt: DetailFilter) -> bool:
    row = candidate.row
    if flt.stock_code is not None and row.stock_code != flt.stock_code:
        return False
    if flt.decision is not None and (row.decision is None or row.decision.code != flt.decision):
        return False
    return not (flt.reason is not None and (row.reason is None or row.reason.code != flt.reason))


def apply_filter(detail: DetailScreen, flt: DetailFilter) -> DetailScreen:
    """絞り込みを表示へ適用する。0 件は『該当なし』として区別できる(shown_candidates = 0)。"""
    owners: list[OwnerDetail] = []
    for owner in detail.owners:
        if not _owner_matches(owner, flt.owner_prefix):
            continue
        kept = tuple(c for c in owner.candidates if _candidate_matches(c, flt))
        owners.append(replace(owner, candidates=kept))
    shown = sum(len(owner.candidates) for owner in owners)
    description = " / ".join(flt.description) if flt.description else None
    return replace(
        detail,
        owners=tuple(owners),
        filter_text=description,
        shown_candidates=shown,
    )

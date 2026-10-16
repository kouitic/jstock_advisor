"""購入側 Shadow の記録から、RAER の式の再現性を確かめる(Issue #934 BT-2a。#602 の Shadow
評価の再現性)。

『記録された入力(`raer_in`・`raer_out.risk_flags`)+ 記録された係数の束(`coefficient_set_id`)から、
承認済みの暫定式どおりに記録された値(`raer_out`)が出るか』を、候補ごとに**完全一致**で検算する。
式・係数・閾値の妥当性は判定しない(それは #960・USER の判断)。許容誤差は作らない。

## 守ること

* ★ **純粋・dormant** ★: I/O・時計・AWS・config・鍵に触れない。どこからも import されない。module
  名に
  `raer` を含むため、#606 の gate の名前の網(CLI・Lambda から到達禁止)に自動で入る。実データの
  replay を CLI へ出すには gate の変更(USER の承認の記録)が要る
* ★ **本番の式を import しない独立の再実装** ★: `compose_raer` を呼ばない(同じ関数で同じ結果になる
  自明な検査にしない)。式の『構造』(リスクの語の順序・重複除外の 2
  規則・信頼度の規則・データ品質の軸が
  未実装であること)はこの module に literal で持ち、★ モデル版に固定する
  ★(`SUPPORTED_MODEL_VERSION`)。
  版が上がったとき、黙って古い式で『一致』『不一致』
  と言わない(契約テストが本番の版との一致を固定する)
* ★ **判定の順序**(版 → 係数の識別子)★: 係数の束の識別子はモデル版を含む(`RaerCoefficients`
  の識別子の
  算出に COMBINED_MODEL_VERSION が入る)。版が上がると識別子も変わるため、
  識別子を先に見ると『係数が違う』と
  読めてしまう。版を先に見て『式が違う = replay の更新が要る』を正しい理由として返す。★
  版の門は識別子の門に
  包含されるが、報告される理由が変わるため削らない ★
* 記録の `model.config_hash` は**配分の方針**(policy_id・constraints・policy・unknown_price_tier_as)
  から
  作られ、係数を守らない。係数を守るのは `coefficient_set_id`
* ★ **数値の regime** ★(本番の `exact_decimal_context()` と同じ): 精度 400・Inexact を trap。
  丸めを許す
  のは『記録の部分 coverage の照合』の除算 1 か所だけ(量子 0.000001・偶数丸め)。Inexact は握らず、
  ★ `NUMERIC_REGIME_VIOLATION` ★ として再現できた件数に入れない(NOT_REPLAYABLE に混ぜない)
* ★ **営業トレンドの部分 coverage の下限は記録に無い**(assembler の方針の値)★:
  軸の適用は記録から読む
  (`trend_score` が在るか)。下限は引数 `trend_min_coverage_required` で受ける整合性の検査で、★
  構造的に片側 ★
  (適用されなかった候補には重みが記録されない → 大きすぎる下限は必ず鳴るが、
  小さすぎる下限は鳴らない)。
  範囲外(None・0 以下・1 超・Decimal でない)は『渡されていない』扱い。データが縛った上限と、
  渡した値の出所を報告に載せる(出所なしの結果は参考扱い)
* ★ **対象外** ★: 入力の組み立て(assembler)の再現(記録には元の事実が無い)/ RAER が算出不能の候補
  (入力は『合成されたときだけ』記録される)/ 係数を変えた what-if(評価用の係数の組は USER 未承認)
* 戻り値・例外に値・銘柄・金額を含めない(パスの語彙と件数・重みの組だけ)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum
from fractions import Fraction
from typing import Any, Final

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.raer_composition import RaerCoefficients

#: 対象にする記録の種別と版(記録の module の定数と同じ。契約テストで一致を固定する)。
#: 記録の module を import しない(記録の書き手 module の importer の網を広げない)
RECORD_KIND: Final = "allocation_shadow_items"
RECORD_SCHEMA_VERSION: Final = 1

#: この replay が式の構造を固定しているモデル版
#: (`raer_out.model_version`・`model.raer_model_version`)
SUPPORTED_MODEL_VERSION: Final = "raer-combined-v1-provisional-axes-y"

# --- 数値の regime(本番の exact_decimal_context / TREND_SCORE_QUANTUM と一致。契約テストで固定)---
EXACT_PRECISION: Final = 400
TREND_SCORE_QUANTUM: Final = Decimal("0.000001")
TREND_SCORE_ROUNDING: Final = ROUND_HALF_EVEN

# --- 式の構造(モデル版 SUPPORTED_MODEL_VERSION に固定。契約テストで本番との一致を固定)---
#: 軸の固定順(本番の AXIS_ORDER)
AXIS_NAMES: Final = (
    "FAIR_VALUE_CONFIDENCE",
    "COMPANY_QUALITY",
    "BUSINESS_RISK",
    "DATA_QUALITY",
    "EARNINGS_TREND",
)
#: データ品質の軸は未実装(常に不適用)
UNIMPLEMENTED_AXES: Final = frozenset({"DATA_QUALITY"})
#: リスクの語の順序(本番の RiskFlag の定義順)。同値のときの重複除外の優先に使う
RISK_ORDER: Final = (
    "cyclical_industry",
    "major_customer_dependency",
    "temporary_earnings_boost_risk",
    "earnings_within_3_business_days",
    "earnings_within_7_business_days",
    "small_cap_or_low_liquidity",
)
#: 重複の除外 ① 決算 3 営業日以内があれば 7 営業日以内は数えない
_EARNINGS_NEAR: Final = "earnings_within_3_business_days"
_EARNINGS_FAR: Final = "earnings_within_7_business_days"
#: 重複の除外 ② 景気に左右される業種と一時的な増益は、引く量の大きい方だけ数える(同値は定義順で先)
_DUPLICATE_PAIR: Final = ("cyclical_industry", "temporary_earnings_boost_risk")
#: 適用軸がこの数以下なら信頼度は LOW(本番の LOW_CONFIDENCE_MAX_APPLIED_AXES)
LOW_CONFIDENCE_MAX_APPLIED_AXES: Final = 3
#: PROVISIONAL の間の信頼度の上限(本番の PROVISIONAL_CONFIDENCE_CEILING)
PROVISIONAL_CONFIDENCE_CEILING: Final = "MEDIUM"
_CONFIDENCE_ORDER: Final = ("LOW", "MEDIUM", "HIGH")
#: compose_raer が常に付ける仕様 flag
EXPECTED_SPEC_FLAGS: Final = ("HORIZON_MISMATCH_PROVISIONAL",)
_HUNDRED: Final = Decimal(100)
_TREND_WEIGHT_NAMES: Final = (
    "operating_income",
    "operating_cashflow",
    "dividend_direction",
    "acceleration",
)

#: 営業トレンドの整合性の検査は片側である(報告に必ず載せる固定の文)
TREND_COVERAGE_CHECK_IS_ONE_SIDED: Final = (
    "この検査は片側である。大きすぎる下限は検出するが、小さすぎる下限は検出できない"
    "(適用されなかった候補の重みが記録に無いため)"
)


class RecordGate(StrEnum):
    """記録 1 件の全体が再現の対象外になる理由(判定の順序 = 上から)。"""

    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"  # 記録の検証に通らない
    MODEL_VERSION_UNSUPPORTED = "MODEL_VERSION_UNSUPPORTED"  # この replay が固定した版ではない
    COEFFICIENT_SET_MISMATCH = "COEFFICIENT_SET_MISMATCH"  # 渡された係数の束の識別子と違う


class CandidateGate(StrEnum):
    """候補 1 件が再現の対象外になる理由。"""

    RAER_UNAVAILABLE = "RAER_UNAVAILABLE"  # 値が無い(入力は合成されたときだけ記録される)


@dataclass(frozen=True)
class TrendCoverageReport:
    """営業トレンドの部分 coverage の整合性の検査の結果(★ 片側の検査 ★)。"""

    #: 下限が有効な値(0 < 値 ≦ 1 の Decimal)で渡されたか。★ None・0・範囲外は『渡されていない』★
    supplied: bool
    #: 渡した値の出所(固定語彙の識別子。値は書かない)。未指定は None = 出所なし(結果は参考扱い)
    source: str | None
    #: 営業トレンドが適用された候補の数(検査に使える候補)
    applied: int
    #: 検査した件数(下限が渡されたときだけ。applied と同じ)
    checked: int
    #: 下限が渡されなかったため検査しなかった、適用された候補の件数
    not_checked: int
    #: 『適用されているのに部分 coverage が下限未満』の件数
    violations: int
    #: データが下限を縛った上限 = min(適用された候補の 利用した重みの和 / 重みの合計)を
    #: 『(和, 合計)』の組で。
    #: ★ 商にしない(丸めを増やさない)★。適用された候補が 0 件なら None
    upper_bound: tuple[str, str] | None
    note: str = TREND_COVERAGE_CHECK_IS_ONE_SIDED


@dataclass(frozen=True)
class RaerReplayReport:
    """RAER の式の再現の結果。値・銘柄・金額を持たない(パスの語彙と件数だけ)。"""

    #: 記録の候補の件数(読めたもの)
    candidates: int
    #: 記録全体が対象外になった理由(版 → 係数の識別子の順)。None = 候補ごとに検算した
    record_gate: RecordGate | None
    #: 再現の対象にした候補(値があり、門を通ったもの)
    replayable: int
    #: 食い違いが 0 件で、regime の違反も無かった候補
    matched: int
    #: 食い違いのパス(例 `candidates[0].raer_out.value`)。重複を除かず出現順
    mismatches: tuple[str, ...]
    #: 候補ごとの対象外の件数(理由別)
    not_replayable: tuple[tuple[CandidateGate, int], ...]
    #: ★ 丸めが起きた(Inexact)候補の件数。再現できた件数に入れない。NOT_REPLAYABLE に混ぜない ★
    regime_violations: int
    trend_coverage: TrendCoverageReport

    @property
    def all_replayed_matched(self) -> bool:
        """対象にした候補がすべて完全一致か(★ 合格の基準ではない。基準は USER 判断 ★)。"""
        return (
            self.record_gate is None
            and self.regime_violations == 0
            and not self.mismatches
            and self.matched == self.replayable
        )


class _MismatchError(Exception):
    """記録の項目が読めない(型・欠落)。メッセージはパスの語彙だけ。"""


def _exact_context() -> Context:
    return Context(
        prec=EXACT_PRECISION, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact]
    )


def _rounding_context() -> Context:
    """丸めを許す唯一の context(記録の部分 coverage の照合の除算だけ)。"""
    return Context(prec=EXACT_PRECISION, traps=[InvalidOperation, DivisionByZero, Overflow])


def _dec(path: str, value: Any) -> Decimal:
    if not isinstance(value, str):
        raise _MismatchError(path)
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _MismatchError(path) from None
    if not number.is_finite():
        raise _MismatchError(path)
    return number


def _on_trend_grid(value: Decimal) -> bool:
    """点が 6 桁の量子の上にあるか。★ 丸めの検査であり、丸めを許す context で行う(Inexact
    を違反に数えない)★"""
    with localcontext(_rounding_context()):
        return value == value.quantize(TREND_SCORE_QUANTUM, rounding=TREND_SCORE_ROUNDING)


def _valid_minimum(value: object) -> Decimal | None:
    """0 < 下限 ≦ 1 の Decimal だけを『渡された』と扱う。それ以外(None・0・負・1 超・型違い)は
    None。"""
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    return value if Decimal(0) < value <= Decimal(1) else None


def _risk_counted(flags: frozenset[str], coefficients: RaerCoefficients) -> dict[str, Decimal]:
    """重複の除外後に数えるリスクの語 → 引く量。"""
    deduction = {flag.value: amount for flag, amount in coefficients.risk_deductions.items()}
    excluded: set[str] = set()
    if _EARNINGS_NEAR in flags and _EARNINGS_FAR in flags:
        excluded.add(_EARNINGS_FAR)
    pair = [flag for flag in _DUPLICATE_PAIR if flag in flags]
    if len(pair) == 2:
        keep = max(pair, key=lambda f: (deduction[f], -RISK_ORDER.index(f)))
        excluded.update(flag for flag in pair if flag != keep)
    return {f: deduction[f] for f in RISK_ORDER if f in flags and f not in excluded}


@dataclass
class _TrendAccumulator:
    """営業トレンドの整合性の検査の集計(候補をまたぐ)。"""

    minimum: Decimal | None
    applied: int = 0
    violations: int = 0
    bound: Fraction | None = None
    bound_pair: tuple[Decimal, Decimal] | None = None


def _check_candidate(
    index: int,
    candidate: Mapping[str, Any],
    coefficients: RaerCoefficients,
    trend: _TrendAccumulator,
) -> list[str]:
    """候補 1 件の検算。食い違いのパスの一覧を返す(空 = 完全一致)。Inexact は呼び出し側が数える。"""
    base = f"candidates[{index}]"
    rin = candidate["raer_in"]
    rout = candidate["raer_out"]
    extra = rout["extra"]
    bad: list[str] = []

    def at(path: str) -> str:
        return f"{base}.{path}"

    # 入力(パースできないものは食い違い)
    upside = _dec(at("raer_in.upside_pct"), rin["upside_pct"])
    income = _dec(at("raer_in.income_pct"), rin["income_pct"])
    base_pct = upside + income

    applied: list[str] = []
    contributions: dict[str, Decimal] = {}

    confidence = rin["fair_value_confidence"]
    if confidence is not None:
        applied.append("FAIR_VALUE_CONFIDENCE")
        factor = coefficients.fv_confidence_factor[ConfidenceLevel(confidence)]
        contributions["FAIR_VALUE_CONFIDENCE"] = max(upside, Decimal(0)) * (Decimal(1) - factor)

    score = rin["quality_score"]
    if score is not None:
        coverage = _dec(at("raer_in.quality_coverage_ratio"), rin["quality_coverage_ratio"])
        minimum_quality = _dec(at("raer_in.quality_min_coverage"), rin["quality_min_coverage"])
        # 企業品質の coverage は両方が記録に在り直接比較(除算・丸めを挟まない)
        if coverage >= minimum_quality:
            applied.append("COMPANY_QUALITY")
            contributions["COMPANY_QUALITY"] = coefficients.quality_deduction_max * (
                Decimal(1)
                - _dec(at("raer_in.quality_score"), score) / coefficients.quality_max_score
            )

    recorded_flags = rout["risk_flags"]
    counted: dict[str, Decimal] = {}
    if rin["risk_flags_observed"]:
        applied.append("BUSINESS_RISK")
        counted = _risk_counted(frozenset(recorded_flags), coefficients)
        total_risk = sum(counted.values(), Decimal(0))
        contributions["BUSINESS_RISK"] = min(total_risk, coefficients.risk_deduction_cap)
    elif recorded_flags:
        bad.append(at("raer_out.risk_flags"))  # 観測の無い軸の語を信じない

    trend_score = rin["trend_score"]
    if trend_score is not None:
        applied.append("EARNINGS_TREND")
        value = _dec(at("raer_in.trend_score"), trend_score)
        if not _on_trend_grid(value):
            bad.append(at("raer_in.trend_score"))  # 記録の点は 6 桁の量子の上にある
        contributions["EARNINGS_TREND"] = (
            coefficients.trend_deduction_scale * max(Decimal(0), -value) / _HUNDRED
        )
        bad.extend(_check_trend_coverage(base, rin, trend))

    result = base_pct - sum(contributions.values(), Decimal(0))

    # 必須軸・最低軸数(満たさない値のある記録 = 不一致)
    if not {axis.value for axis in coefficients.required_axes} <= set(applied):
        bad.append(at("raer_out.applied_axes"))
    if len(applied) < coefficients.min_applied_axes:
        bad.append(at("raer_out.applied_axes"))

    # 記録との突合
    if _dec(at("raer_out.value"), rout["value"]) != result:
        bad.append(at("raer_out.value"))
    if sorted(applied) != list(rout["applied_axes"]):
        bad.append(at("raer_out.applied_axes"))
    unapplied = sorted(axis for axis in AXIS_NAMES if axis not in applied)
    if unapplied != list(rout["unapplied_axes"]):
        bad.append(at("raer_out.unapplied_axes"))
    state = "AVAILABLE" if len(applied) == len(AXIS_NAMES) else "PROVISIONAL"
    if state != rout["state"]:
        bad.append(at("raer_out.state"))
    expected_confidence = _expected_confidence(confidence, state, len(applied))
    if expected_confidence != rout["confidence"]:
        bad.append(at("raer_out.confidence"))
    if rout["risk_flags_available"] != ("BUSINESS_RISK" in applied):
        bad.append(at("raer_out.risk_flags_available"))
    if rout["comparison_key"] != f"{SUPPORTED_MODEL_VERSION}|{','.join(sorted(applied))}":
        bad.append(at("raer_out.comparison_key"))

    base_record = extra["base"] or {}
    for key, expected in (("upside", upside), ("income", income), ("base", base_pct)):
        if _dec(at(f"raer_out.extra.base.{key}"), base_record.get(key)) != expected:
            bad.append(at(f"raer_out.extra.base.{key}"))
    recorded_contributions = extra["contributions"] or {}
    if set(recorded_contributions) != set(contributions):
        bad.append(at("raer_out.extra.contributions"))
    for axis, expected in contributions.items():
        if axis in recorded_contributions and (
            _dec(at(f"raer_out.extra.contributions.{axis}"), recorded_contributions[axis])
            != expected
        ):
            bad.append(at(f"raer_out.extra.contributions.{axis}"))
    recorded_risk = extra["risk_contributions"] or {}
    if set(recorded_risk) != set(counted):
        bad.append(at("raer_out.extra.risk_contributions"))
    for flag, expected in counted.items():
        if flag in recorded_risk and (
            _dec(at(f"raer_out.extra.risk_contributions.{flag}"), recorded_risk[flag]) != expected
        ):
            bad.append(at(f"raer_out.extra.risk_contributions.{flag}"))
    if extra["coefficient_set_id"] != coefficients.coefficient_set_id:
        bad.append(at("raer_out.extra.coefficient_set_id"))
    if list(extra["spec_flags"] or []) != list(EXPECTED_SPEC_FLAGS):
        bad.append(at("raer_out.extra.spec_flags"))
    return bad


def _expected_confidence(confidence: str | None, state: str, applied: int) -> str | None:
    if confidence is None:
        return None
    if applied <= LOW_CONFIDENCE_MAX_APPLIED_AXES:
        return "LOW"
    if state == "PROVISIONAL" and _CONFIDENCE_ORDER.index(confidence) > _CONFIDENCE_ORDER.index(
        PROVISIONAL_CONFIDENCE_CEILING
    ):
        return PROVISIONAL_CONFIDENCE_CEILING
    return confidence


def _check_trend_coverage(base: str, rin: Mapping[str, Any], trend: _TrendAccumulator) -> list[str]:
    """営業トレンドの部分 coverage の整合性(★ 判定には記録の丸めた値を使わない ★)。

    ① 記録の `trend_partial_coverage` は、重みから再計算した商(6 桁・偶数丸め)
    と一致するか(表示の照合)。
    ② 下限が有効に渡されたときだけ、★ 交差乗算 ★(利用した重みの和 < 下限 × 重みの合計)
    で整合を検査する。
    """
    path = f"{base}.raer_in.trend_partial_coverage"
    weights_raw = rin["trend_weights"]
    used_raw = rin["trend_components_used"]
    if not isinstance(weights_raw, dict) or not isinstance(used_raw, list):
        return [f"{base}.raer_in.trend_weights"]
    weights = {
        name: _dec(f"{base}.raer_in.trend_weights", weights_raw.get(name))
        for name in _TREND_WEIGHT_NAMES
    }
    if any(word not in weights for word in used_raw):
        return [f"{base}.raer_in.trend_components_used"]
    used = sum((weights[word] for word in used_raw), Decimal(0))
    total = sum(weights.values(), Decimal(0))
    bad: list[str] = []
    if total <= 0:
        return [f"{base}.raer_in.trend_weights"]
    trend.applied += 1
    pair = (used, total)
    ratio = Fraction(used) / Fraction(total)
    if trend.bound is None or ratio < trend.bound:
        trend.bound, trend.bound_pair = ratio, pair
    # ① 表示の照合(丸めを許す唯一の場所)
    with localcontext(_rounding_context()):
        recomputed = (used / total).quantize(TREND_SCORE_QUANTUM, rounding=TREND_SCORE_ROUNDING)
    if _dec(path, rin["trend_partial_coverage"]) != recomputed:
        bad.append(path)
    # ② 下限の整合性の検査(交差乗算。除算しない)
    if trend.minimum is not None and used < trend.minimum * total:
        trend.violations += 1
        bad.append(path)
    return bad


def _structure(record: object) -> tuple[list[Any], Mapping[str, Any]] | None:
    """記録の外形(種別・版・候補の一覧・model)。読めなければ None(= SCHEMA_UNSUPPORTED)。"""
    try:
        if not isinstance(record, Mapping):
            return None
        if (
            record["record_kind"] != RECORD_KIND
            or record["schema_version"] != RECORD_SCHEMA_VERSION
        ):
            return None
        candidates, model = record["candidates"], record["model"]
        if not isinstance(candidates, list) or not isinstance(model, Mapping):
            return None
        _ = model["raer_model_version"], model["coefficient_set_id"]
    except (KeyError, TypeError):
        return None
    return candidates, model


def replay_raer_in_record(
    record: Mapping[str, Any],
    coefficients: RaerCoefficients,
    *,
    trend_min_coverage_required: Decimal | None = None,
    trend_min_coverage_source: str | None = None,
) -> RaerReplayReport:
    """項目つきの記録 1 件の RAER を、記録された入力と係数の束から再現できるか検算する(純粋)。

    ① 記録の外形の確認 → ② モデル版の門 → ③ 係数の束の識別子の門 → ④ 候補ごとの検算、
    の順(上から先に当たった
    ものを理由とする)。候補ごとの Inexact は握らず `regime_violations` に数える。
    """
    minimum = _valid_minimum(trend_min_coverage_required)
    structure = _structure(record)
    if structure is None:
        return _gated(0, RecordGate.SCHEMA_UNSUPPORTED, minimum, trend_min_coverage_source)
    candidates, model = structure
    if model["raer_model_version"] != SUPPORTED_MODEL_VERSION:
        return _gated(
            len(candidates),
            RecordGate.MODEL_VERSION_UNSUPPORTED,
            minimum,
            trend_min_coverage_source,
        )
    if model["coefficient_set_id"] != coefficients.coefficient_set_id:
        return _gated(
            len(candidates), RecordGate.COEFFICIENT_SET_MISMATCH, minimum, trend_min_coverage_source
        )

    accumulator = _TrendAccumulator(minimum)
    mismatches: list[str] = []
    unavailable = replayable = matched = regime = 0
    for index, candidate in enumerate(candidates):
        try:
            if candidate["raer_out"]["value"] is None:
                unavailable += 1
                continue
        except (KeyError, TypeError):
            replayable += 1
            mismatches.append(f"candidates[{index}]")
            continue
        replayable += 1
        try:
            with localcontext(_exact_context()):
                bad = _check_candidate(index, candidate, coefficients, accumulator)
        except Inexact:
            regime += 1
            continue
        except _MismatchError as error:
            bad = [str(error)]
        except (KeyError, TypeError, AttributeError):
            bad = [f"candidates[{index}]"]  # 項目の欠落・型の違い(パスは候補までしか言えない)
        if bad:
            mismatches.extend(dict.fromkeys(bad))  # 同じパスの重複は 1 つにする(出現順)
        else:
            matched += 1
    return RaerReplayReport(
        candidates=len(candidates),
        record_gate=None,
        replayable=replayable,
        matched=matched,
        mismatches=tuple(mismatches),
        not_replayable=((CandidateGate.RAER_UNAVAILABLE, unavailable),) if unavailable else (),
        regime_violations=regime,
        trend_coverage=_trend_report(accumulator, trend_min_coverage_source),
    )


def _trend_report(accumulator: _TrendAccumulator, source: str | None) -> TrendCoverageReport:
    supplied = accumulator.minimum is not None
    bound = None
    if accumulator.bound_pair is not None:
        bound = (format(accumulator.bound_pair[0], "f"), format(accumulator.bound_pair[1], "f"))
    return TrendCoverageReport(
        supplied=supplied,
        source=source if supplied else None,
        applied=accumulator.applied,
        checked=accumulator.applied if supplied else 0,
        not_checked=0 if supplied else accumulator.applied,
        violations=accumulator.violations,
        upper_bound=bound,
    )


def _gated(
    candidates: int, gate: RecordGate, minimum: Decimal | None, source: str | None
) -> RaerReplayReport:
    return RaerReplayReport(
        candidates=candidates,
        record_gate=gate,
        replayable=0,
        matched=0,
        mismatches=(),
        not_replayable=(),
        regime_violations=0,
        trend_coverage=_trend_report(_TrendAccumulator(minimum), source),
    )


__all__ = [
    "AXIS_NAMES",
    "EXACT_PRECISION",
    "PROVISIONAL_CONFIDENCE_CEILING",
    "RISK_ORDER",
    "SUPPORTED_MODEL_VERSION",
    "TREND_COVERAGE_CHECK_IS_ONE_SIDED",
    "TREND_SCORE_QUANTUM",
    "TREND_SCORE_ROUNDING",
    "CandidateGate",
    "RaerReplayReport",
    "RecordGate",
    "TrendCoverageReport",
    "replay_raer_in_record",
]

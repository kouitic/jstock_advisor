"""判定時点Recommendationからのspread観測の導出(Issue #20 Phase B1)。

保存済みRecommendationの判定時点スナップショット(BUY: valuation_methods+
buy_score_input_facts["valuation_outlier_exclusions"]+decision_valuation_min/max、
SELL: fair_value_methods+fair_value_bear/bull)だけを入力として、
「どの方式がmin/maxを形成し、どの程度のspreadだったか」を決定的に導出する。

【設計原則(承認済み)】
- 永続化しない(スキーマ追加ゼロ)。正本は既存Recommendationの保存済み事実で
  あり、本モジュールはその唯一の解釈実装。過去レコードにも同一意味論で
  遡及適用できる。
- 現在のconfig・市場データ・Providerを一切参照しない(判定時点値の再編成で
  あり再計算ではない)。
- 復元できない状態では値を推測せずOBSERVATION_UNAVAILABLEを返す。
  「データなし(UNAVAILABLE)」と「有効方式0件(AVAILABLE・count=0)」を
  混同しない。
- 本番実行経路からは呼ばれない(Phase Cの分析・テスト専用)。判定・表示への
  影響はゼロ。

【contextの定義】
- BUY_RAW: DCF上方乖離フィルタ・外れ値フィルタ適用前の集計候補値の集合。
  Recommendation.valuation_methodsは外れ値フィルタ適用「前」のスナップ
  ショット(buy_signal_service.pyのコメント参照)のため、applicable=Trueの
  値と、DCF乖離除外(applicable=False+exclusion_detail、値は保持)の値から
  復元する。算出不能・業種モデル未実装等でそもそも集計候補にならなかった
  方式(applicable=False・exclusion_detailなし)はRAWにも含めない。
- BUY_DECISION: valuation_anchorが実際に使用した集合(外れ値フィルタ適用後)。
  applicable値からbuy_score_input_facts["valuation_outlier_exclusions"]の
  方式を除いて復元し、保存済みdecision_valuation_min/maxと照合する。
  照合できない場合(旧世代レコード等)は推測せずUNAVAILABLE。
- SELL_RAW: SELL側FairValueRange(フィルタ機構自体が存在しない)の全有効値。
  fair_value_methodsから復元し、保存済みfair_value_bear/bullと照合する。
  SELLにfilteredコンテキストは存在しないため作らない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from jstock_advisor.domain.entities.recommendation import Recommendation


class ValuationSpreadContext(StrEnum):
    BUY_RAW = "BUY_RAW"
    BUY_DECISION = "BUY_DECISION"
    SELL_RAW = "SELL_RAW"


class ObservationStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    OBSERVATION_UNAVAILABLE = "OBSERVATION_UNAVAILABLE"


@dataclass(frozen=True)
class ExcludedMethodObservation:
    """除外された方式の機械可読情報(保存済みexclusion_detail/外れ値スナップ
    ショットからの写し。messageは分析に不要なためコピーしない)。"""

    method: str
    code: str | None
    actual_value: Decimal | None
    reference_value: Decimal | None


@dataclass(frozen=True)
class ValuationSpreadObservation:
    context: ValuationSpreadContext
    status: ObservationStatus
    min_method: str | None = None
    min_value: Decimal | None = None
    max_method: str | None = None
    max_value: Decimal | None = None
    spread_ratio: float | None = None  # max/min(min<=0または端点なしはNone)
    methods_count: int = 0
    excluded: tuple[ExcludedMethodObservation, ...] = ()
    unavailable_reason: str | None = None
    # Issue #20 Phase C: このcontextの母集団((method, 値)のmethod名昇順tuple)。
    # 端点だけでなく仮説別の集約(shadow分析)が全値集合を必要とするため公開する
    # (導出元・意味論は従来と同一。AVAILABLE時のみ非空)。
    values: tuple[tuple[str, Decimal], ...] = ()


@dataclass(frozen=True)
class _Endpoints:
    min_method: str | None = None
    min_value: Decimal | None = None
    max_method: str | None = None
    max_value: Decimal | None = None
    spread_ratio: float | None = None
    values: tuple[tuple[str, Decimal], ...] = field(default=())


_UNAVAILABLE_NO_BUY_SNAPSHOT = (
    "valuation_methodsが保存されていません(BUY判定記録でないか、スナップショット導入前の記録)"
)
_UNAVAILABLE_RAW_VALUE_LOST = (
    "除外方式の元値がスナップショットに保存されておらず、RAW集合を推測なしで復元できません"
)
_UNAVAILABLE_DECISION_MISMATCH = (
    "導出したdecision集合が保存済みdecision_valuation_min/maxと一致しません"
    "(外れ値スナップショット未保存世代の可能性。推測での補完は行いません)"
)
_UNAVAILABLE_DECISION_UNVERIFIABLE = (
    "decision_valuation_min/maxが保存されておらず、decision集合の照合ができません"
)
_UNAVAILABLE_NO_SELL_SNAPSHOT = (
    "fair_value_methodsが保存されていません(SELL判定記録でないか、スナップショット導入前の記録)"
)
_UNAVAILABLE_SELL_RANGE_MISMATCH = (
    "導出した端点が保存済みfair_value_bear/bullと一致しません(推測での補完は行いません)"
)


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _endpoints(pairs: list[tuple[str, Decimal]]) -> _Endpoints:
    """端点の決定。同値タイはmethod名昇順の先頭を採用する(決定的)。"""
    if not pairs:
        return _Endpoints()
    min_value = min(v for _, v in pairs)
    max_value = max(v for _, v in pairs)
    min_method = min(m for m, v in pairs if v == min_value)
    max_method = min(m for m, v in pairs if v == max_value)
    spread_ratio = float(max_value / min_value) if min_value > 0 else None
    return _Endpoints(
        min_method=min_method,
        min_value=min_value,
        max_method=max_method,
        max_value=max_value,
        spread_ratio=spread_ratio,
        values=tuple(sorted(pairs)),
    )


def _available(
    context: ValuationSpreadContext,
    endpoints: _Endpoints,
    excluded: tuple[ExcludedMethodObservation, ...] = (),
) -> ValuationSpreadObservation:
    return ValuationSpreadObservation(
        context=context,
        status=ObservationStatus.AVAILABLE,
        min_method=endpoints.min_method,
        min_value=endpoints.min_value,
        max_method=endpoints.max_method,
        max_value=endpoints.max_value,
        spread_ratio=endpoints.spread_ratio,
        methods_count=len(endpoints.values),
        excluded=excluded,
        values=endpoints.values,
    )


def _unavailable(context: ValuationSpreadContext, reason: str) -> ValuationSpreadObservation:
    return ValuationSpreadObservation(
        context=context,
        status=ObservationStatus.OBSERVATION_UNAVAILABLE,
        unavailable_reason=reason,
    )


def derive_buy_spread_observations(
    recommendation: Recommendation,
) -> tuple[ValuationSpreadObservation, ValuationSpreadObservation]:
    """(BUY_RAW, BUY_DECISION)を導出する。"""
    methods = recommendation.valuation_methods
    if not methods:
        return (
            _unavailable(ValuationSpreadContext.BUY_RAW, _UNAVAILABLE_NO_BUY_SNAPSHOT),
            _unavailable(ValuationSpreadContext.BUY_DECISION, _UNAVAILABLE_NO_BUY_SNAPSHOT),
        )

    raw_pairs: list[tuple[str, Decimal]] = []
    applicable_pairs: list[tuple[str, Decimal]] = []
    pipeline_exclusions: list[ExcludedMethodObservation] = []
    raw_value_lost = False
    for m in methods:
        if m.applicable and m.fair_value is not None:
            raw_pairs.append((m.method, m.fair_value))
            applicable_pairs.append((m.method, m.fair_value))
            continue
        if not m.applicable and m.exclusion_detail is not None:
            # 集計候補だったがフィルタで除外された方式(valuation_methodsは
            # 外れ値フィルタ「前」のため、現状ここに現れるのはDCF上方乖離除外)。
            # 元値はfair_value(DCF乖離除外は値を保持)またはexclusion_detail.
            # actual_valueから復元する。どちらも無ければ推測せずRAWを不可とする。
            value = m.fair_value if m.fair_value is not None else _to_decimal(
                m.exclusion_detail.actual_value
            )
            pipeline_exclusions.append(
                ExcludedMethodObservation(
                    method=m.method,
                    code=m.exclusion_detail.code,
                    actual_value=value,
                    reference_value=_to_decimal(m.exclusion_detail.reference_value),
                )
            )
            if value is None:
                raw_value_lost = True
            else:
                raw_pairs.append((m.method, value))
        # applicable=False・exclusion_detailなし(算出不能・業種モデル未実装・
        # 不適切前提等)は、そもそも集計候補にならなかった方式のためRAWにも
        # 含めない(存在しなかった値を捏造しない)。

    raw_observation = (
        _unavailable(ValuationSpreadContext.BUY_RAW, _UNAVAILABLE_RAW_VALUE_LOST)
        if raw_value_lost
        else _available(ValuationSpreadContext.BUY_RAW, _endpoints(raw_pairs))
    )

    # --- BUY_DECISION ---
    facts = recommendation.buy_score_input_facts or {}
    outlier_entries = facts.get("valuation_outlier_exclusions")
    outlier_exclusions: list[ExcludedMethodObservation] = []
    if isinstance(outlier_entries, list):
        for entry in outlier_entries:
            if not isinstance(entry, dict):
                continue
            method = entry.get("method")
            if not isinstance(method, str):
                continue
            code = entry.get("code")
            outlier_exclusions.append(
                ExcludedMethodObservation(
                    method=method,
                    code=code if isinstance(code, str) else None,
                    actual_value=_to_decimal(entry.get("actual_value")),
                    reference_value=_to_decimal(entry.get("reference_value")),
                )
            )
    outlier_methods = {e.method for e in outlier_exclusions}
    decision_pairs = [(m, v) for m, v in applicable_pairs if m not in outlier_methods]
    decision_endpoints = _endpoints(decision_pairs)

    saved_min = recommendation.decision_valuation_min
    saved_max = recommendation.decision_valuation_max
    if decision_pairs:
        if saved_min is None or saved_max is None:
            # 照合手段がない(decision範囲の保存導入前の旧世代等)。外れ値
            # スナップショット欠落を検知できないため、推測での確定はしない。
            decision_observation = _unavailable(
                ValuationSpreadContext.BUY_DECISION, _UNAVAILABLE_DECISION_UNVERIFIABLE
            )
        elif decision_endpoints.min_value != saved_min or decision_endpoints.max_value != saved_max:
            decision_observation = _unavailable(
                ValuationSpreadContext.BUY_DECISION, _UNAVAILABLE_DECISION_MISMATCH
            )
        else:
            decision_observation = _available(
                ValuationSpreadContext.BUY_DECISION,
                decision_endpoints,
                excluded=tuple(pipeline_exclusions + outlier_exclusions),
            )
    else:
        # 有効方式0件(NO_VALID_METHODS相当)は「spreadなし」ではなく
        # count=0のAVAILABLE観測として区別する。保存済み範囲が残っている
        # 場合は0件と矛盾するためUNAVAILABLE。
        if saved_min is not None or saved_max is not None:
            decision_observation = _unavailable(
                ValuationSpreadContext.BUY_DECISION, _UNAVAILABLE_DECISION_MISMATCH
            )
        else:
            decision_observation = _available(
                ValuationSpreadContext.BUY_DECISION,
                decision_endpoints,
                excluded=tuple(pipeline_exclusions + outlier_exclusions),
            )
    return raw_observation, decision_observation


def derive_sell_spread_observation(recommendation: Recommendation) -> ValuationSpreadObservation:
    """SELL_RAWを導出する。"""
    sell_methods = recommendation.fair_value_methods
    if not sell_methods:
        return _unavailable(ValuationSpreadContext.SELL_RAW, _UNAVAILABLE_NO_SELL_SNAPSHOT)

    pairs: list[tuple[str, Decimal]] = []
    for entry in sell_methods:
        if not isinstance(entry, dict):
            continue
        method = entry.get("method")
        value = _to_decimal(entry.get("fair_value"))
        if isinstance(method, str) and value is not None:
            pairs.append((method, value))

    endpoints = _endpoints(pairs)
    saved_bear = recommendation.fair_value_bear
    saved_bull = recommendation.fair_value_bull
    if pairs:
        if saved_bear is not None and endpoints.min_value != saved_bear:
            return _unavailable(ValuationSpreadContext.SELL_RAW, _UNAVAILABLE_SELL_RANGE_MISMATCH)
        if saved_bull is not None and endpoints.max_value != saved_bull:
            return _unavailable(ValuationSpreadContext.SELL_RAW, _UNAVAILABLE_SELL_RANGE_MISMATCH)
    elif saved_bear is not None or saved_bull is not None:
        return _unavailable(ValuationSpreadContext.SELL_RAW, _UNAVAILABLE_SELL_RANGE_MISMATCH)
    return _available(ValuationSpreadContext.SELL_RAW, endpoints)


def derive_spread_observations(
    recommendation: Recommendation,
) -> tuple[ValuationSpreadObservation, ValuationSpreadObservation, ValuationSpreadObservation]:
    """(BUY_RAW, BUY_DECISION, SELL_RAW)の3観測を常に返す(該当スナップ
    ショットを持たないcontextはOBSERVATION_UNAVAILABLE)。"""
    buy_raw, buy_decision = derive_buy_spread_observations(recommendation)
    return buy_raw, buy_decision, derive_sell_spread_observation(recommendation)

"""判定時点Recommendationからのdownside scenarioの導出(Issue #20 O-C)。

適正価格の集計(valuation anchor)は、算出値のうち外れ値と判定したものを除外して
決定される。除外そのものは変更しないが、「低い評価も算出されていた」という事実は
判定時点にすでに保存されており、それをユーザーへ提示できないままにしていた。
本モジュールは、その保存済みの事実を再編成して参考情報として読み出す。

【設計原則(Human承認済み: H7=D2 / H8=下方3コード / H10=reader-first不要)】
- 永続化しない(スキーマ追加ゼロ)。正本は既存Recommendationの
  buy_score_input_facts["valuation_outlier_exclusions"]であり、本モジュールは
  その解釈実装である。過去レコードにも同一意味論で遡及適用できる。
- 現在のconfig・閾値・市場データ・Providerを一切参照しない(判定時点値の
  再編成であり再計算ではない)。
- 判定・スコア・価格算出へ影響しない。valuation計算経路・BUY判定経路からは
  本モジュールをimportしない(依存方向は
  保存済みfacts -> 本モジュール -> view/formatter の一方向のみ)。
- 復元できない値を推測しない。「除外0件(AVAILABLE)」と「そもそも観測できない
  (OBSERVATION_UNAVAILABLE)」を混同しない。

【対象とする除外理由(H8)】
下方の3コードのみをdownside scenarioとして扱う。

- EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE
- EXTREME_LOW_RELATIVE_TO_MEDIAN
- BELOW_52_WEEK_LOW

上方の除外(EXTREME_HIGH_RELATIVE_TO_MEDIAN / DCF_UPWARD_DIVERGENCE)は
「低い評価」ではないため対象外。将来コードが追加された場合も、既知の下方3コード
以外はfail-closedとして一覧へ入れない(下方かどうか判断できないものを
悲観シナリオとして提示しない)。

【逆算の禁止】
BELOW_52_WEEK_LOWのreference_valueは判定時点の「直近52週安値 × 0.50」であり、
0.50で割れば52週安値そのものが求まる。しかしその逆算は行わない。0.50は将来
見直され得る閾値であり、現在の閾値から過去の事実を再構成すると、閾値変更の
瞬間に過去レコードの表示が遡って誤りになる。保存済みのmethod / code / message /
actual_value / reference_value以外の値は導出しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.valuation_spread_observation import ObservationStatus

_FACTS_KEY = "valuation_outlier_exclusions"


class DownsideScenarioKind(StrEnum):
    """下方除外の意味の分類。

    すべてを「弱気シナリオ」と一括りにすると、「算出そのものが壊れている可能性が
    高い値」と「方式間で見解が割れている値」の区別が失われるため、除外理由の
    意味を保持する。
    """

    EXTREME_RELATIVE_TO_CURRENT_PRICE = "EXTREME_RELATIVE_TO_CURRENT_PRICE"
    METHOD_DIVERGENT_DOWNSIDE = "METHOD_DIVERGENT_DOWNSIDE"
    HISTORICAL_PRICE_RELATIVE_DOWNSIDE = "HISTORICAL_PRICE_RELATIVE_DOWNSIDE"


# 保存済みexclusion codeとscenario_kindの対応。ここに無いcodeは(上方除外・
# 未知コードのいずれであっても)downside scenarioとして扱わない(fail-closed)。
_DOWNWARD_EXCLUSION_KINDS: dict[str, DownsideScenarioKind] = {
    "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE": (
        DownsideScenarioKind.EXTREME_RELATIVE_TO_CURRENT_PRICE
    ),
    "EXTREME_LOW_RELATIVE_TO_MEDIAN": DownsideScenarioKind.METHOD_DIVERGENT_DOWNSIDE,
    "BELOW_52_WEEK_LOW": DownsideScenarioKind.HISTORICAL_PRICE_RELATIVE_DOWNSIDE,
}

UNAVAILABLE_NO_EXCLUSION_SNAPSHOT = (
    "外れ値除外の判定時点スナップショットが保存されていません"
    "(BUY判定記録でないか、スナップショット導入前の記録)"
)
UNAVAILABLE_ACTUAL_VALUE_MISSING = (
    "下方除外の元値がスナップショットに保存されておらず、"
    "除外された評価額を推測なしで復元できません"
)


@dataclass(frozen=True)
class DownsideValuationScenario:
    """適正価格の集計から下方外れ値として除外された1方式の記録。

    保存済みexclusion entryの写しであり、新しい値は一切算出しない。
    """

    method: str
    code: str
    #: 判定時点の自由文スナップショット。表示・監査でそのまま使う値であり、
    #: parseして分岐条件にしてはならない(既存unusable_reasonと同じ原則)。
    message: str | None
    #: 除外された、その方式の適正価格そのもの(persisted actual_value)。
    fair_value: Decimal
    #: 判定時点に実際に発火した閾値額(persisted reference_value)。
    #: 保存されていない世代・条件があるためNoneを許容する。
    reference_value: Decimal | None
    scenario_kind: DownsideScenarioKind
    #: 常にFalse。この一覧はanchor集計から除外された値だけを含む。
    #: 「除外された事実」を型の上でも明示するために保持する。
    used_in_anchor: bool = False


@dataclass(frozen=True)
class DownsideValuationObservation:
    """1 Recommendationから導出したdownside scenarioの集合。

    statusがAVAILABLEでscenariosが空であることは「下方除外が0件だった」という
    正当な観測結果であり、OBSERVATION_UNAVAILABLE(観測できない)とは異なる。
    このscenariosの空判定だけで両者を区別しないこと。
    """

    status: ObservationStatus
    scenarios: tuple[DownsideValuationScenario, ...] = ()
    unavailable_reason: str | None = None


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _unavailable(reason: str) -> DownsideValuationObservation:
    return DownsideValuationObservation(
        status=ObservationStatus.OBSERVATION_UNAVAILABLE,
        unavailable_reason=reason,
    )


def derive_downside_valuation_observation(
    recommendation: Recommendation,
) -> DownsideValuationObservation:
    """保存済みRecommendationからdownside scenarioを導出する。

    入力は判定時点スナップショットのみで、現在のconfig・市場データは参照しない。
    同じRecommendationからは常に同じ結果を返す(決定的)。
    """
    facts = recommendation.buy_score_input_facts or {}
    entries = facts.get(_FACTS_KEY)
    if not isinstance(entries, list):
        # キー自体が無い(スナップショット導入前の記録・SELL側の記録)。
        # 「下方除外は無かった」と読み替えず、観測不能として区別する。
        return _unavailable(UNAVAILABLE_NO_EXCLUSION_SNAPSHOT)

    scenarios: list[DownsideValuationScenario] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        method = entry.get("method")
        if not isinstance(code, str) or not isinstance(method, str):
            continue
        kind = _DOWNWARD_EXCLUSION_KINDS.get(code)
        if kind is None:
            # 上方除外、および将来追加され得る未知コード。下方と断定できない
            # ものをdownside scenarioとして提示しない(fail-closed)。
            continue
        fair_value = _to_decimal(entry.get("actual_value"))
        if fair_value is None:
            # 下方除外として認識できたが、除外された金額そのものが復元できない。
            # 一部だけを提示すると件数・水準を過小に見せるため、観測全体を
            # 不能として扱う(値を捏造しない)。
            return _unavailable(UNAVAILABLE_ACTUAL_VALUE_MISSING)
        message = entry.get("message")
        scenarios.append(
            DownsideValuationScenario(
                method=method,
                code=code,
                message=message if isinstance(message, str) else None,
                fair_value=fair_value,
                reference_value=_to_decimal(entry.get("reference_value")),
                scenario_kind=kind,
            )
        )

    # 並び順は保存順(=判定時点の監査順)のまま維持する。値の昇順へ並べ替えたり、
    # 最も低い1件へ畳み込んだりしない(1 Recommendationに複数の下方除外が
    # 存在しうる)。
    return DownsideValuationObservation(
        status=ObservationStatus.AVAILABLE,
        scenarios=tuple(scenarios),
    )

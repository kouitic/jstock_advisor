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
UNAVAILABLE_METHOD_MISSING = (
    "下方除外の対象方式がスナップショットに保存されておらず、"
    "どの算出方式が除外されたのかを推測なしで復元できません"
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


def _to_finite_decimal(value: object) -> Decimal | None:
    """保存値をDecimalへ復元する。復元できない値・非有限値はNoneを返す。

    `Decimal("NaN")` / `Decimal("Infinity")` はDecimalとしては構築できてしまうため、
    金額として扱う前に有限であることを要求する(非有限値を表示層へ渡さない)。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        restored = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return restored if restored.is_finite() else None


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

    # 判定は必ず「まずcodeを見て下方かどうかを決め、下方と分かった後に
    # scenarioとして成立するかを検証する」順序で行う。
    # 下方と断定できないentryを落とすこと(skip)と、下方と分かったentryが
    # 復元できないこと(観測全体をUNAVAILABLE)は別の意味を持つため、
    # 両者を同じ扱いにしない。
    scenarios: list[DownsideValuationScenario] = []
    for entry in entries:
        # A: entry自体が構造化されていない。下方かどうか判定できないためskip。
        if not isinstance(entry, dict):
            continue
        # B: codeが無い/不正。同じく下方かどうか判定できないためskip。
        code = entry.get("code")
        if not isinstance(code, str) or not code:
            continue
        # C: 上方除外、および将来追加され得る未知コード。下方と断定できない
        # ものをdownside scenarioとして提示しない(fail-closed)。
        kind = _DOWNWARD_EXCLUSION_KINDS.get(code)
        if kind is None:
            continue

        # D: 下方除外だと認識できた。ここから先の欠損は「無かったこと」に
        # できない。1件でも復元できなければ、一部だけを提示して件数・水準を
        # 過小に見せるより、観測全体を不能として扱う(値を捏造しない)。
        method = entry.get("method")
        if not isinstance(method, str) or not method.strip():
            return _unavailable(UNAVAILABLE_METHOD_MISSING)
        fair_value = _to_finite_decimal(entry.get("actual_value"))
        if fair_value is None:
            return _unavailable(UNAVAILABLE_ACTUAL_VALUE_MISSING)

        message = entry.get("message")
        scenarios.append(
            DownsideValuationScenario(
                method=method,
                code=code,
                message=message if isinstance(message, str) else None,
                fair_value=fair_value,
                # reference_valueは補助情報であり、欠損・不正・非有限でも
                # scenarioは成立する。ただし非有限値を表示層へ渡さないため
                # Noneへ落とす(閾値額を推測で補完しない)。
                reference_value=_to_finite_decimal(entry.get("reference_value")),
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

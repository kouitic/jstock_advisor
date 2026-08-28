"""valuation集約のshadow仮説レジストリ(Issue #20 Phase C)。

現行のvaluation方式を一切変更せず、保存済みRecommendationの判定時点値だけを
入力として複数の集約・grouping仮説を並行計算するための宣言的定義。
本番判定・本番scoring・Recommendation保存へは一切接続しない。

【設計原則(承認済み)】
- PREDEFINED(事前定義)とEXPLORATORY_DATA_DERIVED(既存データ観察から
  導出された探索仮説)をoriginで区別する。探索仮説は候補生成に使った
  サンプルでの性能評価がselection biasとなるため、将来の性能比較では
  探索サンプルとvalidationサンプルを同一視しないこと(分析側の責務)。
- SELLのusability閾値(spread 2.0/最少2件)は判定時点値としてRecommendationへ
  保存されていないため、historical factではなくshadow計算parameterとして
  ここで版管理する(#21保存のfair_value_usable_for_trading_judgment /
  fair_value_unusable_reason_codeがhistorical fact。両者を混同しない)。
- 集約式はvh1時点の本番式(valuation_methods.py)を写した版管理された
  shadow定義であり、本番コードを直接importしない(本番側の将来変更が
  shadow結果を静かに変えないため)。同値性はテストで固定する。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# 仮説集合の世代。仮説の追加・定義変更時に更新し、export metadataへ刻む。
VALUATION_HYPOTHESIS_SET_VERSION = "vh1"

# SELL側usabilityのshadow計算parameter(判定時点値として未保存のため
# historical factではない。config/valuation_rules.yamlのvh1時点値を写した定数)。
SELL_USABILITY_SHADOW_PARAMS: dict[str, float | int] = {
    "max_method_spread_ratio": 2.0,
    "min_methods_required": 2,
}


class HypothesisOrigin(StrEnum):
    PREDEFINED = "PREDEFINED"
    EXPLORATORY_DATA_DERIVED = "EXPLORATORY_DATA_DERIVED"


@dataclass(frozen=True)
class ValuationHypothesis:
    """1つのgrouping仮説。clusters=Noneは「各方式を独立票として扱う」
    (現行方式の母集団解釈)。clustersは標準5方式の分割で、群内はmedianへ
    縮約したうえで群を1票として集約する。"""

    hypothesis_id: str
    origin: HypothesisOrigin
    clusters: tuple[frozenset[str], ...] | None
    description: str
    derivation_note: str | None = None


PREDEFINED_HYPOTHESES: tuple[ValuationHypothesis, ...] = (
    ValuationHypothesis(
        hypothesis_id="H_A_INDEPENDENT_METHODS",
        origin=HypothesisOrigin.PREDEFINED,
        clusters=None,
        description="現行どおり各方式を独立票として扱う(baseline)",
    ),
    ValuationHypothesis(
        hypothesis_id="H_C1A_EARNINGS_TRIO_3GROUP",
        origin=HypothesisOrigin.PREDEFINED,
        clusters=(
            frozenset({"per", "pbr", "target_yield"}),
            frozenset({"historical_range"}),
            frozenset({"dcf"}),
        ),
        description="収益力連動3方式を1群にまとめる3群仮説",
    ),
    ValuationHypothesis(
        hypothesis_id="H_C1B_MULTIPLE_PAIR_3GROUP",
        origin=HypothesisOrigin.PREDEFINED,
        clusters=(
            frozenset({"per", "pbr"}),
            frozenset({"target_yield"}),
            frozenset({"dcf", "historical_range"}),
        ),
        description="倍率系ペア+配当+内在/市場系の3群仮説",
    ),
    ValuationHypothesis(
        hypothesis_id="H_D_PER_PBR_PAIR",
        origin=HypothesisOrigin.PREDEFINED,
        clusters=(
            frozenset({"per", "pbr"}),
            frozenset({"target_yield"}),
            frozenset({"dcf"}),
            frozenset({"historical_range"}),
        ),
        description="PER/PBRのみ1証拠群として扱い他は独立票のまま",
    ),
)

EXPLORATORY_HYPOTHESES: tuple[ValuationHypothesis, ...] = (
    ValuationHypothesis(
        hypothesis_id="H_X1_CORRELATION_CLUSTER_2026_08",
        origin=HypothesisOrigin.EXPLORATORY_DATA_DERIVED,
        clusters=(
            frozenset({"per", "pbr", "historical_range"}),
            frozenset({"target_yield"}),
            frozenset({"dcf"}),
        ),
        description="実測相関クラスタ仮説(探索由来。性能評価時はsample分離必須)",
        derivation_note=(
            "2026-08-27の本番526銘柄Spearman実測(PBR-historical 0.738、"
            "PER-PBR 0.548、target_yield負相関)から導出したdata-derived仮説"
        ),
    ),
)

ALL_HYPOTHESES: tuple[ValuationHypothesis, ...] = PREDEFINED_HYPOTHESES + EXPLORATORY_HYPOTHESES


# --- 集約式(vh1時点の本番式を写したshadow定義) -----------------------------


def weighted_median_equal(values: list[Decimal]) -> Decimal | None:
    """等重みのweighted median(valuation_methods._weighted_medianの
    method_weights均等時と同値。正の値のみ対象)。"""
    positive = sorted(v for v in values if v > 0)
    if not positive:
        return None
    half = len(positive) / 2
    cumulative = 0.0
    for value in positive:
        cumulative += 1.0
        if cumulative >= half:
            return value
    return positive[-1]


def trimmed_mean(values: list[Decimal], trim_fraction: float = 0.1) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    trim_count = int(n * trim_fraction)
    trimmed = ordered[trim_count : n - trim_count] if n - 2 * trim_count > 0 else ordered
    return sum(trimmed, Decimal("0")) / len(trimmed)


def percentile_40(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = 40 / 100 * (n - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, n - 1)
    fraction = Decimal(str(rank - lower_index))
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def simple_median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(statistics.median(values))


def simple_mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / len(values)


# anchor候補式の一覧(exportの列名になる)。MIN_WM_TMは現行のMEDIUM相当式。
ANCHOR_FORMULAS: tuple[str, ...] = (
    "weighted_median",
    "trimmed_mean",
    "min_wm_tm",
    "percentile_40",
    "median",
    "mean",
)

# shadow価格計算の代表式(現行MEDIUM confidence時の保守側式。仮説間比較の
# 基準を1本に固定するための宣言であり、優劣判断ではない)。
REPRESENTATIVE_ANCHOR_FORMULA = "min_wm_tm"


def compute_anchor_candidates(values: list[Decimal]) -> dict[str, Decimal | None]:
    """母集団(独立票または群縮約後の票)に対する全anchor候補を計算する。"""
    wm = weighted_median_equal(values)
    tm = trimmed_mean(values)
    return {
        "weighted_median": wm,
        "trimmed_mean": tm,
        "min_wm_tm": min(wm, tm) if wm is not None and tm is not None else None,
        "percentile_40": percentile_40(values),
        "median": simple_median(values),
        "mean": simple_mean(values),
    }


def reduce_population(
    values_by_method: dict[str, Decimal], clusters: tuple[frozenset[str], ...] | None
) -> list[tuple[str, Decimal]]:
    """grouping仮説を母集団へ適用する。clusters=Noneなら方式そのまま。
    群は所属方式のうち値が存在するものの群内medianへ縮約する(値を持つ
    方式が無い群はスキップ=存在しない値を捏造しない)。母集団に含まれる
    未定義方式(分割へ属さない方式)は独立票のまま残す。ラベルは決定的
    (群はメンバー名昇順の連結、全体をラベル昇順で返す)。"""
    if clusters is None:
        return sorted(values_by_method.items())
    reduced: list[tuple[str, Decimal]] = []
    clustered_methods: set[str] = set()
    for cluster in clusters:
        members = sorted(m for m in cluster if m in values_by_method)
        clustered_methods.update(cluster)
        if not members:
            continue
        member_values = [values_by_method[m] for m in members]
        label = "+".join(members)
        median_value = simple_median(member_values)
        assert median_value is not None  # membersが非空のため常に算出可能
        reduced.append((label, median_value))
    for method, value in values_by_method.items():
        if method not in clustered_methods:
            reduced.append((method, value))
    return sorted(reduced)

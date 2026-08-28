"""valuation手法の依存関係taxonomy(Issue #20 Phase B1)。

各適正価格算出方式について、(1)評価原理(ValuationPrinciple)と、
(2)実コードが直接入力として使用しているデータ(ValuationDependencyTag)を
コード上の事実としてタグ付けする。Phase C(集約方式の比較検証)で任意の
グルーピング仮説(5方式=5票 / 相関クラスタ化 / タグ重複度ベース等)を
後付け計算するための観測用分類であり、本番判定へは一切使用しない。

【重要な設計原則】
- これは「依存関係の事実taxonomy」であり、クラスタ仮説を表現しない。
  原理は1方式1原理(5方式↔5原理)で、いかなる「同一票」グループも
  データモデルに焼き込まない。グルーピング仮説はPhase Cの分析側で
  hypothesis_id/version付きの宣言的定義として扱う。
- タグは実装が直接入力として使う値のみ(例: 現行PBRはbookValue×過去PBR
  中央値でありROEを入力に持たないため、PROFITABILITY等のタグを設けない)。
  倍率中央値(過去PER/PBR)が過去株価と過去EPS/BPSを内包する、配当が
  配当性向を介して利益と連動しうる、といった推移的・経済的な依存は
  仮説レベルの命題としてPhase Cで扱い、タグには含めない。
- 本番判定用のindependent_evidence_count等はこのモジュールから生成しない。
"""

from __future__ import annotations

from enum import StrEnum

# taxonomy定義の世代。方式・原理・タグの対応を変更する場合に更新する。
# Phase B1のRecommendationレコードには何も刻まない(観測=方式名と値の事実で
# ありtaxonomy非依存)。Phase Cのshadow出力を保存する際に、その出力側へ
# このversionとhypothesis_id/versionを刻む。
VALUATION_TAXONOMY_VERSION = "vt1"


class ValuationPrinciple(StrEnum):
    """評価原理。5方式と1対1対応(クラスタ情報を持たない)。"""

    EARNINGS_MULTIPLE = "EARNINGS_MULTIPLE"
    ASSET_MULTIPLE = "ASSET_MULTIPLE"
    SHAREHOLDER_RETURN = "SHAREHOLDER_RETURN"
    INTRINSIC_CASHFLOW = "INTRINSIC_CASHFLOW"
    MARKET_HISTORY = "MARKET_HISTORY"


class ValuationDependencyTag(StrEnum):
    """各方式が直接入力として使用するデータの種別(実コード基準)。"""

    EARNINGS = "EARNINGS"
    BOOK_VALUE = "BOOK_VALUE"
    DIVIDEND = "DIVIDEND"
    CASHFLOW = "CASHFLOW"
    MARKET_PRICE_HISTORY = "MARKET_PRICE_HISTORY"
    MARKET_MULTIPLE_HISTORY = "MARKET_MULTIPLE_HISTORY"


# FairValueMethodResult.methodの標準5方式(entities/valuation.pyのコメントと同一)
METHOD_PRINCIPLES: dict[str, ValuationPrinciple] = {
    "per": ValuationPrinciple.EARNINGS_MULTIPLE,
    "pbr": ValuationPrinciple.ASSET_MULTIPLE,
    "target_yield": ValuationPrinciple.SHAREHOLDER_RETURN,
    "dcf": ValuationPrinciple.INTRINSIC_CASHFLOW,
    "historical_range": ValuationPrinciple.MARKET_HISTORY,
}

# 直接入力の事実(実装の入力データから):
# - per: 平準化EPS(EARNINGS)× 過去PER中央値(MARKET_MULTIPLE_HISTORY)
# - pbr: 予想BPS=bookValue(BOOK_VALUE)× 過去PBR中央値(MARKET_MULTIPLE_HISTORY)
# - target_yield: 予想年間配当(DIVIDEND)÷ 目標利回り(設定値、データではない)
# - dcf: 営業CF・設備投資(CASHFLOW)(割引率・成長率は設定値)
# - historical_range: 52週安値・過去3年安値(MARKET_PRICE_HISTORY)
METHOD_DEPENDENCY_TAGS: dict[str, frozenset[ValuationDependencyTag]] = {
    "per": frozenset(
        {ValuationDependencyTag.EARNINGS, ValuationDependencyTag.MARKET_MULTIPLE_HISTORY}
    ),
    "pbr": frozenset(
        {ValuationDependencyTag.BOOK_VALUE, ValuationDependencyTag.MARKET_MULTIPLE_HISTORY}
    ),
    "target_yield": frozenset({ValuationDependencyTag.DIVIDEND}),
    "dcf": frozenset({ValuationDependencyTag.CASHFLOW}),
    "historical_range": frozenset({ValuationDependencyTag.MARKET_PRICE_HISTORY}),
}


def principle_for_method(method: str) -> ValuationPrinciple:
    """方式名→評価原理。未知の方式は黙ってスキップせず明示的にエラーとする
    (taxonomy未整備の方式を分析から静かに落とさないため)。"""
    try:
        return METHOD_PRINCIPLES[method]
    except KeyError as exc:
        raise ValueError(f"valuation taxonomyに未登録の方式です: {method!r}") from exc


def dependency_tags_for_method(method: str) -> frozenset[ValuationDependencyTag]:
    """方式名→直接入力タグ集合。未知の方式は明示的にエラーとする。"""
    try:
        return METHOD_DEPENDENCY_TAGS[method]
    except KeyError as exc:
        raise ValueError(f"valuation taxonomyに未登録の方式です: {method!r}") from exc

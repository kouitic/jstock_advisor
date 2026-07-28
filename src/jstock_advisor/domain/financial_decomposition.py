"""財務悪化判定の改善: 営業キャッシュフローの要因分解と業績予想修正の検出
(要求仕様4節)。

営業キャッシュフローが低下しても、運転資本(売上債権・棚卸資産・仕入債務)や
一過性支払い(M&A関連等)が主因の場合は、即座に投資前提悪化と断定しない。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.interfaces.types import CashflowDecomposition, Disclosure

_GUIDANCE_REVISION_KEYWORDS = (
    "業績予想の修正",
    "業績予想の下方修正",
    "業績予想を修正",
    "配当予想の修正",
    "通期業績予想の修正",
)

# 運転資本・一過性要因の絶対値合計が税引前利益の絶対値のこの倍率を超える場合、
# 営業CFの変動は運転資本・一過性要因が主因である可能性が高いと判断する。
_WORKING_CAPITAL_DOMINANCE_RATIO = 1.0


def is_fundamentally_driven(decomposition: CashflowDecomposition | None) -> bool | None:
    """営業CFの変動が本業要因(税引前利益)主導か、運転資本・一過性要因主導かを判定する。

    分解データが無い、または一部項目が欠損している場合は判定不能(None)を返す
    (推測で補完しない)。呼び出し側は、Noneの場合に元の継続悪化シグナルを
    自動的に取り下げてはならない(データ不足を理由に安全側の判定を弱めない)。
    """
    if decomposition is None or decomposition.pretax_income is None:
        return None

    working_capital_components: list[Decimal | None] = [
        decomposition.receivables_change,
        decomposition.inventory_change,
        decomposition.payables_change,
        decomposition.one_time_items,
        decomposition.ma_related_items,
        decomposition.other_working_capital,
    ]
    if any(c is None for c in working_capital_components):
        return None

    working_capital_total = sum(
        (abs(c) for c in working_capital_components if c is not None), Decimal("0")
    )

    if decomposition.pretax_income == 0:
        return working_capital_total == 0

    ratio = working_capital_total / abs(decomposition.pretax_income)
    return ratio < Decimal(str(_WORKING_CAPITAL_DOMINANCE_RATIO))


def has_guidance_revision_disclosure(disclosures: list[Disclosure]) -> bool:
    """業績予想の修正(下方修正含む)に関する開示があるかどうかを、開示カテゴリ・
    タイトルのキーワード一致のみで判定する(具体的な修正幅の数値化は行わない
    -- EDINET開示文の構造化パースが未実装のため。要求仕様4節のフィージビリティ制約)。
    """
    for disclosure in disclosures:
        haystack = f"{disclosure.category or ''} {disclosure.title}"
        if any(keyword in haystack for keyword in _GUIDANCE_REVISION_KEYWORDS):
            return True
    return False

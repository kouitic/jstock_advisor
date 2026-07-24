"""EDINET書類ZIP内のCSV(XBRL_TO_CSV)をパースし、指定要素IDの値を抽出する。

要素IDは有価証券報告書・半期報告書の「経営指標等の推移」表(SummaryOfBusinessResults
サフィックスを持つ要素)を主な対象とする。連結決算の会社では同じ日本語項目名でも
連結(コンテキストIDに"NonConsolidated"を含まない)と個別(含む)の両方が存在しうるため、
どちらの値かを明示して返す(実測検証により、取り違えるとEPS/BPS等が最大40%以上
ズレることを確認済み)。
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_CURRENT_PERIOD_LABELS = {"当期", "当期末"}


@dataclass(frozen=True)
class EdinetCsvRow:
    element_id: str
    item_name: str
    context_id: str
    relative_period: str
    consolidated_or_individual: str
    period_or_instant: str
    unit_id: str
    unit_label: str
    value: str


@dataclass(frozen=True)
class ExtractedValue:
    value: Decimal
    is_consolidated: bool


def extract_main_document_rows(zip_bytes: bytes) -> list[EdinetCsvRow] | None:
    """ZIPから本文(jpcrp*)のCSVを探して行データのリストを返す。見つからなければNone。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None

    csv_names = [
        n for n in zf.namelist() if n.startswith("XBRL_TO_CSV/jpcrp") and n.endswith(".csv")
    ]
    if not csv_names:
        return None

    raw = zf.read(csv_names[0])
    text: str | None = None
    for encoding in ("utf-16", "utf-8-sig", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = list(reader)
    if len(rows) < 2:
        return None

    result: list[EdinetCsvRow] = []
    for r in rows[1:]:
        if len(r) < 9:
            continue
        result.append(
            EdinetCsvRow(
                element_id=r[0],
                item_name=r[1],
                context_id=r[2],
                relative_period=r[3],
                consolidated_or_individual=r[4],
                period_or_instant=r[5],
                unit_id=r[6],
                unit_label=r[7],
                value=r[8],
            )
        )
    return result


def find_current_value(
    rows: list[EdinetCsvRow], element_name: str, prefer_consolidated: bool = True
) -> ExtractedValue | None:
    """要素名(名前空間プレフィックスを除いたローカル名)の当期値を取得する。

    prefer_consolidatedがTrueなら連結を優先し、無ければ個別にフォールバックする
    (逆にFalseなら個別を優先)。どちらの基準の値かはExtractedValue.is_consolidatedで判別できる。
    """
    candidates = [
        r
        for r in rows
        if r.element_id.split(":")[-1] == element_name
        and r.relative_period in _CURRENT_PERIOD_LABELS
    ]
    if not candidates:
        return None

    consolidated = [r for r in candidates if "NonConsolidated" not in r.context_id]
    individual = [r for r in candidates if "NonConsolidated" in r.context_id]

    if prefer_consolidated:
        chosen, is_consolidated = (consolidated, True) if consolidated else (individual, False)
    else:
        chosen, is_consolidated = (individual, False) if individual else (consolidated, True)

    if not chosen:
        return None

    try:
        return ExtractedValue(value=Decimal(chosen[0].value), is_consolidated=is_consolidated)
    except (InvalidOperation, ValueError):
        return None

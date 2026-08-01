"""candidate_universe_provider のCSV実装(ウォッチリスト自動追加機能)。

プロジェクト内CSV(1列目にstock_code)を候補銘柄ユニバースとして読み込む。
東証プライム全銘柄等の自動取得は行わない(新規API取得を要するため今回は対象外)。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseError,
    CandidateUniverseItem,
    CandidateUniverseResult,
)

# services/csv_import_service.py・watchlist_csv_import_service.pyと同じ正規表現・
# 正規化方針(.strip()のみ)を踏襲する(このリポジトリに証券コード正規化の共通関数は
# 存在せず、両ファイルとも同一定数を独自に重複定義しているため、その既存の流儀に合わせる)。
_STOCK_CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{4}$")

REQUIRED_COLUMNS = {"stock_code"}


class CsvCandidateUniverseProvider:
    def __init__(self, csv_path: Path) -> None:
        self._csv_path = csv_path

    def get_candidate_universe(self) -> CandidateUniverseResult:
        if not self._csv_path.exists():
            raise CandidateUniverseError(
                f"候補銘柄ユニバースCSVが見つかりません: {self._csv_path}"
            )

        with self._csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise CandidateUniverseError("候補銘柄ユニバースCSVにヘッダー行がありません")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise CandidateUniverseError(
                    f"候補銘柄ユニバースCSVに必須列がありません: {sorted(missing)}"
                )

            raw_row_count = 0
            duplicate_count = 0
            invalid_code_count = 0
            seen: set[str] = set()
            items: list[CandidateUniverseItem] = []

            for row in reader:
                stock_code = (row.get("stock_code") or "").strip()
                if not stock_code:
                    continue  # 空行はraw_row_countにも含めない
                raw_row_count += 1

                if not _STOCK_CODE_PATTERN.match(stock_code):
                    invalid_code_count += 1
                    continue

                if stock_code in seen:
                    duplicate_count += 1
                    continue

                seen.add(stock_code)
                # CSVはstock_code以外のメタデータを持たないため、他フィールドは
                # 常にNone/Falseとする(候補ユニバース本格対応でのItem化対応)。
                items.append(CandidateUniverseItem(stock_code=stock_code))

        return CandidateUniverseResult(
            items=items,
            raw_row_count=raw_row_count,
            duplicate_count=duplicate_count,
            invalid_code_count=invalid_code_count,
            selected_count=len(items),
        )

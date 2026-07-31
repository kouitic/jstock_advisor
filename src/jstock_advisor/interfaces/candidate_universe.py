"""candidate_universe_provider インターフェース(ウォッチリスト自動追加機能)。

週次スクリーニングの対象となる候補銘柄コード一覧を提供する。評価ロジック・
除外判定は一切持たない(スクリーニング対象の"母集団"を定義するだけの責務)。

初期実装はCSVベース(CsvCandidateUniverseProvider)。将来、東証プライム全銘柄・
TOPIX1000・JPX400等の自動取得Providerへ差し替える場合も、このインターフェースを
実装するだけで済むようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CandidateUniverseResult:
    """候補銘柄ユニバースの取得結果。"""

    stock_codes: list[str]
    """正規化・重複除去・形式検証済みの候補コード一覧(出現順を維持)。"""

    raw_row_count: int
    """空行を除いた元データの行数。"""

    duplicate_count: int
    """重複のため除去した件数。"""

    invalid_code_count: int
    """形式不正(空白・4桁英数字パターン不一致等)のため除外した件数。"""


class CandidateUniverseProvider(Protocol):
    def get_candidate_universe(self) -> CandidateUniverseResult:
        """スクリーニング対象銘柄コード一覧を取得する。

        個別行の不正(空白・形式不正・重複)は致命的エラーにせず除外してカウントする。
        ユニバース取得自体が失敗する場合(ファイル未配置・必須列欠如等)は
        CandidateUniverseErrorを送出する。
        """
        ...


class CandidateUniverseError(Exception):
    """ユニバース取得自体が失敗した場合の例外。個別行の不正とは区別する。"""

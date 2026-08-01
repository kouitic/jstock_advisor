"""candidate_universe_provider インターフェース(ウォッチリスト自動追加機能)。

週次スクリーニングの対象となる候補銘柄一覧を提供する。評価ロジック・除外判定は
一切持たない(スクリーニング対象の"母集団"を定義するだけの責務)。

候補ユニバース本格対応(2026-08、第6版修正プラン)で、東証プライム+スタンダード
全銘柄を自動取得するJPX Provider(providers/candidate_universe/jpx_impl.py)を
追加した。CSVベースのCsvCandidateUniverseProviderは小規模検証用にそのまま残す。
JPX400構成銘柄フラグ(is_jpx400_member)はスコアリングには使わず、監査・将来の
絞り込み用のメタデータとして保持するのみ。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CandidateUniverseItem:
    """候補銘柄1件分のメタデータ(候補ユニバース本格対応で導入)。"""

    stock_code: str
    stock_name: str | None = None
    market_segment: str | None = None
    industry_33_code: str | None = None
    industry_33_name: str | None = None
    industry_17_code: str | None = None
    industry_17_name: str | None = None
    size_code: str | None = None
    size_name: str | None = None
    is_jpx400_member: bool = False


@dataclass(frozen=True)
class CandidateUniverseResult:
    """候補銘柄ユニバースの取得結果。"""

    items: list[CandidateUniverseItem] = field(default_factory=list)
    """正規化・重複除去・形式検証済みの候補一覧(出現順を維持)。"""

    raw_row_count: int = 0
    """空行を除いた元データの行数。"""

    duplicate_count: int = 0
    """重複のため除去した件数。"""

    invalid_code_count: int = 0
    """形式不正(空白・4桁英数字パターン不一致等)のため除外した件数。"""

    selected_count: int = 0
    """market_segment等の絞り込み後、最終的にitemsへ残った件数(=len(items))。"""

    # --- 候補ユニバース本格対応で追加。CsvCandidateUniverseProviderでは常にNone
    # (静的CSVには元データの公開日という概念が無いため)。JPX Providerは8節の
    # source_date基準の鮮度判定に使う。 ---
    source_date: dt.date | None = None
    fetched_at: dt.datetime | None = None
    cache_last_modified: dt.datetime | None = None
    cache_age_hours: float | None = None

    @property
    def stock_codes(self) -> list[str]:
        """既存の呼び出し側(WatchlistCandidateCollector等)との後方互換用。"""
        return [item.stock_code for item in self.items]


class CandidateUniverseProvider(Protocol):
    def get_candidate_universe(self) -> CandidateUniverseResult:
        """スクリーニング対象銘柄一覧を取得する。

        個別行の不正(空白・形式不正・重複)は致命的エラーにせず除外してカウントする。
        ユニバース取得自体が失敗する場合(ファイル未配置・必須列欠如・キャッシュ
        最大許容経過時間の超過等)はCandidateUniverseErrorを送出する。
        """
        ...


class CandidateUniverseError(Exception):
    """ユニバース取得自体が失敗した場合の例外。個別行の不正とは区別する。"""

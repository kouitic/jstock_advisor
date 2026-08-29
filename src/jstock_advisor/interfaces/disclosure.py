"""disclosure_provider インターフェース。

Issue #53 Phase B2: `get_disclosures()`の戻り値を`DisclosureQueryResult`へ変更した。
従来は`list[Disclosure]`のみを返していたため、空リストが

  - 取得できて対象開示が0件だった(= 開示リスクなし)
  - そもそも取得できなかった(= 調査できていない)

のどちらなのかを呼び出し側が区別できず、取得失敗が「クリーン」として
判定を通過していた。並行APIは設けず、既存の契約自体を置き換える
(旧list経路が残存して片方だけ使われる事故を防ぐため)。

なお本モジュールが公開するのは「事実(取得できたか)」のみであり、
それを受けて除外するか・通知するか等の判断はdomain/service側の責務とする。
infrastructure層のEdinet固有enum(EdinetFetchStatus等)はここへ持ち込まず、
provider実装が境界で変換する。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from jstock_advisor.interfaces.types import Disclosure


class DisclosureAvailability(StrEnum):
    """開示情報を調査できたかどうか(開示の有無ではない)。"""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class DisclosureUnavailableReason(StrEnum):
    """UNAVAILABLEの内訳(運用上の切り分け用。判定側の扱いはいずれも同じ)。

    provider実装がデータ提供元固有の失敗種別をこの3値へ正規化する。
    APIキー等の秘密情報は決して含めない。
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"  # 認証情報未設定等、恒久的な構成不備
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"  # timeout・HTTPエラー・ダウンロード失敗等
    OTHER = "OTHER"


@dataclass(frozen=True)
class DisclosureQueryResult:
    """開示情報の取得結果。

    AVAILABLE + disclosures=[] は「取得成功・対象開示なし」を意味する正常な結果で、
    開示リスクなしとして扱ってよい。UNAVAILABLEのdisclosuresは常に空であり、
    **判定材料として使ってはならない**(空リストからavailabilityを推測しないこと)。
    """

    availability: DisclosureAvailability
    disclosures: list[Disclosure]
    unavailable_reason: DisclosureUnavailableReason | None = None

    @property
    def is_available(self) -> bool:
        return self.availability is DisclosureAvailability.AVAILABLE

    @classmethod
    def available(cls, disclosures: list[Disclosure]) -> DisclosureQueryResult:
        return cls(DisclosureAvailability.AVAILABLE, disclosures)

    @classmethod
    def unavailable(cls, reason: DisclosureUnavailableReason) -> DisclosureQueryResult:
        return cls(DisclosureAvailability.UNAVAILABLE, [], reason)


class DisclosureProvider(Protocol):
    def get_disclosures(self, stock_code: str, since: dt.date) -> DisclosureQueryResult:
        """適時開示・決算短信等を取得する。

        取得できなかった場合はUNAVAILABLEを返す(空リストで代用しない)。
        """
        ...

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        """次回決算発表予定日を取得する。不明であればNone。"""
        ...

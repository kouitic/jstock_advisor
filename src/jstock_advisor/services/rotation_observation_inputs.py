"""資金入替の観測の『入力の組み立て』(Issue #604 P-b2。#128 R2。dormant)。

保有の一覧・保有ごとの評価記録・記録が指す Recommendation を、注入された ports から読み、P-b1 の
`HoldingInput` の列へ組み立てる。設計: #604 P-b 設計 rev6(…6169320796)・rev8(…6169691749)。
**domain と標準ライブラリだけを import する**(具体の client・repository・boto3・infrastructure を
import しない。ports は Protocol で注入)。時計・環境変数・設定を読まない(残り時間は注入)。
どこからも import されない(dormant。配線・IAM・Scan の adapter は P-b4)。

## 守ること

* ★ 保有 ID は『引くキー』としてだけ使い、結果の `HoldingInput` には持たない ★
  (識別子は集計の前に捨てる)。
  例外・ログへも出さない。
* ★ 失敗の隔離 ★: 保有 1 件の記録の読取の失敗は、その保有だけ `record_read_failed`。
  Recommendation の
  読取の失敗は、その id を指す保有だけ `recommendation_read_failed`。他の保有へ伝えない。
* ★ 総予算(rev8 SHOULD-3)★: 保有ごとの読取の前に `remaining_seconds()` を見て、書込の予約時間
  (`reserve_seconds`)まで残らなければ、★ 部分結果にせず ★ `None`(= 読取の失敗)を返す。
  P-b1 の `observe_day` が READ_FAILED の日にし、最後の回なら gap の記録が残る(観測が消えない)。
* ★ 上限 ★: 保有が `MAX_HOLDINGS` を超えるときは、1 件も読まずに『上限超過』を返す(呼び出し回数の
  暴走を防ぐ。P-b1 の `observe_day` が INPUT_LIMIT_EXCEEDED にする)。
* 保有の列挙(`HoldingIdsPort`)は Protocol。本番の Scan の adapter はここに無い
  (P-b4。USER の IAM 承認の後)。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final, Protocol

from jstock_advisor.domain.signals.rotation_observation import MAX_HOLDINGS, HoldingInput
from jstock_advisor.domain.signals.rotation_observation_reads import (
    RecommendationsRead,
    RecordRead,
    RecordStatus,
    RotationInputsContractError,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import EligibilityWindow


class HoldingIdsPort(Protocol):
    def holding_ids(self) -> Sequence[str] | None:
        """保有の ID の一覧。列挙に失敗したら None。"""
        ...


class HoldingRecordPort(Protocol):
    def latest_in_window(self, holding_id: str, window: EligibilityWindow) -> RecordRead:
        """窓の内側の最新の評価記録 1 件。例外にせず `RecordRead` で返す。"""
        ...


class RecommendationPort(Protocol):
    def get_many(self, ids: Sequence[str]) -> RecommendationsRead:
        """id から Recommendation の事実を一括で引く。例外にせず `RecommendationsRead` で返す。"""
        ...


#: 上限超過のとき、1 件も読まずに返す印(中身を持たない)。
#: `observe_day` が件数だけで上限超過と判断する
_OVER_LIMIT_MARK: Final = HoldingInput(None, {})


def _has_budget(remaining_seconds: Callable[[], float], reserve_seconds: float) -> bool:
    return remaining_seconds() > reserve_seconds


def assemble_holding_inputs(
    *,
    window: EligibilityWindow,
    holding_ids: HoldingIdsPort,
    records: HoldingRecordPort,
    recommendations: RecommendationPort,
    remaining_seconds: Callable[[], float],
    reserve_seconds: float,
) -> tuple[HoldingInput, ...] | None:
    """窓の内側の保有の記録と推奨から、保有ごとの `HoldingInput` を組み立てる。

    戻り値: 保有ごとの入力の列 / `None` = 保有の列挙・総予算で読めなかった(部分結果にしない) /
    `MAX_HOLDINGS + 1` 件の中身の無い列 = 上限超過(1 件も読んでいない)。
    ports は例外にしない契約だが、守られない場合も 1 件の失敗として隔離する。
    """
    if isinstance(reserve_seconds, bool) or not isinstance(reserve_seconds, (int, float)):
        raise RotationInputsContractError("reserve_seconds must be a number")
    if not _has_budget(remaining_seconds, reserve_seconds):
        return None
    try:
        ids = holding_ids.holding_ids()
    except Exception:  # noqa: BLE001 - 列挙の失敗は読取の失敗(値・識別子を持たない)
        return None
    if ids is None:
        return None
    if len(ids) > MAX_HOLDINGS:
        return (_OVER_LIMIT_MARK,) * (MAX_HOLDINGS + 1)

    reads: list[RecordRead] = []
    for holding_id in ids:
        if not _has_budget(remaining_seconds, reserve_seconds):
            return None
        try:
            read = records.latest_in_window(holding_id, window)
        except Exception:  # noqa: BLE001 - 1 件の失敗として隔離する
            read = RecordRead(RecordStatus.FAILED)
        if not isinstance(read, RecordRead):
            read = RecordRead(RecordStatus.FAILED)
        reads.append(read)

    wanted = sorted(
        {i for read in reads if read.facts is not None for i in read.facts.recommendation_ids()}
    )
    result = RecommendationsRead()
    if wanted:
        if not _has_budget(remaining_seconds, reserve_seconds):
            return None
        try:
            result = recommendations.get_many(wanted)
            if not isinstance(result, RecommendationsRead):
                raise RotationInputsContractError("unexpected recommendations result")
        except Exception:  # noqa: BLE001 - 読めなかった id を指す保有だけが失敗になる
            result = RecommendationsRead(failed=frozenset(wanted))

    inputs: list[HoldingInput] = []
    for read in reads:
        if read.status is RecordStatus.FAILED:
            inputs.append(HoldingInput(None, {}, record_read_failed=True))
        elif read.facts is None:
            inputs.append(HoldingInput(None, {}))
        else:
            pointed = read.facts.recommendation_ids()
            inputs.append(
                HoldingInput(
                    read.facts,
                    {i: result.found[i] for i in pointed if i in result.found},
                    recommendation_read_failed=any(i in result.failed for i in pointed),
                )
            )
    return tuple(inputs)

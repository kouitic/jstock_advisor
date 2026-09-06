"""永続レコードのデコード失敗を、コレクション単位のポリシーに従って扱う機構
(Issue #63 / A-U1a)。

## 何のためにあるか

読み込みは常に全フィールド検証(`extra="forbid"`)を伴い、per-record の
エラー隔離・skip・quarantine がどこにも存在しない。そのため**不正なレコードが
1 件あるだけで、そのコレクションを読む経路が全滅する**。通知の再送判定
(`notification_log` の全件走査)もこの経路であり、1 件の不正で通知が
出せなくなる。

## なぜ一律に skip しないか

「読めたものだけ返す」は呼び出し側の前提を黙って変える。

    notification_log で 1 件 skip -> 過去の送信実績が見えない -> **重複通知**
    holdings / purchase_lots で 1 件 skip -> 保有比率・取得単価・損益が
                                            **静かに間違う**

投資助言システムでは「落ちる」より「静かに間違った金額で判断する」ほうが悪い。
したがって隔離は**コレクションごとの明示的なポリシー**とセットでなければ
入れられない(Issue #63 Phase A の C 節、承認済み設計 O-1')。

## 既定は STRICT

`RecordFailurePolicy.STRICT` は現行と同一の挙動(最初の失敗で元の例外を
そのまま送出)である。**宣言しなければ挙動は変わらない。**

## 本 module の位置づけ(PR-1 = A-U1a)

本 module は**追加のみ**であり、既存の呼び出し元を 1 つも変更しない。
`json_store` / `dynamodb_store` / `collection_store` をこの機構へ差し替えるのは
PR-2(A-U1b)であり、その時点で `LOCK_LEVEL_2` / 全領域 lock を取得する。
本 module を import する既存 module は現時点で 0 件である。

コレクションごとのポリシーは、本 module ではなく
`build_collection_store()` の引数として**呼び出し側(repository)が宣言する**
(PR-3 以降)。ポリシー表を永続化ストア層の内部に持つと、コレクションを 1 つ
`LENIENT` にするたびに S-17 の変更 = 全領域 lock が必要になるため。

## 出力に record の中身を含めない

失敗の記録は 面 / 所在 / 例外の種別 / 件数 だけで表す。レコード本文や
`ValidationError` の message は**出さない**(message は保有数量・氏名等の
フィールド値を含みうる)。これは Issue #131 で公開面の PII 検出について
確立した方針と同じである。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class RecordFailurePolicy(StrEnum):
    """デコードに失敗した 1 件をどう扱うか。"""

    STRICT = "STRICT"
    """元の例外をそのまま送出する(現行と同一の挙動)。既定。

    保有・取引・推奨のように、欠損が集計を静かに歪めるコレクション向け。
    """

    LENIENT = "LENIENT"
    """失敗した 1 件を skip し、残りを返す。

    監査ログ・cache のように、欠損しても判定結果が変わらないコレクション向け。
    skip した件数は必ず可視化する(黙って無視しない)。
    """

    FAIL_SAFE_SUPPRESS = "FAIL_SAFE_SUPPRESS"
    """skip したうえで、結果へ「判定不能」を立てる。

    `notification_log` の再送判定向け。STRICT では通知が出せなくなり、
    LENIENT では過去の送信実績を見落として重複送信になる。どちらも困るため、
    「判定できなかった」ことを呼び出し側へ伝え、**送信を見送らせる**。

    抑止するかどうかを決めるのは呼び出し側であり、本 module ではない
    (欠落と重複のどちらを受け入れるかは運用判断であるため)。
    """


@dataclass(frozen=True)
class RecordFailure:
    """デコードに失敗した 1 件。**レコードの中身は保持しない。**

    collection  コレクション名(テーブル名 / ファイル名)
    item_id     所在。是正できるように所在だけは残す
    error_type  例外クラス名。message は含めない(値を含みうるため)
    """

    collection: str
    item_id: str
    error_type: str


@dataclass(frozen=True)
class DecodeOutcome[RecordT]:
    """デコード結果。

    records      デコードできたレコード
    failures     失敗した 1 件ごとの記録
    undecidable  FAIL_SAFE_SUPPRESS で失敗があった場合に True。
                 「この結果を判断の根拠にしてはいけない」ことを表す
    """

    records: list[RecordT]
    failures: tuple[RecordFailure, ...]
    undecidable: bool

    @property
    def failure_count(self) -> int:
        return len(self.failures)


def emit_record_failure(failure: RecordFailure, policy: RecordFailurePolicy) -> None:
    """失敗 1 件を構造化ログへ出す。**レコードの中身は出さない。**"""
    logger.warning(
        "persistence record decode failed collection=%s item_id=%s error=%s policy=%s",
        failure.collection,
        failure.item_id,
        failure.error_type,
        policy,
    )


def emit_failure_summary(
    collection: str, scanned: int, failures: Iterable[RecordFailure]
) -> None:
    """走査単位の集計を出す。監視はこの件数を見る(平常時は 0)。"""
    failure_count = len(tuple(failures))
    if failure_count == 0:
        return
    logger.warning(
        "persistence decode summary collection=%s scanned=%d failed=%d",
        collection,
        scanned,
        failure_count,
    )


@dataclass
class RecordFailureCollector:
    """走査中の失敗を蓄積する。

    `iter_decoded_records()` のようなストリーミング経路で使う。
    `DecodeOutcome` を組み立てずに 1 件ずつ処理できるため、
    `iter_all()` のピークメモリ有界性(Issue #113)を壊さない。
    """

    collection: str
    policy: RecordFailurePolicy = RecordFailurePolicy.STRICT
    emit: Callable[[RecordFailure, RecordFailurePolicy], None] | None = emit_record_failure
    _failures: list[RecordFailure] = field(default_factory=list, init=False)

    @property
    def failures(self) -> tuple[RecordFailure, ...]:
        return tuple(self._failures)

    @property
    def undecidable(self) -> bool:
        """FAIL_SAFE_SUPPRESS で 1 件でも失敗していれば True。"""
        return bool(self._failures) and self.policy is RecordFailurePolicy.FAIL_SAFE_SUPPRESS

    def handle(self, item_id: str, error: Exception) -> None:
        """失敗 1 件を処理する。

        STRICT では**元の例外をそのまま送出する**。例外の型・message・
        traceback を包み直さないため、`ValidationError` を捕捉している
        呼び出し元の挙動が変わらない。
        """
        failure = RecordFailure(
            collection=self.collection,
            item_id=item_id,
            error_type=type(error).__name__,
        )
        if self.emit is not None:
            self.emit(failure, self.policy)
        if self.policy is RecordFailurePolicy.STRICT:
            raise error
        self._failures.append(failure)


def iter_decoded_records[RawT, RecordT](
    raw_items: Iterable[tuple[str, RawT]],
    decode: Callable[[RawT], RecordT],
    collector: RecordFailureCollector,
) -> Iterator[RecordT]:
    """`(item_id, raw)` の並びをデコードし、成功したものを 1 件ずつ返す。

    失敗の扱いは `collector` のポリシーに従う。STRICT では最初の失敗で
    例外が送出されるため、以降の要素は評価されない(現行と同じ打ち切り方)。

    遅延評価であり全件を保持しない。`iter_all()` から使ってもピークメモリが
    件数に比例しない(Issue #113)。
    """
    for item_id, raw in raw_items:
        try:
            yield decode(raw)
        except Exception as error:  # noqa: BLE001 - ポリシーに委ねるため広く捕捉する
            collector.handle(item_id, error)


def decode_records[RawT, RecordT](
    raw_items: Iterable[tuple[str, RawT]],
    decode: Callable[[RawT], RecordT],
    *,
    collection: str,
    policy: RecordFailurePolicy = RecordFailurePolicy.STRICT,
    emit: Callable[[RecordFailure, RecordFailurePolicy], None] | None = emit_record_failure,
) -> DecodeOutcome[RecordT]:
    """`(item_id, raw)` の並びを全件デコードして結果をまとめる。

    全件を materialize するため、`list_all()` のように元々全件を保持する
    経路で使う。ストリーミングが要る場合は `iter_decoded_records()` を使う。

    `policy` を省略すると `STRICT` であり、**現行と同一の挙動**になる。
    """
    collector = RecordFailureCollector(collection=collection, policy=policy, emit=emit)
    scanned = 0
    records: list[RecordT] = []
    for item_id, raw in raw_items:
        scanned += 1
        try:
            records.append(decode(raw))
        except Exception as error:  # noqa: BLE001 - ポリシーに委ねるため広く捕捉する
            collector.handle(item_id, error)
    emit_failure_summary(collection, scanned, collector.failures)
    return DecodeOutcome(
        records=records,
        failures=collector.failures,
        undecidable=collector.undecidable,
    )

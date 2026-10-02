"""datetime正規化(naive→aware UTC)の共通部品(S-25。Issue #576)。

内部で保持するdatetimeはすべてaware UTCであるべき(書き込み経路は常に
`dt.datetime.now(dt.UTC)`を起源とする)。naive値(想定外の旧データ・
テストデータ・未知の書き込み経路)が読み取り時に混入すると、aware値との
比較が`TypeError`になる(#66 F-L6)。本モジュールはその正規化契約を1箇所へ
集約する(`domain/jst.py`とは責務が分離される: jst.py=UTC→JST表示変換、
本モジュール=入力側のUTC正規化)。
"""

from __future__ import annotations

import datetime as dt


def normalize_to_aware_utc(value: dt.datetime) -> dt.datetime:
    """datetimeをtimezone-aware UTCへ正規化する。

    naive値(tzinfo無し)はローカルタイムゾーンとして暗黙解釈せず、UTCと
    みなす(保存値は歴史的にUTC基準のため。
    `notification_log_repository.py::_sent_at_as_utc()`と同一契約)。
    aware値は他タイムゾーンで表現されていてもUTCへ変換する。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)

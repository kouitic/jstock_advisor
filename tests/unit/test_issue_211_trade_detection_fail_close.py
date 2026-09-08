"""Issue #211(#70 F-B3): finalize recovery 経路の trade_detection_confirmed を fail-close にする。

`finalize-only recovery` の payload に `trade_detection_confirmed` が入っておらず、
子 handler の既定値が `True`(fail-open)だったため、**売買検知が未確定のまま
通常通知が送られていた**。

```
承認済みの設計 O-D 案 1(#70 issuecomment-5559963573 / DECIDED_BY = USER)

  U1  生産側  build_finalize_only_payload() が trade_detection_confirmed を必ず渡す
              recovery 経路では常に False(この実行では検知完了を確認していない、
              という事実どおり)
  U2  消費側  子 handler 2 箇所の既定値を False(fail-close)へ

★ U1 と U2 は 1 単位である。既定値だけ直しても payload を直さなければ
  recovery は「常に抑止」へ倒れるだけで意図した値は届かず、
  payload だけ直しても手動 invoke や将来の欠落に対する fail-close は得られない。
```

```
★ LineNotificationService のコンストラクタ既定(trade_detection_confirmed: bool = True)は
  **変更していない**。実測では生成箇所 10 か所のうち引数を渡しているのは 2 か所だけで、
  残り 8 経路がこの既定に依存している。False へ倒すと、それらすべてで
  重大リスク以外の通常通知が抑止される(本 Issue の scope 外)。
  U1 / U2 が入れば子 handler は常に明示値を渡すため、
  **本 Issue の経路では LNS の既定は参照されない。**
```

```
★ Production への failure injection は行わない。
  recovery はローカルで payload を組み立てて再現する。
  値はすべて架空値であり、実在の銘柄・所有者・保有データを含まない。
```
"""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.lambda_handlers._finalize_recovery import (
    FINALIZE_ONLY_ACTION,
    RECOVERY_ACTION_KEY,
    build_finalize_only_payload,
    is_recovery_event,
)

_KEY = "trade_detection_confirmed"


class _Family:
    """`record.family.value` だけを使うため最小の代用を置く(架空値)。"""

    value = "buy_candidate"


class _Context:
    mode = ExecutionMode.NORMAL


class _Record:
    """`build_finalize_only_payload()` が読む属性だけを持つ架空のレコード。"""

    batch_id = "batch-0000-0000"
    family = _Family()
    execution_context = _Context()


# --- T-1  U1: 生産側が必ずキーを載せる -------------------------------------


def test_finalize_only_payload_always_carries_the_key() -> None:
    """★ recovery payload に trade_detection_confirmed が含まれること。"""
    payload = build_finalize_only_payload(_Record())

    assert _KEY in payload


def test_finalize_only_payload_declares_detection_not_confirmed() -> None:
    """★ 値は **False** である(この実行では検知完了を確認していない、という事実)。

    recovery は停滞 batch を finalize するだけで
    TradeCooldownService.detect_and_apply() を走らせない。
    True を送ると「確認した」と偽ることになる。
    """
    payload = build_finalize_only_payload(_Record())

    assert payload[_KEY] is False


def test_finalize_only_payload_keeps_the_existing_keys() -> None:
    """既存 4 キーの意味を変えていないこと(recovery 専用の解決経路を作らない)。"""
    payload = build_finalize_only_payload(_Record())

    assert payload[RECOVERY_ACTION_KEY] == FINALIZE_ONLY_ACTION
    assert payload["batch_id"] == "batch-0000-0000"
    assert payload["batch_family"] == "buy_candidate"
    assert payload["execution_mode"] == ExecutionMode.NORMAL.value
    assert is_recovery_event(payload), "recovery event の判定が変わっていないこと"


def test_finalize_only_payload_still_rejects_incomplete_records() -> None:
    """family / execution_context が無いレコードは従来どおり例外(挙動不変)。"""

    class _Incomplete(_Record):
        family = None

    with pytest.raises(ValueError):
        build_finalize_only_payload(_Incomplete())


# --- T-2  U2: 消費側の既定が fail-close ------------------------------------
#
# 子 handler の handler() 全体を呼ぶと provider / LINE / DynamoDB へ到達するため、
# ここでは **その handler が実際に書いているのと同じ読み取り式**を対象にする。
# 式そのものが戻れば test_d7_trade_detection_confirmed_is_fail_closed
# (test_cross_pipeline_invariants.py)がソース上で検出する。両輪で固定する。


def _read_as_handler_does(event: dict[str, Any]) -> bool:
    """子 handler と同じ読み取り(既定 False = fail-close)。"""
    return event.get(_KEY, False)


def test_missing_key_falls_back_to_fail_close() -> None:
    """★ キーが無ければ False(= 検知未確認)として扱う。

    「確認できていない」と「確認して問題なし」を同じ値にしない、が本 Issue の要点。
    """
    assert _read_as_handler_does({"batch_id": "batch-0000-0000"}) is False


def test_recovery_payload_reaches_the_consumer_as_false() -> None:
    """★ U1 と U2 をつないだ状態: recovery payload は消費側で False になる。

    U1 だけ、U2 だけでは成立しない経路をここで固定する。
    """
    payload = build_finalize_only_payload(_Record())

    assert _read_as_handler_does(payload) is False


def test_normal_worker_path_is_unchanged() -> None:
    """★ 通常の親→子経路は **1 件も変わらない**(既定値を参照しない)。

    親は detect_and_apply() の結果を必ず payload へ載せるため、
    子はキーの実値を読む。既定を False にしても True は True のまま届く。
    """
    assert _read_as_handler_does({_KEY: True}) is True
    assert _read_as_handler_does({_KEY: False}) is False


def test_parent_still_emits_the_actual_detection_outcome() -> None:
    """親が実値を載せる実装が残っていること(既定値に依存しない前提の固定)。

    ここが消えると「通常経路は変わらない」という本 Issue の前提が崩れ、
    既定 False によって通常の通知まで抑止されてしまう。
    """
    for module in ("buy_candidates_handler", "holdings_watchlist_handler"):
        source = Path(
            inspect.getfile(importlib.import_module(f"jstock_advisor.lambda_handlers.{module}"))
        ).read_text(encoding="utf-8")
        assert f'"{_KEY}": detection_outcome.confirmed' in source, (
            f"{module}: 親が子 payload へ検知結果の実値を載せる実装が見当たらない"
        )


# --- T-3  fail-close が実際に通知を止めること -------------------------------


def test_service_suppresses_normal_notification_when_not_confirmed() -> None:
    """★ False を受けた通知サービスが通常通知を抑止すること(§5-1 の fail-close)。

    本 Issue が直すのは「False が届くこと」であり、抑止そのものは既存実装である。
    その既存実装が生きていることをここで確かめる(片方だけ壊れても気づける)。
    """
    from jstock_advisor.services.line_notification_service import LineNotificationService

    source = inspect.getsource(LineNotificationService.check_trade_cooldown_eligibility)

    assert "if not self._trade_detection_confirmed:" in source
    assert "TRADE_DETECTION_IN_PROGRESS" in source


def test_line_notification_service_default_is_left_unchanged() -> None:
    """★ LNS のコンストラクタ既定 True は **変えていない**(scope 外)。

    生成箇所 10 か所のうち 8 か所がこの既定に依存しており、False へ倒すと
    それらすべてで重大リスク以外の通常通知が抑止される。
    本 Issue は U1 / U2 に限定する、という承認済み設計の範囲を固定する。
    """
    from jstock_advisor.services.line_notification_service import LineNotificationService

    signature = inspect.signature(LineNotificationService.__init__)

    assert signature.parameters[_KEY].default is True


# --- T-4  時刻に依存しないこと ---------------------------------------------


def test_payload_does_not_depend_on_the_current_time() -> None:
    """本変更は時刻・営業日・市場セッションに依存しないこと(§3.5 の確認)。

    同じレコードから 2 回組み立てても、時刻に由来する差が出ない。
    """
    first = build_finalize_only_payload(_Record())
    second = build_finalize_only_payload(_Record())

    assert first == second
    assert not any(isinstance(value, dt.datetime) for value in first.values())


def test_payload_stays_json_primitive() -> None:
    """★ invoke payload の値が JSON 素値のままであること(lock 宣言の裏づけ)。

    本 Issue は SHARED_TOUCHED = なし で宣言している。payload へ新しい型
    (enum・dataclass・日時オブジェクト等)を持ち込むと、それは共通の契約変更に
    あたり lock 範囲が変わる。足したのは **bool 1 つだけ**であることを固定する。
    """
    payload = build_finalize_only_payload(_Record())

    for key, value in payload.items():
        assert isinstance(value, str | bool), f"{key}: JSON 素値でない型 {type(value)!r}"

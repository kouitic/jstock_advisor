"""Issue #63 A-U1a: 永続レコードのデコード失敗ポリシー機構のテスト。

不正レコードは fixture としてのみ作る。**Production への注入は行わない。**
値はすべて架空値であり、実在人物の情報・実際の保有データを含めない。

本 module は PR-1 の時点で既存の呼び出し元を持たない(追加のみ)。
既存 store への差し替えは PR-2(A-U1b)で行う。
"""

from __future__ import annotations

import logging

import pytest

from jstock_advisor.infrastructure.record_failure_policy import (
    DecodeOutcome,
    RecordFailure,
    RecordFailureCollector,
    RecordFailurePolicy,
    decode_records,
    emit_record_failure,
    iter_decoded_records,
)


class _FixtureDecodeError(ValueError):
    """テスト用のデコード失敗。実際の ValidationError の代役。

    message にフィールド値が入りうる状況を再現するため、
    あえて「値らしき文字列」を含めている(出力へ漏れないことの確認用)。
    """


# 失敗記録・ログへ **現れてはいけない** 架空のフィールド値。
# 実際の ValidationError の message はフィールド値を含みうるため、その代役を置く。
# 識別子に secret / key / token 等を含めないこと(gitleaks の generic-api-key が
# 「秘密情報の混入」として検出してしまうため。実際にそれで CI が落ちた)。
# 値も低エントロピーな平易な文字列にしておく。
_FIXTURE_FIELD_VALUE = "owner a quantity value"


def _decode(raw: str) -> str:
    """"bad" で始まる raw を失敗させる単純な decoder。"""
    if raw.startswith("bad"):
        raise _FixtureDecodeError(f"decode failed value={_FIXTURE_FIELD_VALUE}")
    return raw.upper()


def _items(*raws: str) -> list[tuple[str, str]]:
    return [(f"id{i}", raw) for i, raw in enumerate(raws, start=1)]


# --- STRICT（既定。現行と同一の挙動） ---------------------------------------


def test_strict_is_the_default_policy() -> None:
    collector = RecordFailureCollector(collection="c")

    assert collector.policy is RecordFailurePolicy.STRICT


def test_strict_reraises_the_original_exception_object() -> None:
    """包み直さないこと。ValidationError を捕捉している呼び出し元を壊さないため。"""
    with pytest.raises(_FixtureDecodeError) as excinfo:
        decode_records(_items("ok", "bad1", "ok"), _decode, collection="c")

    assert type(excinfo.value) is _FixtureDecodeError
    assert _FIXTURE_FIELD_VALUE in str(excinfo.value), "元の例外そのものが伝わること"


def test_strict_stops_at_the_first_failure() -> None:
    """打ち切り方も現行と同じであること（2 件目以降を評価しない）。"""
    decoded: list[str] = []

    def counting_decode(raw: str) -> str:
        decoded.append(raw)
        return _decode(raw)

    with pytest.raises(_FixtureDecodeError):
        decode_records(_items("ok", "bad1", "bad2"), counting_decode, collection="c")

    assert decoded == ["ok", "bad1"], "3 件目は評価されない"


def test_strict_succeeds_when_every_record_is_valid() -> None:
    outcome = decode_records(_items("a", "b"), _decode, collection="c")

    assert outcome.records == ["A", "B"]
    assert outcome.failures == ()
    assert outcome.undecidable is False


# --- LENIENT -----------------------------------------------------------------


def test_lenient_skips_the_bad_record_and_keeps_the_rest() -> None:
    outcome = decode_records(
        _items("a", "bad1", "b"),
        _decode,
        collection="audit_log",
        policy=RecordFailurePolicy.LENIENT,
    )

    assert outcome.records == ["A", "B"], "1 件の不正で全滅しない"
    assert outcome.failure_count == 1
    assert outcome.undecidable is False, "LENIENT は判定不能にしない"


def test_lenient_records_where_the_failure_was() -> None:
    outcome = decode_records(
        _items("bad1"), _decode, collection="audit_log", policy=RecordFailurePolicy.LENIENT
    )

    assert outcome.failures == (
        RecordFailure(collection="audit_log", item_id="id1", error_type="_FixtureDecodeError"),
    )


def test_lenient_accumulates_every_failure() -> None:
    outcome = decode_records(
        _items("bad1", "a", "bad2", "b"),
        _decode,
        collection="c",
        policy=RecordFailurePolicy.LENIENT,
    )

    assert outcome.records == ["A", "B"]
    assert [f.item_id for f in outcome.failures] == ["id1", "id3"]


# --- FAIL_SAFE_SUPPRESS ------------------------------------------------------


def test_suppress_sets_undecidable_when_a_record_fails() -> None:
    outcome = decode_records(
        _items("a", "bad1"),
        _decode,
        collection="notification_log",
        policy=RecordFailurePolicy.FAIL_SAFE_SUPPRESS,
    )

    assert outcome.records == ["A"]
    assert outcome.undecidable is True, "この結果を再送判定の根拠にしてはいけない"


def test_suppress_is_not_undecidable_when_everything_decodes() -> None:
    outcome = decode_records(
        _items("a", "b"),
        _decode,
        collection="notification_log",
        policy=RecordFailurePolicy.FAIL_SAFE_SUPPRESS,
    )

    assert outcome.undecidable is False


def test_suppress_does_not_decide_suppression_itself() -> None:
    """抑止するかどうかは呼び出し側が決める。本 module は事実だけを返す。"""
    outcome = decode_records(
        _items("bad1"),
        _decode,
        collection="notification_log",
        policy=RecordFailurePolicy.FAIL_SAFE_SUPPRESS,
    )

    assert isinstance(outcome, DecodeOutcome)
    assert not hasattr(outcome, "should_send")


# --- 出力に record の中身を含めないこと（本 Issue の中核要件） -----------------


def test_failure_does_not_carry_record_content() -> None:
    outcome = decode_records(
        _items("bad1"), _decode, collection="c", policy=RecordFailurePolicy.LENIENT
    )

    rendered = repr(outcome.failures)
    assert _FIXTURE_FIELD_VALUE not in rendered
    assert "decode failed" not in rendered, "例外 message を持たない（値を含みうるため）"
    assert "id1" in rendered, "所在は残す（是正できるようにするため）"


def test_emitted_log_does_not_contain_record_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        decode_records(
            _items("bad1"), _decode, collection="c", policy=RecordFailurePolicy.LENIENT
        )

    text = caplog.text
    assert _FIXTURE_FIELD_VALUE not in text
    assert "collection=c" in text
    assert "item_id=id1" in text
    assert "error=_FixtureDecodeError" in text


def test_emit_helper_outputs_only_metadata(caplog: pytest.LogCaptureFixture) -> None:
    failure = RecordFailure(collection="c", item_id="id9", error_type="ValidationError")

    with caplog.at_level(logging.WARNING):
        emit_record_failure(failure, RecordFailurePolicy.LENIENT)

    assert "item_id=id9" in caplog.text
    assert "error=ValidationError" in caplog.text


def test_summary_is_emitted_only_when_something_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        decode_records(_items("a", "b"), _decode, collection="quiet")

    assert "decode summary" not in caplog.text, "平常時は静かであること"


def test_summary_reports_counts_when_something_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        decode_records(
            _items("a", "bad1", "bad2"),
            _decode,
            collection="c",
            policy=RecordFailurePolicy.LENIENT,
        )

    assert "scanned=3 failed=2" in caplog.text


# --- 空入力・全件失敗 --------------------------------------------------------


def test_empty_input_is_safe_under_every_policy() -> None:
    for policy in RecordFailurePolicy:
        outcome = decode_records([], _decode, collection="c", policy=policy)

        assert outcome.records == []
        assert outcome.failures == ()
        assert outcome.undecidable is False


def test_all_records_failing_under_lenient_returns_empty_not_exception() -> None:
    outcome = decode_records(
        _items("bad1", "bad2"),
        _decode,
        collection="c",
        policy=RecordFailurePolicy.LENIENT,
    )

    assert outcome.records == []
    assert outcome.failure_count == 2


def test_all_records_failing_under_strict_raises() -> None:
    with pytest.raises(_FixtureDecodeError):
        decode_records(_items("bad1", "bad2"), _decode, collection="c")


# --- ストリーミング経路（iter_all のピークメモリ有界性を壊さないこと） ---------


def test_iter_decoded_records_is_lazy() -> None:
    """Issue #113 の iter_all() から使えるよう、全件を先に読まないこと。"""
    consumed: list[str] = []

    def tracking_decode(raw: str) -> str:
        consumed.append(raw)
        return _decode(raw)

    collector = RecordFailureCollector(
        collection="c", policy=RecordFailurePolicy.LENIENT
    )
    iterator = iter_decoded_records(_items("a", "b", "c"), tracking_decode, collector)

    assert consumed == [], "生成しただけでは 1 件も読まない"
    assert next(iterator) == "A"
    assert consumed == ["a"], "1 件目だけを読んでいる"


def test_iter_decoded_records_skips_under_lenient() -> None:
    collector = RecordFailureCollector(
        collection="c", policy=RecordFailurePolicy.LENIENT
    )

    result = list(iter_decoded_records(_items("a", "bad1", "b"), _decode, collector))

    assert result == ["A", "B"]
    assert [f.item_id for f in collector.failures] == ["id2"]


def test_iter_decoded_records_raises_under_strict() -> None:
    collector = RecordFailureCollector(collection="c")

    with pytest.raises(_FixtureDecodeError):
        list(iter_decoded_records(_items("a", "bad1"), _decode, collector))


def test_collector_undecidable_only_under_suppress() -> None:
    lenient = RecordFailureCollector(collection="c", policy=RecordFailurePolicy.LENIENT)
    suppress = RecordFailureCollector(
        collection="c", policy=RecordFailurePolicy.FAIL_SAFE_SUPPRESS
    )

    for collector in (lenient, suppress):
        list(iter_decoded_records(_items("bad1"), _decode, collector))

    assert lenient.undecidable is False
    assert suppress.undecidable is True

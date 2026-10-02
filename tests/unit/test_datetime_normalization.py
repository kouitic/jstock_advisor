"""domain/datetime_normalization.py(S-04共通部品。Issue #576)のテスト。"""

import datetime as dt

from jstock_advisor.domain.datetime_normalization import normalize_to_aware_utc

_JST = dt.timezone(dt.timedelta(hours=9))


def test_naive_value_is_treated_as_utc() -> None:
    """naive値はローカルタイムゾーンとして暗黙解釈せず、UTCとみなす。"""
    naive = dt.datetime(2026, 8, 1, 7, 0)  # noqa: DTZ001 - naive扱いの検証のため意図的
    result = normalize_to_aware_utc(naive)

    assert result == dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
    assert result.tzinfo is dt.UTC


def test_aware_utc_value_is_unchanged() -> None:
    aware = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
    assert normalize_to_aware_utc(aware) == aware
    assert normalize_to_aware_utc(aware).tzinfo is dt.UTC


def test_aware_non_utc_value_is_converted_to_the_same_instant_in_utc() -> None:
    """他タイムゾーンで表現されたaware値も、同一の瞬間をUTCへ変換する。"""
    jst_value = dt.datetime(2026, 8, 1, 16, 0, tzinfo=_JST)  # UTC 07:00と同一瞬間
    result = normalize_to_aware_utc(jst_value)

    assert result == dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
    assert result.tzinfo == dt.UTC


def test_naive_and_aware_representing_the_same_instant_compare_equal_after_normalization() -> None:
    """naive値と、同じ瞬間を表すaware値を正規化すると、比較可能かつ等価になる
    (#66 F-L6の根本原因: 正規化無しではTypeErrorになる組み合わせ)。
    """
    naive = dt.datetime(2026, 8, 1, 7, 0)  # noqa: DTZ001 - naive扱いの検証のため意図的
    aware_utc = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)

    assert normalize_to_aware_utc(naive) == normalize_to_aware_utc(aware_utc)


def test_sorting_mixed_naive_and_aware_values_does_not_raise() -> None:
    """正規化を経由したsort keyであれば、naive/aware混在のリストでもTypeError
    にならない(リストのsort/maxがこの関数をkeyとして使う契約の根拠)。
    """
    values = [
        dt.datetime(2026, 8, 1, 9, 0),  # noqa: DTZ001 - naive扱いの検証のため意図的
        dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 1, 17, 0, tzinfo=_JST),  # UTC 08:00と同一瞬間
    ]

    result = sorted(values, key=normalize_to_aware_utc)

    assert [normalize_to_aware_utc(v) for v in result] == [
        dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 1, 8, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.UTC),
    ]

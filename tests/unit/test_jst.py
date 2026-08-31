import datetime as dt

import pytest

from jstock_advisor.domain.jst import (
    evaluation_date_jst,
    format_jst,
    require_timezone_aware,
    to_jst,
)


def test_to_jst_converts_utc_to_jst_plus_9_hours() -> None:
    utc_value = dt.datetime(2026, 7, 24, 6, 40, tzinfo=dt.UTC)
    jst_value = to_jst(utc_value)
    assert jst_value.hour == 15
    assert jst_value.day == 24
    assert jst_value.utcoffset() == dt.timedelta(hours=9)


def test_to_jst_can_shift_date_across_midnight() -> None:
    utc_value = dt.datetime(2026, 7, 24, 20, 0, tzinfo=dt.UTC)
    jst_value = to_jst(utc_value)
    assert jst_value.day == 25
    assert jst_value.hour == 5


def test_format_jst_includes_jst_suffix() -> None:
    utc_value = dt.datetime(2026, 7, 24, 6, 40, tzinfo=dt.UTC)
    assert format_jst(utc_value) == "2026-07-24 15:40 JST"


# ===== evaluation_date_jst / require_timezone_aware(決算日修正デプロイ前対応) =====


def test_evaluation_date_jst_crosses_day_boundary_from_utc() -> None:
    """JST 2026-08-06 08:00 = UTC 2026-08-05 23:00。素の.date()を使うと前日
    (8/5)になってしまうが、evaluation_date_jst()は正しく8/6を返す。
    """
    now_utc = dt.datetime(2026, 8, 5, 23, 0, tzinfo=dt.UTC)
    assert now_utc.date() == dt.date(2026, 8, 5)  # 素の.date()は誤り(比較用)
    assert evaluation_date_jst(now_utc) == dt.date(2026, 8, 6)


def test_evaluation_date_jst_matches_utc_date_away_from_boundary() -> None:
    now_utc = dt.datetime(2026, 8, 6, 3, 0, tzinfo=dt.UTC)  # JST 12:00、日跨ぎなし
    assert evaluation_date_jst(now_utc) == dt.date(2026, 8, 6)


def test_require_timezone_aware_accepts_aware_datetime() -> None:
    require_timezone_aware(dt.datetime(2026, 8, 6, tzinfo=dt.UTC))  # 例外を送出しない


def test_require_timezone_aware_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware(dt.datetime(2026, 8, 6))


# =============================================================================
# Issue #85 Phase B2 / Group 3: JST 暦日 helper contract(BP-06)
#
# 過去バグ(#53 / #23)の共通根は、UTC の `now.date()` を業務日として使い
# JST 00:00-08:59 の間だけ前日扱いになること。`domain/jst.py` は
# 「暦日比較は必ず evaluation_date_jst を経由する」という規約の正本である。
#
# ここで固定するのは **helper 自身の暦日境界契約** のみ。
# 市場営業日 calendar は持ち込まない(暦日と営業日は別 domain のため)。
#
# 以下は本 Group の対象外で、Issue #66 が扱う consumer 側の欠陥である
# (現状 main では未修正のため、ここで検出テストを入れると失敗する):
#   - 第1土曜の業務判定(monthly/quarterly review handler、#66 F-B6)
#   - 営業日評価の start_date が UTC 暦日(#66 F-L3)
#   - 長期保有優待の接近判定(#66 F-L4)
#   - CLI / CSV の date.today() 既定値(#66 F-L5)
# =============================================================================

_JST = dt.timezone(dt.timedelta(hours=9))


@pytest.mark.parametrize(
    ("utc_moment", "expected_jst_date"),
    [
        # UTC 14:59:59 = JST 23:59:59(同日)
        (dt.datetime(2026, 8, 31, 14, 59, 59, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # UTC 15:00:00 = JST 翌日 00:00:00(暦日が進む)
        (dt.datetime(2026, 8, 31, 15, 0, 0, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
        # JST 00:00-08:59 帯: UTC ではまだ前日だが JST では当日
        (dt.datetime(2026, 8, 31, 15, 0, 1, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
        (dt.datetime(2026, 8, 31, 23, 59, 59, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
        # UTC 00:00 = JST 09:00(同日)
        (dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.UTC), dt.date(2026, 9, 1)),
    ],
)
def test_evaluation_date_jst_day_boundary(
    utc_moment: dt.datetime, expected_jst_date: dt.date
) -> None:
    """JST 暦日の境界(23:59:59 → 00:00:00)を跨いで正しい業務日を返すこと。"""
    assert evaluation_date_jst(utc_moment) == expected_jst_date


@pytest.mark.parametrize(
    ("utc_moment", "expected_jst_date", "label"),
    [
        # 月末 → 月初
        (dt.datetime(2026, 8, 31, 14, 59, 59, tzinfo=dt.UTC), dt.date(2026, 8, 31), "月末最終瞬間"),
        (dt.datetime(2026, 8, 31, 15, 0, 0, tzinfo=dt.UTC), dt.date(2026, 9, 1), "月初へ繰上り"),
        # 年末 → 年始
        (dt.datetime(2026, 12, 31, 14, 59, 59, tzinfo=dt.UTC), dt.date(2026, 12, 31), "年末"),
        (dt.datetime(2026, 12, 31, 15, 0, 0, tzinfo=dt.UTC), dt.date(2027, 1, 1), "年始へ繰上り"),
        # うるう年 2/28 → 2/29
        (dt.datetime(2028, 2, 28, 15, 0, 0, tzinfo=dt.UTC), dt.date(2028, 2, 29), "うるう日"),
    ],
)
def test_evaluation_date_jst_month_and_year_rollover(
    utc_moment: dt.datetime, expected_jst_date: dt.date, label: str
) -> None:
    """月末・年末・うるう日の繰り上がりが JST 基準で行われること。"""
    assert evaluation_date_jst(utc_moment) == expected_jst_date, label


@pytest.mark.parametrize(
    ("utc_moment", "expected_weekday", "label"),
    [
        # 2026-09-04 は金曜。UTC 15:00 で JST 土曜へ繰り上がる
        (dt.datetime(2026, 9, 4, 14, 59, 59, tzinfo=dt.UTC), 4, "金曜のまま"),
        (dt.datetime(2026, 9, 4, 15, 0, 0, tzinfo=dt.UTC), 5, "土曜へ繰上り"),
        # 2026-09-06 は日曜 → 月曜
        (dt.datetime(2026, 9, 6, 14, 59, 59, tzinfo=dt.UTC), 6, "日曜のまま"),
        (dt.datetime(2026, 9, 6, 15, 0, 0, tzinfo=dt.UTC), 0, "月曜へ繰上り"),
    ],
)
def test_evaluation_date_jst_weekday_boundary(
    utc_moment: dt.datetime, expected_weekday: int, label: str
) -> None:
    """曜日境界(金→土、日→月)も JST 暦日で決まること。

    **営業日かどうかの判定はここでは行わない**(市場営業日 calendar は別 domain)。
    """
    assert evaluation_date_jst(utc_moment).weekday() == expected_weekday, label


def test_evaluation_date_jst_differs_from_naive_utc_date_in_the_risk_window() -> None:
    """JST 00:00-08:59 帯では **UTC 暦日と JST 暦日が食い違う**ことを明示する。

    過去バグ(#23 / #53)はこの帯で `now.date()` を業務日として使ったことが原因。
    この差が存在する限り `evaluation_date_jst` を経由する必要がある、という
    規約の根拠そのものを固定する。
    """
    utc_moment = dt.datetime(2026, 8, 31, 20, 0, 0, tzinfo=dt.UTC)  # JST 09-01 05:00

    assert utc_moment.date() == dt.date(2026, 8, 31)
    assert evaluation_date_jst(utc_moment) == dt.date(2026, 9, 1)
    assert evaluation_date_jst(utc_moment) != utc_moment.date()


@pytest.mark.parametrize(
    "tz",
    [dt.UTC, _JST, dt.timezone(dt.timedelta(hours=-5))],
    ids=["utc", "jst", "utc-minus-5"],
)
def test_evaluation_date_jst_is_tzinfo_independent(tz: dt.tzinfo) -> None:
    """同一時刻であれば、入力の tzinfo 表現に関わらず同じ JST 暦日を返すこと。"""
    instant = dt.datetime(2026, 8, 31, 15, 0, 0, tzinfo=dt.UTC)

    assert evaluation_date_jst(instant.astimezone(tz)) == dt.date(2026, 9, 1)


def test_evaluation_date_jst_rejects_naive_datetime_via_require_timezone_aware() -> None:
    """naive datetime は暗黙に UTC 扱いせず、ガードで弾けること。

    `evaluation_date_jst` 自体は naive を受けると astimezone がローカル時刻を
    仮定してしまうため、呼び出し側は `require_timezone_aware` を使う契約である。
    その契約が機能することを固定する。
    """
    naive = dt.datetime(2026, 8, 31, 15, 0, 0)  # noqa: DTZ001 - naive の拒否を検証するため意図的

    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware(naive)


@pytest.mark.parametrize(
    "aware",
    [
        dt.datetime(2026, 8, 31, 15, 0, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 8, 31, 15, 0, 0, tzinfo=_JST),
    ],
    ids=["utc", "jst"],
)
def test_require_timezone_aware_accepts_any_aware_datetime(aware: dt.datetime) -> None:
    """aware であれば tz の種類を問わず受理すること(JST 固定を強制しない)。"""
    require_timezone_aware(aware)


def test_jst_offset_is_fixed_at_plus_nine_hours() -> None:
    """JST は固定 +9h(日本にDSTが無いためオフセットは季節で変わらない)。"""
    summer = dt.datetime(2026, 8, 1, 0, 0, tzinfo=dt.UTC)
    winter = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC)

    assert to_jst(summer).utcoffset() == dt.timedelta(hours=9)
    assert to_jst(winter).utcoffset() == dt.timedelta(hours=9)

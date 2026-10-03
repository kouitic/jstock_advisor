"""Issue #474(USER決定 2026-10-03、OPTION 2): 実際に送信されるLINE短文へ、財務データが
最新の決算を反映していない可能性(財務鮮度 STALE)を「決算未反映」で確実に出す。

## 検査の対象

**実際に push された本文**を検査する。`LineNotificationService.send_recommendation_notification()`
(→ `_render_notification_body()` → `build_notification_text_input()` →
`format_notification_text()`)の結果を `_FakeLineClient` で受け取り、固定の期待文字列(literal)と
比較する。`render_notification_preview()`(診断用の長文)の assert は使わない。

期待文字列は、実 adapter が作る `NotificationTextInput` に本変更を適用して得た文字列を
そのまま固定値として持つ(実装の式から期待値を作らない)。fixture は銘柄「三菱UFJ」・コード 8306・
現在値 1,850 円・owner「本人」。STALE は `key_risks=[FINANCIAL_STALE_USER_WARNING]`。

## 契約(USER 決定の優先順位)

```
MANDATORY  判定種別・銘柄・現在値・売却数量/比率・必要な価格情報(目標価格または算定不可)
HIGH       決算未反映(STALE のときのみ)
OPTIONAL   継続日数・理由文・銘柄分類等
```

- STALE でない本文は**現行と1文字も変えない**(下の non-STALE の期待文字列がそのまま現行の本文)。
- 文字数が足りないとき落ちるのは OPTIONAL だけ(丸ごと drop。文章の途中切りは作らない)。
- MANDATORY と警告だけで 70 文字を超える場合は、既存の必須セグメントと同じく超過を許容する
  (70 の値は変更しない。MANAGER 判断 D-1)。

## 注意(PARTIAL_SELL の文字数)

先行 HANDOFF の PARTIAL_SELL の文字数(62/65/55)は測定入力の誤りで、実際の PARTIAL_SELL は
reason・stock_types を持たず 47〜59 字(STALE は +6 字)。競合は銘柄名+owner が長い場合にのみ起きる
(#474 issuecomment-5963587682)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.financial_stale_warning import (
    FINANCIAL_STALE_SHORT_LABEL,
    FINANCIAL_STALE_WARNING_TEXT,
    has_financial_stale_warning,
)
from jstock_advisor.domain.notification.recommendation_adapter import (
    build_attention_text_input,
    build_notification_text_input,
    build_watch_end_text_input,
)
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.financial_freshness_integration import FINANCIAL_STALE_USER_WARNING
from jstock_advisor.services.line_notification_service import LineNotificationService

_CONFIG = load_config()
_NOW = dt.datetime(2026, 10, 3, 8, 0, tzinfo=dt.UTC)
_LABEL = "決算未反映"


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _price(value: str) -> PriceWithRationale:
    return PriceWithRationale(price=Decimal(value), rationale="x")


def _recommendation(
    recommendation_type: RecommendationType,
    *,
    stock_name: str = "三菱UFJ",
    sell_prices: SellPriceLevels | None = None,
    reasons: list[str] | None = None,
    shares: int | None = None,
    ratio: float | None = None,
    key_risks: list[str] | None = None,
    valuation_caveats: list[str] | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id=f"rec-{uuid.uuid4()}",
        stock_code="8306",
        stock_name=stock_name,
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        sell_prices=sell_prices if sell_prices is not None else SellPriceLevels(),
        price_at_recommendation=Decimal("1850"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        suggested_sell_shares=shares,
        suggested_sell_ratio=ratio,
        reasons=reasons or [],
        key_risks=key_risks or [],
        valuation_caveats=valuation_caveats or [],
        holding_id="本人#8306",
        owner="本人",
    )


def _send(tmp_path: Path, recommendation: Recommendation) -> str:
    """実際の送信経路(send_recommendation_notification)で push された本文を返す。
    送信ごとに保存先を分けるため、同一銘柄の連続送信が dedup/claim で抑止されない。"""
    store_dir = tmp_path / f"store-{uuid.uuid4()}"
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        notification_claim_repository=NotificationClaimRepository(store_dir=store_dir),
    )
    recommendation_repo.save(recommendation)
    service.send_recommendation_notification(recommendation, _NOW)
    assert len(client.sent) == 1
    return client.sent[0]


def _text_with(tmp_path: Path, rec_factory, stale: bool) -> str:  # type: ignore[no-untyped-def]
    key_risks = [FINANCIAL_STALE_USER_WARNING] if stale else []
    return _send(tmp_path, rec_factory(key_risks))


# --- 定数の一致(D-3 案D: domain側の正本と services 側の定数が drift しない) -------------


def test_domain_warning_text_matches_the_services_constant() -> None:
    assert FINANCIAL_STALE_WARNING_TEXT == FINANCIAL_STALE_USER_WARNING


def test_short_label_is_the_decided_wording() -> None:
    assert FINANCIAL_STALE_SHORT_LABEL == _LABEL


# --- T1 / T2 / T3 / T6: 実送信本文の固定(非STALEは現行と同一、STALEは警告のみ追加) ----------
#
# (id, factory(key_risks) -> Recommendation, 非STALEの本文, STALEの本文)

_SELL_PRICE = SellPriceLevels(stop_review_price=_price("1700"))
_FULL_PRICE = SellPriceLevels(full_profit_consideration_price=_price("2000"))
_PARTIAL_PRICE = SellPriceLevels(recommended_limit_price=_price("2000"))


def _case(rtype, **kw):  # type: ignore[no-untyped-def]
    return lambda key_risks: _recommendation(rtype, key_risks=key_risks, **kw)


_CASES = [
    pytest.param(
        _case(
            RecommendationType.SELL_CONSIDERATION,
            sell_prices=_SELL_PRICE,
            reasons=["財務健全性の悪化を検知"],
        ),
        "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円｜財務健全性の悪化を検知",
        "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円｜決算未反映｜財務健全性の悪化を検知",
        id="T1-sell-consideration",
    ),
    pytest.param(
        _case(
            RecommendationType.FULL_PROFIT_TAKE,
            sell_prices=_FULL_PRICE,
            reasons=["含み益率が全部利確基準を超過"],
        ),
        "全部売却検討 8306 三菱UFJ（本人）\n"
        "1,850円｜全部売却目安2,000円｜含み益率が全部利確基準を超過",
        "全部売却検討 8306 三菱UFJ（本人）\n"
        "1,850円｜全部売却目安2,000円｜決算未反映｜含み益率が全部利確基準を超過",
        id="T1-full-profit-take",
    ),
    pytest.param(
        _case(
            RecommendationType.STRONG_SELL_CONSIDERATION,
            sell_prices=_FULL_PRICE,
            reasons=["複数の悪化要因"],
        ),
        "全部売却検討 8306 三菱UFJ（本人）\n1,850円｜全部売却目安2,000円｜複数の悪化要因",
        "全部売却検討 8306 三菱UFJ（本人）\n"
        "1,850円｜全部売却目安2,000円｜決算未反映｜複数の悪化要因",
        id="T1-strong-sell-consideration",
    ),
    pytest.param(
        _case(RecommendationType.SELL, sell_prices=_SELL_PRICE, reasons=["下落の継続"]),
        "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円｜下落の継続",
        "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円｜決算未反映｜下落の継続",
        id="T1-sell",
    ),
    pytest.param(
        _case(
            RecommendationType.PARTIAL_PROFIT_TAKE,
            sell_prices=_PARTIAL_PRICE,
            shares=300,
            ratio=0.6,
        ),
        "一部売却 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安2,000円",
        "一部売却 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安2,000円｜決算未反映",
        id="T3-partial-profit-take",
    ),
    pytest.param(
        _case(
            RecommendationType.PARTIAL_RISK_REDUCTION,
            sell_prices=_PARTIAL_PRICE,
            shares=300,
            ratio=0.6,
        ),
        "一部縮小 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安2,000円",
        "一部縮小 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安2,000円｜決算未反映",
        id="T3-partial-risk-reduction",
    ),
    pytest.param(
        _case(
            RecommendationType.URGENT_REVIEW,
            reasons=["継続企業の疑義の開示を確認", "重大な減配の公表"],
        ),
        "緊急確認 8306 三菱UFJ（本人）\n1,850円｜継続企業の疑義の開示を確認 / 重大な減配の公表",
        "緊急確認 8306 三菱UFJ（本人）\n"
        "1,850円｜決算未反映｜継続企業の疑義の開示を確認 / 重大な減配の公表",
        id="T6-urgent-review-critical-risk",
    ),
    pytest.param(
        _case(RecommendationType.URGENT_HOLDING_REVIEW, reasons=["重大条件のため上限補正"]),
        "緊急確認 8306 三菱UFJ（本人）\n1,850円｜重大条件のため上限補正",
        "緊急確認 8306 三菱UFJ（本人）\n1,850円｜決算未反映｜重大条件のため上限補正",
        id="T6-urgent-holding-review-critical-risk",
    ),
    pytest.param(
        _case(RecommendationType.REVIEW),
        "要確認 8306 三菱UFJ（本人）\n1,850円｜売買判断を保留",
        "要確認 8306 三菱UFJ（本人）\n1,850円｜決算未反映｜売買判断を保留",
        id="T6-review-manual-review",
    ),
    pytest.param(
        _case(
            RecommendationType.WATCH,
            sell_prices=SellPriceLevels(partial_profit_start_price=_price("1900")),
        ),
        "監視 8306 三菱UFJ（本人）\n1,850円｜利確検討1,900円",
        "監視 8306 三菱UFJ（本人）\n1,850円｜利確検討1,900円｜決算未反映",
        id="T6-watch",
    ),
    pytest.param(
        _case(RecommendationType.WATCH_BEFORE_EARNINGS),
        "監視 8306 三菱UFJ（本人）\n1,850円｜決算発表接近のため様子見",
        "監視 8306 三菱UFJ（本人）\n1,850円｜決算未反映｜決算発表接近のため様子見",
        id="T6-watch-before-earnings-profit-taking-side",
    ),
]


@pytest.mark.parametrize(("factory", "non_stale", "_stale_text"), _CASES)
def test_non_stale_text_is_unchanged_and_has_no_label(
    tmp_path: Path, factory, non_stale: str, _stale_text: str
) -> None:  # type: ignore[no-untyped-def]
    text = _text_with(tmp_path, factory, stale=False)
    assert text == non_stale
    assert _LABEL not in text


@pytest.mark.parametrize(("factory", "_non_stale_text", "stale_text"), _CASES)
def test_stale_text_carries_the_label_in_the_actually_sent_text(
    tmp_path: Path, factory, _non_stale_text: str, stale_text: str
) -> None:  # type: ignore[no-untyped-def]
    text = _text_with(tmp_path, factory, stale=True)
    assert text == stale_text
    assert text.count(_LABEL) == 1


# --- T4 / T5: 70文字との競合・境界 --------------------------------------------------------


def _sell_with_reason(reason_chars: int):  # type: ignore[no-untyped-def]
    return _case(
        RecommendationType.SELL_CONSIDERATION,
        sell_prices=_SELL_PRICE,
        reasons=["あ" * reason_chars],
    )


_SELL_PREFIX = "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円"


@pytest.mark.parametrize(
    ("reason_chars", "stale", "expected_len", "reason_kept"),
    [
        # 理由27字: STALEでも70字ちょうどで収まる(理由は残る)
        pytest.param(27, False, 64, True, id="reason27-non-stale"),
        pytest.param(27, True, 70, True, id="reason27-stale-exactly-70"),
        # 理由28字: 非STALEは65字で理由が残る / STALEは71字になるため理由だけを丸ごと drop
        pytest.param(28, False, 65, True, id="reason28-non-stale"),
        pytest.param(28, True, 42, False, id="reason28-stale-reason-dropped"),
        # 理由33字: 非STALEは70字ちょうどで残る / STALEは理由を drop
        pytest.param(33, False, 70, True, id="reason33-non-stale-exactly-70"),
        pytest.param(33, True, 42, False, id="reason33-stale-reason-dropped"),
        # 理由34字: 非STALEでも理由は落ちる(現行の仕様)/ STALEも同じ
        pytest.param(34, False, 36, False, id="reason34-non-stale-reason-dropped"),
        pytest.param(34, True, 42, False, id="reason34-stale-reason-dropped"),
    ],
)
def test_sell_conflict_drops_only_the_optional_reason_and_keeps_label_and_prices(
    tmp_path: Path, reason_chars: int, stale: bool, expected_len: int, reason_kept: bool
) -> None:
    reason = "あ" * reason_chars
    text = _text_with(tmp_path, _sell_with_reason(reason_chars), stale=stale)
    assert len(text) == expected_len
    assert (reason in text) is reason_kept
    # MANDATORY(判定種別・銘柄・現在値・必要な価格)は常に残る
    assert text.startswith(_SELL_PREFIX)
    # 警告はSTALEのときだけ、競合の有無にかかわらず残る(落とすのはreasonだけ)
    assert (_LABEL in text) is stale
    if stale:
        assert text.startswith(_SELL_PREFIX + "｜" + _LABEL)


@pytest.mark.parametrize(
    ("name_chars", "stale", "expected_len"),
    [
        pytest.param(22, False, 64, id="name22-non-stale"),
        pytest.param(22, True, 70, id="name22-stale-exactly-70"),
        pytest.param(23, False, 65, id="name23-non-stale"),
        # 銘柄名23字: MANDATORY+警告だけで71字(落とせる任意セグメントが無いため70超過を許容。D-1)
        pytest.param(23, True, 71, id="name23-stale-over-70-allowed"),
    ],
)
def test_partial_sell_conflict_keeps_every_mandatory_segment_and_the_label(
    tmp_path: Path, name_chars: int, stale: bool, expected_len: int
) -> None:
    name = "あ" * name_chars
    factory = _case(
        RecommendationType.PARTIAL_PROFIT_TAKE,
        stock_name=name,
        sell_prices=_PARTIAL_PRICE,
        shares=300,
        ratio=0.6,
    )
    text = _text_with(tmp_path, factory, stale=stale)
    assert len(text) == expected_len
    # 数量・比率・現在値・必要な価格(売却目安)は STALE でも1つも欠けない
    for mandatory in (name, "1,850円", "300株(60%)", "売却目安2,000円"):
        assert mandatory in text
    assert (_LABEL in text) is stale
    if stale:
        assert text.endswith("売却目安2,000円｜" + _LABEL)


def test_partial_sell_without_a_price_keeps_the_withheld_label_and_the_stale_label(
    tmp_path: Path,
) -> None:
    """価格が算定不可の PARTIAL_SELL でも、算定不可ラベル(必須)・数量・警告が残る。"""
    factory = _case(
        RecommendationType.PARTIAL_PROFIT_TAKE, sell_prices=SellPriceLevels(), shares=300, ratio=0.6
    )
    assert _text_with(tmp_path, factory, stale=False) == (
        "一部売却 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安は算定不可"
    )
    assert _text_with(tmp_path, factory, stale=True) == (
        "一部売却 8306 三菱UFJ（本人）\n1,850円｜300株(60%)｜売却目安は算定不可｜決算未反映"
    )


# --- exact match(substring / prefix 禁止。負の証拠) ---------------------------------------

_NEAR_MISSES = [
    pytest.param(FINANCIAL_STALE_WARNING_TEXT[:10], id="prefix"),
    pytest.param(FINANCIAL_STALE_WARNING_TEXT[3:12], id="middle-substring"),
    pytest.param(FINANCIAL_STALE_WARNING_TEXT + "。", id="suffix-appended"),
    pytest.param(" " + FINANCIAL_STALE_WARNING_TEXT, id="leading-space"),
    pytest.param(FINANCIAL_STALE_WARNING_TEXT + " ", id="trailing-space"),
    pytest.param(FINANCIAL_STALE_WARNING_TEXT + "\n", id="trailing-newline"),
    pytest.param(
        "注意: " + FINANCIAL_STALE_WARNING_TEXT + "(補足)", id="embedded-in-longer-sentence"
    ),
    pytest.param("", id="empty-string"),
]


@pytest.mark.parametrize("near_miss", _NEAR_MISSES)
def test_a_near_miss_of_the_warning_does_not_produce_the_label(
    tmp_path: Path, near_miss: str
) -> None:
    rec = _recommendation(
        RecommendationType.SELL_CONSIDERATION,
        sell_prices=_SELL_PRICE,
        reasons=["財務健全性の悪化を検知"],
        key_risks=[near_miss],
    )
    text = _send(tmp_path, rec)
    assert _LABEL not in text
    # 非STALEと完全に同じ本文(部分一致で拾っていない)
    assert text == "売却検討 8306 三菱UFJ（本人）\n1,850円｜見直し1,700円｜財務健全性の悪化を検知"


def test_an_exact_element_among_other_key_risks_produces_the_label(tmp_path: Path) -> None:
    rec = _recommendation(
        RecommendationType.SELL_CONSIDERATION,
        sell_prices=_SELL_PRICE,
        reasons=["財務健全性の悪化を検知"],
        key_risks=["含み損益率-8.0%", FINANCIAL_STALE_USER_WARNING],
    )
    assert _LABEL in _send(tmp_path, rec)


def test_has_financial_stale_warning_is_an_exact_element_match() -> None:
    assert has_financial_stale_warning([FINANCIAL_STALE_WARNING_TEXT]) is True
    assert has_financial_stale_warning(["x", FINANCIAL_STALE_WARNING_TEXT]) is True
    assert has_financial_stale_warning([]) is False
    assert has_financial_stale_warning([FINANCIAL_STALE_WARNING_TEXT + "。"]) is False
    # str が誤って渡されても部分一致にならない(`in` ではなく要素ごとの等値比較)
    assert has_financial_stale_warning(FINANCIAL_STALE_WARNING_TEXT) is False


# --- 二重表示の契約(#701。valuation_caveats は現在 SHORT_TEXT へ接続されていない) --------------


def test_the_label_appears_exactly_once_even_if_valuation_caveats_repeats_the_warning(
    tmp_path: Path,
) -> None:
    rec = _recommendation(
        RecommendationType.SELL_CONSIDERATION,
        sell_prices=_SELL_PRICE,
        reasons=["財務健全性の悪化を検知"],
        key_risks=[FINANCIAL_STALE_USER_WARNING],
        valuation_caveats=[FINANCIAL_STALE_USER_WARNING],
    )
    text = _send(tmp_path, rec)
    assert text.count(_LABEL) == 1
    assert FINANCIAL_STALE_USER_WARNING not in text


def test_valuation_caveats_alone_does_not_produce_the_label(tmp_path: Path) -> None:
    """短文へ出す財務鮮度の源は key_risks の完全一致だけ(valuation_caveats は読まない)。"""
    rec = _recommendation(
        RecommendationType.SELL_CONSIDERATION,
        sell_prices=_SELL_PRICE,
        reasons=["財務健全性の悪化を検知"],
        valuation_caveats=[FINANCIAL_STALE_USER_WARNING],
    )
    assert _LABEL not in _send(tmp_path, rec)


# --- 適用範囲(MANAGER 判断 D-2): 対象外のカテゴリ・builder には出ない -----------------------


def test_buy_and_near_buy_do_not_get_the_label_even_with_the_warning_in_key_risks() -> None:
    rec = _recommendation(RecommendationType.BUY, key_risks=[FINANCIAL_STALE_USER_WARNING])
    for category in (NotificationCategory.BUY, NotificationCategory.NEAR_BUY):
        assert build_notification_text_input(rec, category).financial_stale_label is None


def test_buy_side_watch_before_earnings_does_not_get_the_label() -> None:
    rec = _recommendation(
        RecommendationType.WATCH_BEFORE_EARNINGS, key_risks=[FINANCIAL_STALE_USER_WARNING]
    )
    result = build_notification_text_input(rec, NotificationCategory.WATCH_BEFORE_EARNINGS)
    assert result.financial_stale_label is None


def test_attention_and_watch_end_builders_do_not_get_the_label() -> None:
    rec = _recommendation(RecommendationType.WATCH, key_risks=[FINANCIAL_STALE_USER_WARNING])
    attention = build_attention_text_input(rec, "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE")
    assert attention.financial_stale_label is None
    ended = rec.model_copy(
        update={
            "watch_end_reason": "PRICE_OUT_OF_RANGE",
            "watch_previous_consecutive_business_days": 3,
        }
    )
    assert build_watch_end_text_input(ended).financial_stale_label is None


@pytest.mark.parametrize(
    "category",
    [
        NotificationCategory.SELL,
        NotificationCategory.CRITICAL_RISK,
        NotificationCategory.MANUAL_REVIEW,
        NotificationCategory.PARTIAL_SELL,
        NotificationCategory.WATCH,
    ],
)
def test_target_categories_get_the_short_label_only_when_the_warning_is_present(
    category: NotificationCategory,
) -> None:
    with_warning = _recommendation(
        RecommendationType.SELL_CONSIDERATION, key_risks=[FINANCIAL_STALE_USER_WARNING]
    )
    without = _recommendation(RecommendationType.SELL_CONSIDERATION)
    assert (
        build_notification_text_input(with_warning, category).financial_stale_label
        == FINANCIAL_STALE_SHORT_LABEL
    )
    assert build_notification_text_input(without, category).financial_stale_label is None

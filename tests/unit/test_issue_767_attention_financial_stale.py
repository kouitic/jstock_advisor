"""Issue #767(USER決定 B、2026-10-03): 利益保全注意(ATTENTION)の実送信LINE短文にも、
財務データが最新の決算を反映していない可能性(財務鮮度 STALE)を「決算未反映」で出す。

## 検査の対象

**実際に push された本文**を検査する。`LineNotificationService.send_attention_notification()`
(→ `build_attention_text_input()` → `format_notification_text()`)の結果を `_FakeLineClient` で
受け取り、固定の期待文字列(literal)と比較する。診断用の長文プレビューの assert は使わない。
fixture は銘柄「三菱UFJ」・コード 8306・現在値 1,850 円・所有者「本人」・高値からの下落 12.0%・
含み益の減少 35%。STALE は `key_risks=[FINANCIAL_STALE_USER_WARNING]`。

## 契約(USER 決定の優先順位。#474 と同じ)

```
MANDATORY  判定(利益保全注意)・銘柄・所有者
PRICE      現在値(formatter 上は任意セグメント。既存の性質で本件では変更しない)
HIGH       決算未反映(STALE のときのみ。70 字を超えても落とさない)
OPTIONAL   理由文(高値比・含み益の減少 / 一部売却見送り)。70 字を超えるときは丸ごと落ちる
```

- STALE でない利益保全注意の本文は**現行と1バイトも変えない**。
- 監視終了の通知は今回は変更しない(警告を付けない)。
- 重複抑止の identity(content_hash)は本文を含まないため、警告の有無で変わらない。

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    利益保全注意の実送信本文(CANDIDATE・STRONG の 2 起因 × 理由データの有無 ×
            STALE の有無)。
            銘柄名の字数を変えた 70 字との競合の境界(理由文だけが落ち、警告は残る)。完全一致の判定。
            重複抑止の content_hash が警告の有無で変わらないこと。
            監視終了の builder が警告を付けないこと。
していない  ・利益保全注意以外の通知本文(BUY・NEAR_BUY・#474 の 5 カテゴリ。
              tests/unit/test_issue_474_*.py が固定)
            ・財務鮮度の判定そのもの(STALE になる条件。各判定サービスの責務)
            ・通知するか否かの判定・Recommendation の保存・DecisionSnapshot
            ・Production の通知頻度・STALE と利益保全注意が同じ日に起きる割合(未測定)
            ・極端に長い銘柄名(48 字以上)で現在値が先に落ちる性質の変更
              (formatter の共通契約。変更していない)

時刻は固定。Production・AWS へは触れない。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.financial_stale_warning import (
    FINANCIAL_STALE_SHORT_LABEL,
    FINANCIAL_STALE_WARNING_TEXT,
)
from jstock_advisor.domain.notification.message_formatter import format_notification_text
from jstock_advisor.domain.notification.recommendation_adapter import (
    build_attention_text_input,
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
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    _compute_attention_event_identity,
)

_CONFIG = load_config()
_NOW = dt.datetime(2026, 10, 3, 8, 0, tzinfo=dt.UTC)
_LABEL = "決算未反映"
_CANDIDATE = "CANDIDATE"
_STRONG = "STRONG"
_ORIGIN = {
    _CANDIDATE: "PROFIT_PROTECTION_CANDIDATE",
    _STRONG: "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE",
}
_HEAD = "利益保全注意 8306 三菱UFJ（本人）\n1,850円"


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _attention(
    signal: str,
    *,
    stale: bool,
    stock_name: str = "三菱UFJ",
    with_reason_data: bool = True,
    key_risks: list[str] | None = None,
    valuation_caveats: list[str] | None = None,
) -> Recommendation:
    risks = (
        key_risks if key_risks is not None else ([FINANCIAL_STALE_USER_WARNING] if stale else [])
    )
    update: dict[str, object] = {"profit_protection_signal": signal}
    if with_reason_data:
        update.update(
            profit_protection_peak_gain_pct=50.0,
            profit_protection_drawdown_from_peak_pct=12.0,
            profit_protection_gain_giveback_ratio_pct=35.0,
        )
    return Recommendation(
        recommendation_id=f"rec-{uuid.uuid4()}",
        stock_code="8306",
        stock_name=stock_name,
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        price_at_recommendation=Decimal("1850"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        key_risks=risks,
        valuation_caveats=valuation_caveats or [],
        holding_id="本人#8306",
        owner="本人",
    ).model_copy(update=update)


def _send(tmp_path: Path, recommendation: Recommendation) -> tuple[str, str]:
    """実際の送信経路(send_attention_notification)で push された本文と、保存された content_hash。
    送信ごとに保存先を分けるため、連続送信が dedup / claim で抑止されない。"""
    store_dir = tmp_path / f"store-{uuid.uuid4()}"
    recommendation_repo = RecommendationRepository(store_dir=store_dir)
    log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=log_repo,
        recommendation_repository=recommendation_repo,
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        notification_claim_repository=NotificationClaimRepository(store_dir=store_dir),
    )
    recommendation_repo.save(recommendation)
    service.send_attention_notification(recommendation, _NOW)
    assert len(client.sent) == 1
    [log] = log_repo.list_all()
    return client.sent[0], log.content_hash


def _text(tmp_path: Path, recommendation: Recommendation) -> str:
    return _send(tmp_path, recommendation)[0]


def test_the_decided_wording_and_the_warning_are_the_ones_from_474() -> None:
    assert FINANCIAL_STALE_SHORT_LABEL == _LABEL
    assert FINANCIAL_STALE_WARNING_TEXT == FINANCIAL_STALE_USER_WARNING


# --- T1 / T2: 実送信本文(固定の期待文字列) ---------------------------------------------

_WITH_REASON = "高値比-12.0%・含み益35%減"
_STRONG_SUFFIX = "(一部売却見送り)"
_CASES = [
    # (signal, with_reason_data, 非STALEの本文, STALEの本文)
    pytest.param(
        _CANDIDATE,
        True,
        f"{_HEAD}｜{_WITH_REASON}",
        f"{_HEAD}｜{_LABEL}｜{_WITH_REASON}",
        id="candidate-with-reason",
    ),
    pytest.param(
        _CANDIDATE,
        False,
        _HEAD,
        f"{_HEAD}｜{_LABEL}",
        id="candidate-without-reason",
    ),
    pytest.param(
        _STRONG,
        True,
        f"{_HEAD}｜{_WITH_REASON}{_STRONG_SUFFIX}",
        f"{_HEAD}｜{_LABEL}｜{_WITH_REASON}{_STRONG_SUFFIX}",
        id="strong-with-reason",
    ),
    pytest.param(
        _STRONG,
        False,
        f"{_HEAD}｜利益保全の強いシグナル{_STRONG_SUFFIX}",
        f"{_HEAD}｜{_LABEL}｜利益保全の強いシグナル{_STRONG_SUFFIX}",
        id="strong-without-reason-data",
    ),
]


@pytest.mark.parametrize(("signal", "with_data", "non_stale", "_stale"), _CASES)
def test_non_stale_attention_text_is_unchanged_byte_for_byte(
    tmp_path: Path, signal: str, with_data: bool, non_stale: str, _stale: str
) -> None:
    text = _text(tmp_path, _attention(signal, stale=False, with_reason_data=with_data))
    assert text == non_stale
    assert _LABEL not in text


@pytest.mark.parametrize(("signal", "with_data", "_non_stale", "stale_text"), _CASES)
def test_stale_attention_text_carries_the_label_after_the_price_and_before_the_reason(
    tmp_path: Path, signal: str, with_data: bool, _non_stale: str, stale_text: str
) -> None:
    text = _text(tmp_path, _attention(signal, stale=True, with_reason_data=with_data))
    assert text == stale_text
    assert text.count(_LABEL) == 1
    assert text.index("1,850円") < text.index(_LABEL)


def test_the_example_in_issue_767_is_the_actually_sent_text(tmp_path: Path) -> None:
    """#767 本文の (B) の例と同じ本文(52 字。この例では 70 文字に収まる)。"""
    text = _text(tmp_path, _attention(_CANDIDATE, stale=True))
    assert (
        text == "利益保全注意 8306 三菱UFJ（本人）\n1,850円｜決算未反映｜高値比-12.0%・含み益35%減"
    )
    assert len(text) == 52


# --- T3: 70 文字との競合(理由文だけが落ち、警告・現在値は残る)-----------------------------
# 銘柄名「あ」× n。数値は実送信本文の長さ(len)で、境界は次のとおり:
#   CANDIDATE  非STALE: n ≤ 29 で理由文が残る(n=29 で 70 字)
#              STALE: n ≤ 23 で残る(n=23 で 70 字)/ n=24 から理由文が落ちる
#   STRONG     非STALE: n ≤ 20 で理由文が残る(n=20 で 70 字)
#              STALE: n ≤ 14 で残る(n=14 で 70 字)/ n=15 から理由文が落ちる
# STALE のときだけ理由文が落ちる範囲(CANDIDATE n=24〜29・STRONG n=15〜20)が、
# USER が承認した「警告がある日は、文字数制約により理由文を省略してよい」の範囲。


def _name(n: int) -> str:
    return "あ" * n


@pytest.mark.parametrize(
    ("signal", "n", "stale", "expected_len", "reason_kept"),
    [
        pytest.param(_CANDIDATE, 23, False, 64, True, id="candidate-n23-non-stale"),
        pytest.param(_CANDIDATE, 23, True, 70, True, id="candidate-n23-stale-exactly-70"),
        pytest.param(_CANDIDATE, 24, False, 65, True, id="candidate-n24-non-stale"),
        pytest.param(_CANDIDATE, 24, True, 53, False, id="candidate-n24-stale-reason-dropped"),
        pytest.param(_CANDIDATE, 29, False, 70, True, id="candidate-n29-non-stale-exactly-70"),
        pytest.param(_CANDIDATE, 29, True, 58, False, id="candidate-n29-stale-reason-dropped"),
        pytest.param(_CANDIDATE, 30, False, 53, False, id="candidate-n30-non-stale-reason-dropped"),
        pytest.param(_CANDIDATE, 30, True, 59, False, id="candidate-n30-stale-reason-dropped"),
        pytest.param(_STRONG, 14, False, 64, True, id="strong-n14-non-stale"),
        pytest.param(_STRONG, 14, True, 70, True, id="strong-n14-stale-exactly-70"),
        pytest.param(_STRONG, 15, False, 65, True, id="strong-n15-non-stale"),
        pytest.param(_STRONG, 15, True, 44, False, id="strong-n15-stale-reason-dropped"),
        pytest.param(_STRONG, 20, False, 70, True, id="strong-n20-non-stale-exactly-70"),
        pytest.param(_STRONG, 20, True, 49, False, id="strong-n20-stale-reason-dropped"),
        pytest.param(_STRONG, 21, False, 44, False, id="strong-n21-non-stale-reason-dropped"),
        pytest.param(_STRONG, 21, True, 50, False, id="strong-n21-stale-reason-dropped"),
    ],
)
def test_conflict_drops_only_the_optional_reason_and_keeps_the_label_and_the_price(
    tmp_path: Path, signal: str, n: int, stale: bool, expected_len: int, reason_kept: bool
) -> None:
    text = _text(tmp_path, _attention(signal, stale=stale, stock_name=_name(n)))
    prefix = f"利益保全注意 8306 {_name(n)}（本人）\n1,850円"
    assert len(text) == expected_len
    assert text.startswith(prefix)  # 判定・銘柄・所有者・現在値は常に残る
    assert ("高値比" in text) is reason_kept
    assert (_LABEL in text) is stale  # 警告は STALE のときだけ、競合の有無にかかわらず残る
    if stale:
        assert text.startswith(f"{prefix}｜{_LABEL}")


def test_a3_the_conflict_range_really_differs_between_stale_and_non_stale(tmp_path: Path) -> None:
    """★ 前提: STALE のときだけ理由文が落ちる範囲が実在する(競合のテストが空振りしない)。"""
    for signal, n in ((_CANDIDATE, 27), (_STRONG, 18)):
        stale_text = _text(tmp_path, _attention(signal, stale=True, stock_name=_name(n)))
        plain_text = _text(tmp_path, _attention(signal, stale=False, stock_name=_name(n)))
        assert "高値比" in plain_text
        assert "高値比" not in stale_text
        assert _LABEL in stale_text


def test_the_price_is_dropped_before_the_label_only_for_extremely_long_names(
    tmp_path: Path,
) -> None:
    """現在値は formatter 上は任意セグメントのため、銘柄名が 48 字以上のときは警告より先に落ちる
    (#474 の SELL・WATCH と同じ既存の性質。本件では変更しない)。47 字までは現在値も残る。"""
    kept = _text(tmp_path, _attention(_CANDIDATE, stale=True, stock_name=_name(47)))
    dropped = _text(tmp_path, _attention(_CANDIDATE, stale=True, stock_name=_name(48)))

    assert "1,850円" in kept
    assert "1,850円" not in dropped
    assert _LABEL in kept
    assert _LABEL in dropped  # 警告は残る


# --- T4: 完全一致(substring / prefix / 追記は一致としない)---------------------------------


@pytest.mark.parametrize(
    "near_miss",
    [
        FINANCIAL_STALE_WARNING_TEXT + "。",
        FINANCIAL_STALE_WARNING_TEXT[:-1],
        "※" + FINANCIAL_STALE_WARNING_TEXT,
        " " + FINANCIAL_STALE_WARNING_TEXT,
        FINANCIAL_STALE_WARNING_TEXT.replace("可能性がある", "可能性が高い"),
        "決算未反映",
    ],
)
def test_a_near_miss_of_the_warning_does_not_produce_the_label(
    tmp_path: Path, near_miss: str
) -> None:
    text = _text(tmp_path, _attention(_CANDIDATE, stale=False, key_risks=[near_miss]))
    assert _LABEL not in text
    assert text == f"{_HEAD}｜{_WITH_REASON}"


def test_an_exact_element_among_other_key_risks_produces_the_label(tmp_path: Path) -> None:
    text = _text(
        tmp_path,
        _attention(_CANDIDATE, stale=False, key_risks=["別の留意", FINANCIAL_STALE_USER_WARNING]),
    )
    assert text == f"{_HEAD}｜{_LABEL}｜{_WITH_REASON}"


def test_valuation_caveats_alone_do_not_produce_the_label_and_never_duplicate_it(
    tmp_path: Path,
) -> None:
    alone = _text(
        tmp_path,
        _attention(_CANDIDATE, stale=False, valuation_caveats=[FINANCIAL_STALE_USER_WARNING]),
    )
    both = _text(
        tmp_path,
        _attention(_CANDIDATE, stale=True, valuation_caveats=[FINANCIAL_STALE_USER_WARNING]),
    )
    assert _LABEL not in alone
    assert both.count(_LABEL) == 1


# --- T5: 監視終了・他の builder は変更しない ----------------------------------------------


def test_the_watch_end_notification_does_not_get_the_label() -> None:
    rec = _attention(_CANDIDATE, stale=True).model_copy(
        update={
            "watch_end_reason": "PRICE_OUT_OF_RANGE",
            "watch_previous_consecutive_business_days": 3,
        }
    )
    ended = build_watch_end_text_input(rec)

    assert ended.financial_stale_label is None
    assert _LABEL not in format_notification_text(ended)


def test_the_attention_builder_sets_the_label_only_for_the_exact_warning() -> None:
    with_warning = build_attention_text_input(_attention(_STRONG, stale=True), _ORIGIN[_STRONG])
    without = build_attention_text_input(_attention(_STRONG, stale=False), _ORIGIN[_STRONG])

    assert with_warning.financial_stale_label == FINANCIAL_STALE_SHORT_LABEL
    assert without.financial_stale_label is None


# --- T6: 重複抑止の identity は警告の有無で変わらない ----------------------------------------


@pytest.mark.parametrize("signal", [_CANDIDATE, _STRONG])
def test_the_dedup_identity_does_not_depend_on_the_warning(tmp_path: Path, signal: str) -> None:
    """content_hash(event identity)は本文を含まず、警告が付いても同じ局面は同じ identity になる。"""
    plain = _attention(signal, stale=False)
    stale = _attention(signal, stale=True)
    # 同じ局面(basis_date・peak_date・peak_price が同じ)として比べるため、3 値を揃える。
    event = {
        "profit_protection_basis_date": dt.date(2026, 9, 1),
        "profit_protection_peak_date": dt.date(2026, 9, 20),
        "profit_protection_peak_price": Decimal("2100"),
    }
    plain = plain.model_copy(update=event)
    stale = stale.model_copy(update=event)

    assert _compute_attention_event_identity(plain) == _compute_attention_event_identity(stale)
    assert _send(tmp_path, plain)[1] == _send(tmp_path, stale)[1]
    # 本文は実際に異なる(同じ identity でも、本文の差は警告の有無による)
    assert _text(tmp_path, plain) != _text(tmp_path, stale)

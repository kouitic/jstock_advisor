"""Issue #902: 重要開示検知の速報(`notify_disclosure_risk`)の重複抑止の回帰テスト。

## 何の不具合か

「同一開示は再送しない」という契約に対し、重複の判定が**その銘柄・種別の直近1件**との比較
だけだったため、同じ銘柄に一致する開示が2件以上あると、A を送ると直近が A、B を送ると
直近が B になり、次の実行で A が「直近と違う」として**再送**された(A→B→A→B…)。

## 修正

比較相手を、その銘柄・IMPORTANT_DISCLOSUREの**読めた送信記録の全件**へ広げた(同じ
`latest_by_stock_and_type()` の結果の `records` を使うため、読取は増えない)。比較範囲は
記録が残っている期間(TTL 730日)であり、`_LOOKBACK_DAYS`(7日。取得する開示の範囲)ではない。

## 変えないもの(このテストが固定する)

- 通知の文面・条件・キーワードの判定(送った本文は修正前後で同じ)
- 異なる開示は従来どおり送る / 別の銘柄の同名の開示は互いに抑止しない(hash に銘柄が入る)
- 過去の記録を読めなかったとき(undecidable)は送らない(#279)。★ undecidable を**先に**
  確認する順序は、戻り値ではなく**警告ログ**で固定する(どちらの順序でも戻り値は False に
  なるため、戻り値だけでは順序を区別できない)
- VALIDATION では記録を保存しないため、抑止の根拠は増えない(従来どおり)
- claim の identity(`prev` を含む)と、claim が有効な構成での動作

## 限界

- 窓(TTL 730日)を跨ぐ合成は費用対効果が悪いため置かない(窓は docstring に明記)。
- 同型の「latest のみと比べる」形(BATCH_SUMMARY・WATCHLIST_AUTO_ADDITION・
  PROFIT_PROTECTION_ATTENTION)は、一意な事実の通知ではなく繰り返し起きる状態の通知で、
  同じ内容が再び成立したときの再通知が妥当でありうるため、このテストの範囲外(#902 の設計)。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
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
from jstock_advisor.services.line_notification_service import LineNotificationService

_CONFIG = load_config()
_T0 = dt.datetime(2026, 10, 1, 1, 0, tzinfo=dt.UTC)
_STOCK = "0000"  # 架空のコード
_OTHER_STOCK = "0001"
_KEYWORD = ["第三者委員会"]


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _build(
    tmp_path: Path, *, validation: bool = False
) -> tuple[LineNotificationService, _FakeLineClient]:
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    kwargs: dict[str, object] = {}
    if validation:
        kwargs["execution_context"] = ExecutionContext(mode=ExecutionMode.VALIDATION)
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        # Issue #17: 本番構成と同じく claim 機構を有効にした状態で通す(T6)
        notification_claim_repository=NotificationClaimRepository(store_dir=store_dir),
        **kwargs,  # type: ignore[arg-type]
    )
    return service, client


@pytest.fixture
def service_and_client(tmp_path: Path) -> tuple[LineNotificationService, _FakeLineClient]:
    return _build(tmp_path)


def _notify(
    service: LineNotificationService,
    title: str,
    *,
    published: dt.datetime,
    now: dt.datetime,
    stock: str = _STOCK,
) -> bool:
    return service.notify_disclosure_risk(
        stock_code=stock,
        disclosure_title=title,
        disclosure_summary="合成の概要",
        matched_keywords=_KEYWORD,
        published_at=published,
        now=now,
    )


_PUB_A = _T0
_PUB_B = _T0 + dt.timedelta(days=1)


# --- T1: A→B→A の最後の A は送られない(★ 本不具合の核心。修正前は 3 回目が True) ----------------


def test_t1_a_then_b_then_a_does_not_resend_a(service_and_client) -> None:
    service, client = service_and_client

    first = _notify(service, "開示A", published=_PUB_A, now=_T0)
    second = _notify(service, "開示B", published=_PUB_B, now=_T0 + dt.timedelta(minutes=1))
    third = _notify(service, "開示A", published=_PUB_A, now=_T0 + dt.timedelta(hours=2))

    assert (first, second, third) == (True, True, False)
    assert len(client.sent) == 2


# --- T2: 本文の再現(A・B を 1 実行として 2 時間ずつ進めて 3 回)= 送信の合計は 2(修正前は 6) -----


def test_t2_three_runs_of_a_and_b_send_each_disclosure_exactly_once(service_and_client) -> None:
    service, client = service_and_client

    results = []
    for run in range(3):
        now = _T0 + dt.timedelta(hours=2 * run)
        results.append(_notify(service, "開示A", published=_PUB_A, now=now))
        results.append(_notify(service, "開示B", published=_PUB_B, now=now))

    assert results == [True, True, False, False, False, False]
    assert len(client.sent) == 2


def test_m_disclosures_are_each_sent_once_across_many_runs(service_and_client) -> None:
    """一致する開示が M 件(M = 4)あっても、送信は M 件 × 1 回(修正前は実行のたびに M 件)。"""
    service, client = service_and_client
    disclosures = [(f"開示{i}", _T0 + dt.timedelta(days=i)) for i in range(4)]

    for run in range(3):
        now = _T0 + dt.timedelta(hours=2 * run)
        for title, published in disclosures:
            _notify(service, title, published=published, now=now)

    assert len(client.sent) == 4


# --- T3: 既存の挙動は維持(同じ開示は抑止 / 違う開示は送る)-------------------------------------


def test_t3_same_disclosure_is_not_resent_and_different_one_is_sent(service_and_client) -> None:
    service, client = service_and_client

    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True
    assert _notify(service, "開示A", published=_PUB_A, now=_T0 + dt.timedelta(hours=1)) is False
    assert _notify(service, "開示B", published=_PUB_B, now=_T0 + dt.timedelta(hours=2)) is True
    assert len(client.sent) == 2


def test_t3b_same_title_with_a_different_published_at_is_a_different_disclosure(
    service_and_client,
) -> None:
    """同一開示 = 公開日時 + タイトル(docstring の契約)。題名が同じでも公開日時が違えば別の開示。"""
    service, client = service_and_client

    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True
    assert _notify(service, "開示A", published=_PUB_B, now=_T0 + dt.timedelta(hours=1)) is True
    assert len(client.sent) == 2


# --- T4: 別の銘柄の同じ開示は互いに抑止しない(hash に銘柄が入る)--------------------------------


def test_t4_other_stock_with_the_same_title_and_time_is_not_suppressed(service_and_client) -> None:
    service, client = service_and_client

    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True
    assert (
        _notify(
            service,
            "開示A",
            published=_PUB_A,
            now=_T0 + dt.timedelta(minutes=1),
            stock=_OTHER_STOCK,
        )
        is True
    )
    # それぞれの銘柄の中では、A→B→A の再送が起きない
    assert (
        _notify(
            service,
            "開示B",
            published=_PUB_B,
            now=_T0 + dt.timedelta(minutes=2),
            stock=_OTHER_STOCK,
        )
        is True
    )
    assert (
        _notify(
            service,
            "開示A",
            published=_PUB_A,
            now=_T0 + dt.timedelta(minutes=3),
            stock=_OTHER_STOCK,
        )
        is False
    )
    assert len(client.sent) == 3


# --- T5: 過去の記録を読めないとき(undecidable)は送らない(#279)。順序は警告ログで固定 -----------


def _seed_broken_and_matching_rows(store_dir: Path, *, matching_hash: str | None) -> None:
    """decode できない 1 件(sent_at が日時でない)と、必要なら decode できる 1 件を置く。"""
    store_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "notification_id": "22222222-2222-4222-8222-222222222222",
            "notification_type": NotificationType.IMPORTANT_DISCLOSURE.value,
            "stock_code": _STOCK,
            "content_hash": "broken-hash-0001",
            "sent_at": "not-a-timestamp",
            "related_recommendation_id": None,
            "owner": None,
            "holding_id": None,
        }
    ]
    if matching_hash is not None:
        rows.append(
            {
                "notification_id": "11111111-1111-4111-8111-111111111111",
                "notification_type": NotificationType.IMPORTANT_DISCLOSURE.value,
                "stock_code": _STOCK,
                "content_hash": matching_hash,
                "sent_at": "2026-09-30T00:00:00+00:00",
                "related_recommendation_id": None,
                "owner": None,
                "holding_id": None,
            }
        )
    (store_dir / "notification_log.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


def test_t5_undecidable_history_suppresses_sending(tmp_path: Path, caplog) -> None:
    """読めなかった記録がある銘柄は、(まだ送っていない開示でも)送らない。"""
    _seed_broken_and_matching_rows(tmp_path / "local_store", matching_hash=None)
    service, client = _build(tmp_path)

    with caplog.at_level("WARNING"):
        sent = _notify(service, "開示A", published=_PUB_A, now=_T0)

    assert sent is False
    assert client.sent == []
    assert "past log undecidable" in caplog.text


def test_t5b_undecidable_is_checked_before_the_full_comparison(tmp_path: Path, caplog) -> None:
    """★ 順序の固定: 読めなかった記録があり、かつ読めた記録の中に同じ開示がある場合でも、
    先に undecidable として扱う(警告ログが出る)。

    戻り値はどちらの順序でも False になるため、戻り値では順序を区別できない。区別できるのは
    警告ログだけで、これが出ることで『undecidable を先に確認する』(`.latest` や全件比較より先に
    `undecidable` を見る、という NotificationLookup の契約)を固定する。
    """
    import hashlib

    # 同じ開示の hash を、サービスと同じ式で作る(同じ式であることは下の T5c で確認)
    matching_hash = hashlib.sha256(f"{_STOCK}|{_PUB_A.isoformat()}|開示A".encode()).hexdigest()[:16]
    _seed_broken_and_matching_rows(tmp_path / "local_store", matching_hash=matching_hash)
    service, client = _build(tmp_path)

    with caplog.at_level("WARNING"):
        sent = _notify(service, "開示A", published=_PUB_A, now=_T0)

    assert sent is False
    assert client.sent == []
    assert "past log undecidable" in caplog.text


def test_t5c_the_test_hash_expression_matches_the_service(
    service_and_client, tmp_path: Path
) -> None:
    """T5b が作る hash が、サービスが保存する content_hash と同じ式であることの確認。"""
    import hashlib

    service, _client = service_and_client
    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True
    saved = NotificationLogRepository(store_dir=tmp_path / "local_store").list_by_stock_and_type(
        _STOCK, NotificationType.IMPORTANT_DISCLOSURE
    )
    expected = hashlib.sha256(f"{_STOCK}|{_PUB_A.isoformat()}|開示A".encode()).hexdigest()[:16]
    assert [r.content_hash for r in saved.records] == [expected]


# --- T6: claim が有効な構成でも、A→B→A の 3 回目は claim へ到達する前に抑止される ---------------


def test_t6_third_call_is_suppressed_before_the_claim_is_acquired(
    service_and_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client = service_and_client
    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True
    assert _notify(service, "開示B", published=_PUB_B, now=_T0 + dt.timedelta(minutes=1)) is True

    acquired: list[str] = []
    original = service._acquire_send_claim

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        acquired.append(kwargs.get("identity", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_acquire_send_claim", _spy)

    sent = _notify(service, "開示A", published=_PUB_A, now=_T0 + dt.timedelta(hours=2))

    assert sent is False
    assert acquired == []  # 送信済みの開示は claim へ到達しない(prev が変わっても)
    assert len(client.sent) == 2


# --- T7: VALIDATION では記録を保存しない = 抑止の根拠は増えない(従来どおり)-----------------------


def test_t7_validation_mode_does_not_store_records_so_nothing_is_suppressed(tmp_path: Path) -> None:
    service, _client = _build(tmp_path, validation=True)

    first = _notify(service, "開示A", published=_PUB_A, now=_T0)
    second = _notify(service, "開示A", published=_PUB_A, now=_T0 + dt.timedelta(hours=1))

    assert (first, second) == (True, True)
    stored = NotificationLogRepository(store_dir=tmp_path / "local_store").list_all()
    assert stored == []


# --- 読取は増えない: 1 回の呼び出しで記録の読取は 1 回(全件比較のための追加読取はない)-----------


def test_a_call_reads_the_notification_log_exactly_once(
    service_and_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 実際の記録の読取(`list_by_stock_and_type`)は、1 回の呼び出しにつき 1 回。
    全件比較のために、直近 1 件の取得とは別に読み直さない(`latest_by_stock_and_type` が
    `list_by_stock_and_type` の結果そのものを返す、という前提の固定)。"""
    service, _client = service_and_client
    assert _notify(service, "開示A", published=_PUB_A, now=_T0) is True

    repo = service._log_repo
    reads: list[tuple[str, NotificationType]] = []
    original = repo.list_by_stock_and_type

    def _counting(stock_code: str, notification_type: NotificationType):  # type: ignore[no-untyped-def]
        reads.append((stock_code, notification_type))
        return original(stock_code, notification_type)

    monkeypatch.setattr(repo, "list_by_stock_and_type", _counting)

    assert _notify(service, "開示B", published=_PUB_B, now=_T0 + dt.timedelta(hours=1)) is True
    assert _notify(service, "開示A", published=_PUB_A, now=_T0 + dt.timedelta(hours=2)) is False

    assert reads == [
        (_STOCK, NotificationType.IMPORTANT_DISCLOSURE),
        (_STOCK, NotificationType.IMPORTANT_DISCLOSURE),
    ]  # 2 回の呼び出し = 2 回の読取(各 1 回)


# --- 本文は変わらない(修正は『送るか』だけで、『何を送るか』は不変)-------------------------------


def test_sent_message_content_is_unchanged(service_and_client) -> None:
    service, client = service_and_client

    _notify(service, "開示A", published=_PUB_A, now=_T0)

    text = client.sent[0]
    assert text.startswith(f"【重要開示検知】{_STOCK}")
    assert "検出キーワード: 第三者委員会" in text
    assert "開示タイトル: 開示A" in text
    assert "概要: 合成の概要" in text
    assert "対応内容: 開示内容を確認し、投資前提に影響がないか確認してください。" in text

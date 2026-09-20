"""Issue #255: 通知文面の golden テスト(5 種類の LINE 本文をスナップショットで固定する)。

## なぜ必要か

利用者が実際に受け取るのは、通知の**文面**である。文面は、判定ロジック・整形部品・設定値のいずれの
変更からも影響を受けるが、通常のテストは値を見ており、文面全体を見ていない。そのため、意図しない
形で文面が変わっても誰も気づかない(#222 では銘柄分析の表示を変更したが、その種の変更が他の文面へ
波及していないことを機械的に確認する手段が無かった)。

## 何を固定するか(5 種類)

    1 銘柄分析の通知      StockAnalysisViewService(LINE の銘柄分析の返信)。3 スナップショット
                          BUY 判定の詳細 / 保有の利確判定の状況(#222 N-5)/ 保有継続の事実(#222 N-3)
    2 利確判定の通知      実送信の本文(_render_notification_body)/ FULL_PROFIT_TAKE
    3 売却判断の通知      実送信の本文(_render_notification_body)/ SELL
    4 監視銘柄の追加通知   render_watchlist_addition_message(追加あり)
    5 取得失敗日の通知    render_watchlist_addition_message(候補一覧の取得に失敗した日。#234)

スナップショットは `tests/unit/golden/notification/*.txt`。**「変えてはいけない」ものではなく、
「変えたことに気づく」ためのもの**である。意図した変更なら、スナップショットを更新して
同じ PR に含める(更新手順は同ディレクトリの README.md)。

## fixture の規則

- 架空値のみ。実在の銘柄コード・企業名・所有者名・保有数量・取得単価・含み益の実額を使わない
  (銘柄コードは実在しない `0000` 系、企業名は `架空銘柄A` のように書く)
- 日付・時刻は固定する(実行日時・実行 ID など、実行ごとに変わる値を含めない)。実時刻に依存しない
- 失敗時は、どの行が変わったかが分かる unified diff を出す
"""

from __future__ import annotations

import datetime as dt
import difflib
import os
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding_evaluation_record import (
    HoldingEvaluationRecord,
    build_holding_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services import line_notification_service as line_notification_service_module
from jstock_advisor.services.line_notification_service import render_watchlist_addition_message
from jstock_advisor.services.stock_analysis_view_service import StockAnalysisViewService
from jstock_advisor.services.watchlist_addition_summary_builder import (
    EvaluationHighlight,
    WatchlistAdditionItemView,
    WatchlistAdditionSummary,
)

_GOLDEN_DIR = Path(__file__).parent / "golden" / "notification"
_UPDATE_ENV = "UPDATE_GOLDEN"

# 固定の時刻(実時刻に依存しない)。
_NOW = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)
_EVALUATED_AT = dt.datetime(2026, 9, 10, 21, 0, tzinfo=dt.UTC)

# 架空の銘柄コード・名称(実在しない値)。
_CODE = "0000"
_NAME = "架空銘柄A"


def _normalize(text: str) -> str:
    """改行コードと末尾の改行の差(エディタ・git の autocrlf)を、比較の対象から外す。"""
    return text.replace("\r\n", "\n").rstrip("\n")


def assert_matches_golden(name: str, actual: str) -> None:
    """`actual` が `golden/notification/<name>.txt` と一致することを確認する。

    一致しない場合は、どの行が変わったかが分かる unified diff を、失敗メッセージへ出す。
    環境変数 `UPDATE_GOLDEN=1` を付けて実行すると、スナップショットを現在の出力で書き換える
    (意図した変更のときだけ使う。書き換え後の差分は PR で必ず読む)。
    """
    path = _GOLDEN_DIR / f"{name}.txt"
    if os.environ.get(_UPDATE_ENV) == "1":
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_normalize(actual) + "\n", encoding="utf-8", newline="\n")
        return
    if not path.exists():
        pytest.fail(
            f"スナップショットが無い: {path.name}。意図した新規追加なら、"
            f"`{_UPDATE_ENV}=1` を付けて実行して作成し、内容を確認して PR に含める"
        )
    expected = _normalize(path.read_text(encoding="utf-8"))
    actual_normalized = _normalize(actual)
    if actual_normalized != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual_normalized.splitlines(),
                fromfile=f"golden/{path.name}",
                tofile="actual",
                lineterm="",
            )
        )
        pytest.fail(
            f"通知文面が {path.name} と一致しない。意図した変更なら `{_UPDATE_ENV}=1` で"
            "スナップショットを更新して PR に含める(手順は golden/notification/README.md)。"
            f"\n{diff}"
        )


# --- 検査そのものの確認 ------------------------------------------------------------------


def test_the_golden_check_reports_a_readable_diff_and_ignores_only_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検査が「常に緑」でないこと・差分が読めること・改行コードの差だけを無視することを確認する。"""
    monkeypatch.setattr(f"{__name__}._GOLDEN_DIR", tmp_path)
    monkeypatch.delenv(_UPDATE_ENV, raising=False)  # 更新モードで実行しても、比較の側を確かめる
    (tmp_path / "sample.txt").write_text("1行目\n2行目\n", encoding="utf-8", newline="\n")

    assert_matches_golden("sample", "1行目\n2行目")  # 末尾の改行の有無は無視する
    assert_matches_golden("sample", "1行目\r\n2行目\r\n")  # CRLF は無視する
    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_matches_golden("sample", "1行目\n2行目が変わった")
    message = str(excinfo.value)
    assert "-2行目" in message
    assert "+2行目が変わった" in message
    with pytest.raises(pytest.fail.Exception, match="スナップショットが無い"):
        assert_matches_golden("missing", "x")


# --- 1 銘柄分析の通知 -------------------------------------------------------------------


class _FakeDisplayNameResolver:
    def resolve(self, stock_code: str) -> str:
        return _NAME


def _analysis_service(store_dir: Path) -> StockAnalysisViewService:
    return StockAnalysisViewService(
        evaluation_record_repository=BuyCandidateEvaluationRecordRepository(store_dir=store_dir),
        latest_batch_pointer_repository=LatestBuyCandidateBatchPointerRepository(
            store_dir=store_dir
        ),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        holding_evaluation_record_repository=HoldingEvaluationRecordRepository(store_dir=store_dir),
        audit_log_repository=AuditLogRepository(store_dir=store_dir),
        display_name_resolver=_FakeDisplayNameResolver(),  # type: ignore[arg-type]
    )


def test_stock_analysis_notification_body(tmp_path: Path) -> None:
    """銘柄分析(BUY 判定の詳細)。#222 で文面を整えた領域(事実・解釈・総合判断・価格目安)。"""
    LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path).update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=1
        )
    )
    RecommendationRepository(store_dir=tmp_path).save(
        Recommendation(
            recommendation_id="rec-1",
            stock_code=_CODE,
            stock_name=_NAME,
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH_BUY,
            price_at_recommendation=Decimal("150"),
            confidence=ConfidenceLevel.MEDIUM,
            rule_version="v1",
            buy_action=BuyAction.BUY,
            raw_buy_action=BuyAction.BUY,
            company_quality_score=62.77,
            buy_prices=BuyPriceLevels(
                entry=PriceWithRationale(price=Decimal("160"), rationale="x"),
                standard=PriceWithRationale(price=Decimal("155"), rationale="x"),
                strong=PriceWithRationale(price=Decimal("145"), rationale="x"),
            ),
            buy_decision_reasons=(
                BuyDecisionReason(
                    code="PRICE_TIER",
                    message="x",
                    actual_value=Decimal("150"),
                    threshold_value=None,
                ),
            ),
            dividend_yield_pct_at_recommendation=3.2,
            shareholder_benefit_yield_pct_at_recommendation=1.0,
            total_yield_pct_at_recommendation=4.2,
            buy_score_input_facts={
                "current_per": "9.8",
                "current_pbr": "0.82",
                "historical_per_median": "12.3",
                "historical_pbr_median": "1.05",
                "equity_ratio_pct": 55.5,
                "payout_ratio_pct": 25.0,
                "consecutive_dividend_increase_years": 4,
                "is_progressive_or_doe_policy": True,
                "operating_income_non_decrease_ratio": 0.33,
                "annualized_volatility_pct": 38.5,
            },
        )
    )
    BuyCandidateEvaluationRecordRepository(store_dir=tmp_path).upsert(
        BuyCandidateEvaluationRecord(
            evaluation_id=f"batch-1:{_CODE}",
            batch_id="batch-1",
            stock_code=_CODE,
            evaluated_at=_NOW,
            rule_version="v1",
            candidate_source=CandidateSource.WATCHLIST,
            purchase_category=PurchaseCategory.BUY_CANDIDATE,
            final_buy_action=BuyAction.BUY,
            raw_buy_action=BuyAction.BUY,
            recommendation_id="rec-1",
        )
    )

    text = _analysis_service(tmp_path).build_buy_analysis_text(_CODE)

    assert_matches_golden("stock_analysis_buy", text)


#: 架空の所有者。holding_id は `<所有者>#<銘柄コード>`。
_OWNER = "所有者A"
_HOLDING_ID = f"{_OWNER}#{_CODE}"


def _save_holding_evaluation_record(store_dir: Path, **overrides: object) -> None:
    defaults: dict[str, object] = dict(
        holding_evaluation_id=build_holding_evaluation_id(_HOLDING_ID, _NOW),
        holding_id=_HOLDING_ID,
        owner=_OWNER,
        stock_code=_CODE,
        evaluated_at=_NOW,
        rule_version="v1",
        authoritative_engine="PROFIT_TAKING",
        authoritative_outcome_category="watch",
    )
    defaults.update(overrides)
    HoldingEvaluationRecordRepository(store_dir=store_dir).save(
        HoldingEvaluationRecord(**defaults)  # type: ignore[arg-type]
    )


def test_stock_analysis_holding_profit_taking_status_body(tmp_path: Path) -> None:
    """銘柄分析(保有銘柄)。#222 N-5: 含み益率・上値余地・まだ利確しない理由を本文へ出す領域。"""
    RecommendationRepository(store_dir=tmp_path).save(
        Recommendation(
            recommendation_id="rec-watch",
            stock_code=_CODE,
            stock_name=_NAME,
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH,
            price_at_recommendation=Decimal("3000"),
            confidence=ConfidenceLevel.MEDIUM,
            rule_version="v1",
            reasons=["含み益率が監視の基準に達しました"],
            unrealized_profit_loss_pct=Decimal("32.5"),
            profit_taking_upside_pct=8.4,
            not_yet_action_reasons=["決算発表が近いため、価格基準の利確判定を保留しています"],
        )
    )
    _save_holding_evaluation_record(
        tmp_path,
        authoritative_recommendation_id="rec-watch",
        authoritative_engine="PROFIT_TAKING",
        authoritative_outcome_category="watch",
    )

    text = _analysis_service(tmp_path).build_holding_analysis_text(_OWNER, _CODE)

    assert_matches_golden("stock_analysis_holding_profit_taking_status", text)


def test_stock_analysis_holding_hold_facts_body(tmp_path: Path) -> None:
    """銘柄分析(保有継続)。#222 N-3: 投資前提悪化ルールの状況を、ラベルと値で出す領域。"""
    AuditLogRepository(store_dir=tmp_path).save(
        AuditLogEntry(
            audit_id="audit-hold",
            timestamp=_NOW,
            decision_type="sell_signal",
            stock_code=_CODE,
            input_values={
                "rule_evidence_details": [
                    {
                        "rule_name": "balance_sheet_insolvency",
                        "status": "NOT_TRIGGERED",
                        "current_value": "36.4%",
                        "threshold": "0%",
                        "explanation": "自己資本比率はマイナスではない(債務超過ではない)",
                    }
                ]
            },
            calculation_formulas={},
            output_values={},
            data_sources=[],
            rule_version="v1",
        )
    )
    _save_holding_evaluation_record(
        tmp_path,
        authoritative_recommendation_id=None,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
        authoritative_audit_log_id="audit-hold",
    )

    text = _analysis_service(tmp_path).build_holding_analysis_text(_OWNER, _CODE)

    assert_matches_golden("stock_analysis_holding_hold_facts", text)


# --- 2 利確判定の通知 / 3 売却判断の通知(実送信の本文)---------------------------------------


def test_profit_taking_notification_body() -> None:
    """利確判定(全部売却の検討)。実際に LINE へ送信される本文(短文の経路)。"""
    recommendation = Recommendation(
        recommendation_id="rec-profit",
        stock_code=_CODE,
        stock_name=_NAME,
        recommended_at=_NOW,
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            full_profit_consideration_price=PriceWithRationale(price=Decimal("1650"), rationale="x")
        ),
        price_at_recommendation=Decimal("1500"),
        reasons=["適正価格レンジ上限を超過"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )

    body = line_notification_service_module._render_notification_body(recommendation)

    assert_matches_golden("profit_taking", body)


def test_sell_notification_body() -> None:
    """売却判断(SELL)。実際に LINE へ送信される本文(短文の経路)。"""
    recommendation = Recommendation(
        recommendation_id="rec-sell",
        stock_code=_CODE,
        stock_name=_NAME,
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("1100"), rationale="x")
        ),
        price_at_recommendation=Decimal("1234"),
        average_purchase_price_at_recommendation=Decimal("1000"),
        shares_at_recommendation=10,
        reasons=["減配(major)", "自己資本比率が閾値を下回った"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        recommended_action_summary="複数の独立した根拠に基づき投資前提の悪化が疑われます。",
        holding_risks=["自己資本比率が閾値を下回っている"],
        independent_evidence_group_count=2,
    )

    body = line_notification_service_module._render_notification_body(recommendation)

    assert_matches_golden("sell", body)


# --- 4 監視銘柄の追加通知 / 5 取得失敗日の通知 ---------------------------------------------


def _watchlist_summary(
    *,
    items: list[WatchlistAdditionItemView],
    universe_fetch_failed: bool = False,
    universe_source_date: str | None = None,
) -> WatchlistAdditionSummary:
    return WatchlistAdditionSummary(
        policy_name="multi_style_monitoring",
        policy_label="複合スタイル監視",
        policy_conditions=["架空の条件A", "架空の条件B"],
        total_target_count=300,
        ranked_count=280,
        data_unavailable_count=20,
        added_count=len(items),
        addition_rate_pct=len(items) / 300 * 100,
        evaluated_at=_EVALUATED_AT,
        items=items,
        universe_fetch_failed=universe_fetch_failed,
        universe_source_date=universe_source_date,
    )


def test_watchlist_addition_notification_body() -> None:
    items = [
        WatchlistAdditionItemView(
            stock_code="0001",
            display_name="架空銘柄B",
            rank=1,
            total_score=78.4,
            highlights=[
                EvaluationHighlight(label="配当利回り", detail="3.8%", score=9.0),
                EvaluationHighlight(label="自己資本比率", detail="62.0%", score=8.0),
            ],
        ),
        WatchlistAdditionItemView(
            stock_code="0002",
            display_name="架空銘柄C",
            rank=2,
            total_score=71.0,
            highlights=[],
        ),
    ]

    body = render_watchlist_addition_message(_watchlist_summary(items=items))

    assert_matches_golden("watchlist_addition", body)


def test_universe_fetch_failure_day_notification_body() -> None:
    """候補一覧の取得に失敗した日(#234)。追加 0 件でも通知が出る日の本文。"""
    body = render_watchlist_addition_message(
        _watchlist_summary(items=[], universe_fetch_failed=True, universe_source_date="2026-09-01")
    )

    assert_matches_golden("universe_fetch_failure_day", body)

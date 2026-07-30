# BUY候補通知パイプライン 第3次修正 完了報告(2026-07-31)

コミット`7e1ba0a`(第2次修正)以降のレビューで指摘された5件の問題を修正した。
すべて「個別銘柄の例外処理」ではなく、共通ロジック(config設定・enum・
既存の信頼性ゲート機構)として実装している。

## 1. 変更ファイル一覧

- `config/notification_rules.yaml` — 新規設定キー追加
- `src/jstock_advisor/config/models.py` — `BuyCandidatesNotificationConfig`/`OperationsNotificationConfig`追加
- `src/jstock_advisor/domain/entities/enums.py` — `NotificationContext`追加
- `src/jstock_advisor/domain/entities/valuation.py` — `FairValueRange.outlier_filter_blocking_reason`追加
- `src/jstock_advisor/domain/valuation/valuation_methods.py` — `apply_outlier_filters()`の全面書き換え、`OutlierFilterResult`追加
- `src/jstock_advisor/domain/valuation/buy_price_reliability.py` — `outlier_filter_blocking_reason`パラメータ追加
- `src/jstock_advisor/services/buy_signal_service.py` — 新パラメータの配線
- `src/jstock_advisor/services/line_notification_service.py` — `NotificationContext`対応、`_margin_adjustment_reasons_line()`のフィルタ追加
- `src/jstock_advisor/lambda_handlers/buy_candidates_handler.py` — データエラー通知の設定化、`_finalize_batch()`の繰り上げ方式化
- `tests/unit/test_buy_candidates_handler.py` / `test_line_notification_service.py` / `test_valuation_methods.py` — 新規テスト追加・既存テストの型調整
- `docs/functional_spec.md` — 5.4節に本改訂の挙動を追記、変更履歴に追記

## 2. データ取得エラー通知の修正

**調査結果**: `notify_data_error()`自体は第2次修正の時点で既にLINE送信を行わない実装
(`logger.warning`のみ、`return False`)になっていた。今回の修正は、この「LINE送信しない」
という挙動を**呼び出し側で明示的・設定駆動にする**ことが目的であり、既存の暗黙的な
無害化に依存させないための構造変更である。

- `config/notification_rules.yaml`に`notification.buy_candidates.notify_data_errors`
  (既定`false`)を追加。`false`の場合、`buy_candidates_handler.py`は
  `notification_service.notify_data_error(...)`自体を呼ばず、`logger.warning(...)`と
  `data_insufficient`カテゴリへの計上のみを行う。
- `notification.operations.notify_batch_failure`(既定`true`)も追加し、将来の運用障害
  通知がBUY候補の個別データエラー通知と混同されないよう、設定上明確に分離した(現時点で
  対応する新規の障害通知機構自体は未実装。設定キーの土台のみ追加)。
- 監査ログへの記録は、`BuySignalService.analyze()`の`snapshot is None`分岐が既に
  `output_values={"data_error": ..., "final_buy_action": "DATA_INSUFFICIENT"}`として
  無条件に記録しており、追加実装は不要だった(第2次修正時の調査結果と同じ構造)。

## 3. 上位5件選定ロジック(繰り上げ方式)

**修正前**: `buy_entries[:max_notifications]`で上位N件を先に確定し、その中だけを
`evaluate_notification_status`で評価していた。上位N件の一部が再通知抑止・データ品質
チェックで除外されると、下位に適格な候補が残っていても繰り上げが起こらず、実際の
送信数がN件を割り込んでいた。

**修正後**: ランキング全件を順位順に1件ずつ評価し、適格(`status == SENT`かつ
`data_quality_blocked=False`)と判定したものを`eligible_winners`へ追加、
`max_notifications`件に達した時点、または全件評価し終えた時点でループを終了する。

```python
eligible_winners: list[Recommendation] = []
for _sort_key, stock_code, recommendation_id in buy_entries:  # 全件を順位順に走査
    recommendation = recommendation_repo.get(recommendation_id)
    ...
    eligibility = notification_service.evaluate_notification_status(
        recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
    )
    if eligibility.data_quality_blocked:
        quality_blocked_count += 1
        continue
    if eligibility.status != NotificationStatus.SENT:
        suppressed_count += 1
        continue
    eligible_winners.append(recommendation)
    if len(eligible_winners) >= max_notifications:
        break
```

通知本文の表示順位(`notify_buy_candidates_digest`内で`enumerate(winners, start=1)`)は
最終的な送信順(=`eligible_winners`の順)でそのまま1..Nに振り直される(追加の変更不要)。

### テスト結果(`tests/unit/test_buy_candidates_handler.py`)

| ケース | 結果 |
|---|---|
| 1位が再通知抑止で除外、6位まで存在(上限5件) | 2位〜6位の5件が送信される(1位のみ欠落) |
| 上位5件のうち3件が抑止、下位に候補あり(全8件) | 4位〜8位の5件が送信される(上限まで埋まる) |
| 適格な候補が3件のみ(上限5件) | 3件のみ送信(存在しない候補を水増ししない) |
| 全候補が抑止対象 | 送信0件(`digest`は空リストで呼ばれる) |
| — | `evaluate_notification_status`が`NotificationContext.BUY_CANDIDATE_BATCH`付きで呼ばれることを確認 |

全5テストPASS。

## 4. BUY候補バッチでの要手動確認LINE通知の抑止

`NotificationContext`(`DEFAULT`/`BUY_CANDIDATE_BATCH`/`HOLDING_REVIEW`)を新設し、
`evaluate_notification_status(recommendation, now, context=...)`にパラメータを追加した。

`_check_data_quality()`が`requires_manual_review=True`を返した場合:
- `context == BUY_CANDIDATE_BATCH`: `notify_manual_review_required()`(LINE送信)を
  呼ばず、`NotificationOutcome(status=NOT_REQUIRED, sent=False, data_quality_blocked=True)`
  を返す。異常自体は`_check_data_quality()`内の`self._audit.record(...)`で監査ログへ
  記録済み(変更なし)。
- `context in (DEFAULT, HOLDING_REVIEW)`: 従来通り`notify_manual_review_required()`を
  呼び、実際にLINE送信する。

`buy_candidates_handler.py::_finalize_batch()`は`context=NotificationContext.BUY_CANDIDATE_BATCH`
を渡すよう変更した。SELL/保有銘柄レビュー系の呼び出し元は存在しない(`evaluate_notification_status`
の呼び出し箇所は本ハンドラの1箇所のみ)ため、実質的な呼び出し元は変更なし。

### テスト結果(`tests/unit/test_line_notification_service.py`)

- `context=BUY_CANDIDATE_BATCH`かつ要手動確認相当のアラートがある場合、
  `client.sent == []`(LINE未送信)かつ`data_quality_blocked is True`であることを確認。
- `context`省略(デフォルト)の場合、従来通り「【要手動確認】」を含むLINEメッセージが
  1件送信されることを確認(回帰防止)。

## 5. 外れ値フィルタの最小方式数を3件に

**修正前**: `len(applicable) < 2`のみをガードとしており、有効な方式が2件のときに
外れ値検知を実行していた。2件では「相手が唯一の比較対象」になるため、双方が互いを
外れ値とみなし合い、有効な方式が0件になりうる不具合があった。

**修正後**:
1. ガードを`len(applicable) < 3`に引き上げ、2件以下では外れ値検知自体を行わない
   (`methods_used_count <= 2`の低信頼シグナルは、既存の
   `determine_buy_price_reliability()`の`TOO_FEW_VALUATION_METHODS`ゲートに委ねる)。
2. 3件以上で外れ値検知を実行した結果、残る方式が1件以下になった場合は除外結果を
   採用せず、除外前の全件へフォールバックする。
3. `apply_outlier_filters()`の戻り値を`OutlierFilterResult`(`results` /
   `excluded_count` / `remaining_count` / `reliability` / `blocking_reason`)に変更。
   フォールバック時は`reliability=LOW`, `blocking_reason="TOO_FEW_METHODS_AFTER_OUTLIER_FILTER"`
   を設定する。
4. `build_valuation_summary()`が`blocking_reason`を`FairValueRange.outlier_filter_blocking_reason`
   として伝播し、`buy_signal_service.py`が`determine_buy_price_reliability()`へ渡す。
   `outlier_filter_blocking_reason`が設定されている場合、他の懸念件数にかかわらず
   単独で`reliability=LOW`とする(フォールバック後は`methods_used_count`が3のまま
   残るため、既存のTOO_FEW_VALUATION_METHODSだけでは検出できないケースをカバーする)。

### テスト結果(`tests/unit/test_valuation_methods.py`)

| 入力(円) | 期待結果 | 結果 |
|---|---|---|
| `[500, 1500]`(2件) | 外れ値検知を行わない。2件ともapplicable維持 | PASS |
| `[38, 100, 2900]`(3件) | 全滅 → 除外前へフォールバック、`reliability=LOW`、`blocking_reason`設定。中央値(100円)だけを機械的に採用しない | PASS |
| `[1000, 1050, 1100, 3000]`(4件) | 3000のみ除外、他3件維持 | PASS |
| `[50, 950, 1000, 1050]`(4件) | 50のみ除外、他3件維持 | PASS |

`build_valuation_summary()`への伝播、`determine_buy_price_reliability()`が
`outlier_filter_blocking_reason`単独でLOWを強制することも個別に検証。全7テストPASS
(既存3テストの戻り値アクセス方法の更新を含む)。

## 6. 通知は採用済み安全余裕理由のみ表示

`_margin_adjustment_reasons_line()`を、`recommendation.margin_adjustments`のうち
`superseded_by is None`(カテゴリ内で採用された)ものだけを表示するよう変更。
不採用(`superseded_by`にコードが設定されている)分はLINE本文から除外される。
監査ログ(`BuySignalService.analyze()`の`self._audit.record(...)`)は
`recommendation.margin_adjustments`をそのまま渡しているため、採用・不採用を問わず
すべて記録される点は変更していない。

### テスト結果(`tests/unit/test_line_notification_service.py`)

- 採用1件+不採用1件+採用1件のケースで、不採用分の理由文言のみLINE本文に含まれない
  ことを確認。
- 全件が不採用のケースで、「必要安全余裕を拡大した理由」の見出し自体が表示されない
  (空見出しを出さない)ことを確認。

## 7. pytest / ruff / mypy 結果

```
.venv/Scripts/python.exe -m pytest tests -q       → 744 passed
.venv/Scripts/python.exe -m ruff check src tests   → All checks passed!
.venv/Scripts/python.exe -m mypy src               → Success: no issues found in 184 source files
```

## 8. 完了条件チェックリスト

- [x] データ取得エラーの個別LINE通知を設定駆動化(既定false)し、監査ログ・件数集計は維持
- [x] 上位5件選定を繰り上げ方式に変更し、下位候補が正しく繰り上がることをテストで確認
- [x] BUY候補バッチで要手動確認LINEを送らないことを`NotificationContext`で保証
- [x] SELL/保有銘柄レビュー系の要手動確認LINE送信は従来通り維持(回帰テストで確認)
- [x] 外れ値フィルタの最小方式数を3件に引き上げ、2件の相互排除を防止
- [x] 外れ値除外で1件以下になった場合はフォールバックし、明示的な低信頼シグナルで信頼度LOWを強制
- [x] 通知本文の安全余裕理由を採用済みのみに限定、監査ログは全件保持
- [x] §6記載の必須テストケースをすべて追加
- [x] pytest全件PASS(744件)
- [x] ruff全件PASS
- [x] mypy全件PASS
- [x] `docs/functional_spec.md`の変更履歴・5.4節を更新
- [x] 個別銘柄向けの特例処理を追加せず、config・enum・既存の信頼性ゲート機構による
      共通ロジックとして実装

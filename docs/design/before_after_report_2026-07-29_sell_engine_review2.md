# 売却判定エンジン再設計 レビュー2次対応 before/afterレポート(2026-07-29基準)

前回パス([before_after_report_2026-07-29_sell_engine_redesign.md](before_after_report_2026-07-29_sell_engine_redesign.md))で実装した売却判定エンジンに対し、ソースコードの再レビューで指摘された11項目を修正した。本レポートは、その修正の実データ確認結果と、要求された出力項目(raw/final判定・格下げ理由・独立根拠グループ・一次情報確認状態・財務期間種別・反対材料評価状態・整合性検査結果)をまとめる。

## 対象5銘柄の再評価結果(実データ、2026-07-29 07:28 JST時点)

| 銘柄 | raw_recommendation_type | final_recommendation_type | 格下げ理由 | 独立根拠グループ数 | 即時執行/指値/売却目安価格 |
|---|---|---|---|---|---|
| 8306 MUFG | HOLD | HOLD | — | 0 | すべてNone |
| 4631 DIC | HOLD | HOLD | — | 0 | すべてNone |
| 5401 日本製鉄 | REVIEW | REVIEW | なし(格下げ発生せず) | 1 | すべてNone |
| 8136 サンリオ | HOLD | HOLD | — | 0 | すべてNone |
| 2914 日本たばこ産業 | HOLD | HOLD | — | 0 | すべてNone |

- 8306/4631は前回パスと同じくHOLDのまま(§2・§10-12の修正が引き続き有効であることを再確認)。
- 5401は「営業利益の継続悪化(major、EARNINGS、primary_source_confirmed=False)」の1件のみが根拠。独立根拠グループ=1のため、判定エンジン自体がREVIEW止まりで確定し、SellSignalService側のyfinance単独格下げは発生しなかった(raw=final=REVIEW)。
- 5401の`counter_factors_evaluated=False`(反対材料の一部カテゴリーを評価できなかったことを正直に示す)。信頼度はMEDIUM。
- いずれの銘柄も`immediate_execution_price`/`recommended_limit_price`/`stop_review_price`は全てNone(現在値の自動コピーは発生していない)。

## 人工ケースの確認結果(ユニットテストとして実装・実行済み)

実データでは自然に再現しない境界ケースは、`tests/unit/test_sell_signal.py`・`tests/unit/test_financial_industry.py`・`tests/unit/test_recommendation_consistency_validator.py`にユニットテストとして実装し、全件グリーンを確認した。

| ケース | 該当テスト | 結果 |
|---|---|---|
| sector欠損の銀行(自己資本比率5%) | `test_sector_missing_bank_does_not_apply_general_corporate_rule` | financial_health_severe_deterioration=NOT_EVALUATED、判定=HOLD(UNKNOWNをGENERAL_CORPORATEとして扱わない) |
| 第三者委員会設置だけで重大影響未確認 | `test_risk_keyword_only_without_material_event_confirmation_stays_review` | major_scandal=TRIGGERED、is_immediate_critical=False、判定=REVIEW(URGENT_REVIEWにならない) |
| yfinance上の予想配当0だが公式無配発表なし | `test_yfinance_forecast_zero_without_official_announcement_is_suspected` | dividend_omission=SUSPECTED(TRIGGEREDではない)、判定=HOLD |
| yfinance上で自己資本比率が負だが一次情報未確認 | `test_negative_equity_ratio_is_suspected_not_triggered` | balance_sheet_insolvency=SUSPECTED、判定=HOLD(URGENT_REVIEWにならない) |
| raw判定URGENTからREVIEWへ格下げ | 既存の`test_sell_message_with_insufficient_evidence_routes_to_manual_review`等でSELL側は確認済み。URGENT側は`test_urgent_review_with_unconfirmed_immediate_critical_flagged`で、一次情報未確認の即時criticalが独立根拠1件のみの場合に整合性検査が発火することを確認 | 整合性検査`sell_based_on_single_evidence`が発火し、MANUAL_REVIEW_REQUIREDへ切替 |
| 四半期値と年次値が混在した営業利益系列 | `test_mixed_period_types_are_not_evaluated`、`test_cumulative_period_is_not_evaluated` | period_type不一致・累積値混在のいずれもNone(判定不能)を返し、継続悪化ルールはNOT_EVALUATEDになる |

## 11項目の修正内容と検証

1. **格下げ時の価格nullクリア**: `SellSignalService.analyze()`でSELL/URGENT_REVIEW→REVIEW格下げ時、`immediate_execution_price`/`recommended_limit_price`/`stop_review_price`を明示的にNoneへ再構築するよう修正。整合性検査に`review_retains_immediate_execution_price`(REVIEW+即時価格残存)を追加。
2. **業種不明と一般事業会社の区別**: `IndustryClassification`(GENERAL_CORPORATE/FINANCIAL/UNKNOWN)を新設。sector欠損・空文字・未知の値はUNKNOWNとし、GENERAL_CORPORATEへフォールバックしない。日本語業種名(「金融」「銀行業」等)にも対応。
3. **キーワード検出の二段階化**: `DisclosureRiskConfirmationLevel`(RISK_KEYWORD_DETECTED/MATERIAL_EVENT_CONFIRMED)を新設。major_scandal/listing_maintenance_riskは、決算訂正等の重大事象確認語が別途検出された場合のみis_immediate_critical=True。
4. **推測の無配転落・債務超過の分離**: `official_dividend_omission_announced`/`inferred_dividend_omission`をDividendInfoに追加。balance_sheet_insolvencyもTriggerStatus.SUSPECTEDを新設し、yfinance由来の推測は major/critical件数・独立根拠グループ数に算入しない。
5. **財務期間の構造化**: `FinancialPeriodValue`(value/period_end/period_type/fiscal_year/is_cumulative/source)を新設。`detect_continuous_decline_period_aware()`は比較窓内のperiod_type不一致・累積値混在を検出しNone(判定不能)を返す。
6. **一次情報取得率の分母修正**: `primary_source_fetch_rate`の分母をTRIGGERED件数のみに変更(NOT_TRIGGERED/NOT_EVALUATEDを含めない)。
7. **決算までの日数を営業日換算**: `BusinessCalendar.business_days_between()`を使用するよう変更(土日祝日を除外)。
8. **counter_factors_evaluatedの実態化**: 増益・業績上方修正・増配・自社株買い・配当方針維持・財務余力・銀行規制資本余力・一過性要因・モメンタム・重大リスク単一性の10カテゴリーを評価し、1つでも未評価があればFalseとする(固定Trueを廃止)。
9. **債務超過のsuspected/confirmed分離**: 上記4と同一実装。
10. **独立根拠グループ数ベースの整合性検査**: `_check_sell_single_evidence`をTRIGGERED件数から`independent_evidence_group_count`ベースへ変更。ただし一次情報確認済みの即時criticalが存在する場合は単一グループでもURGENT_REVIEWを許可する例外を実装。
11. **回帰テスト**: 上記の通り、5銘柄の実データ再評価+6人工ケースをすべてユニットテストとして実装し、全512件のテスト・ruff・mypyがグリーンであることを確認済み。

## 完了条件チェックリスト

- [x] REVIEWへ格下げ後に即時執行価格が残らない(`review_retains_immediate_execution_price`検査+null化ロジックで保証)
- [x] 業種不明へ一般企業ルールを適用しない(UNKNOWN分類の新設)
- [x] キーワードだけで即時criticalにならない(二段階確認)
- [x] 推測の無配転落をSELL根拠へ数えない(SUSPECTED分離)
- [x] 財務期間の混在を検知できる(period_type不一致検出)
- [x] 一次情報率がTRIGGERED根拠のみで計算される
- [x] 決算までの日数を営業日で計算する
- [x] counter_factors_evaluatedを実態に合わせる
- [x] yfinanceだけで債務超過確定としない(SUSPECTED)
- [x] 整合性検査が独立根拠グループ数を使用する
- [x] 全512件の単体テスト、ruff、mypyが成功する

## 未実装・feasibility制約(継続)

前回レポートに記載した以下の制約は、今回のパスでも変わらず残っている(正直な申告として継続記載):

- 配当の真の普通/特別内訳(§10の`ordinary_dividend_per_share`等)は、yfinance・EDINETいずれも提供しないため未達成のまま。
- 銀行専用の健全性指標(CET1比率等)は取得できるデータソースが無いため、`regulatory_capital_breach`は常にNOT_EVALUATED。
- 真の四半期同期(前年同期)比較は、fiscal_quarterの安定した算出手段が無いため未実装。今回追加したperiod_type検証は「異なる粒度を混在させない」ことは保証するが、「同一四半期同士の比較」までは保証しない(TTM/ANNUAL変換後の連続比較に留まる)。

本番反映は、上記完了条件をすべて満たしたことを確認した上で、ユーザーの明示的な許可を得てから行う。

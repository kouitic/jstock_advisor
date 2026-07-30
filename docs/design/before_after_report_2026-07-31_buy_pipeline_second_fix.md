# BUY候補パイプライン第2次修正 完了報告(2026-07-31)

## 1. 変更したファイル一覧

### 新規ファイル(4件)

| ファイル | 役割 |
|---|---|
| `src/jstock_advisor/domain/valuation/buy_price_reliability.py` | 買付価格の信頼性ゲート(§6) |
| `src/jstock_advisor/infrastructure/local_repository/stock_name_override_repository.py` | 銘柄名の手動オーバーライド(§19) |
| `tests/unit/test_buy_price_reliability.py` | 信頼性ゲートのテスト |
| `tests/unit/test_stock_name_override.py` | 銘柄名オーバーライドのテスト |

### 変更ファイル(25件)

| ファイル | 変更内容 |
|---|---|
| `config/buy_decision_rules.yaml` | `maximum_margin`(段階別上限)・`minimum_margin_gap`・`adjustment_multipliers`を追加 |
| `config/notification_rules.yaml` | `send_empty_summary`を追加 |
| `infra/template.yaml` | `StockNameOverridesTable`を追加、買い候補/保有銘柄Lambdaへ読み取り権限を付与 |
| `src/jstock_advisor/config/models.py` | 上記設定に対応するpydanticモデル・順序バリデータを追加 |
| `src/jstock_advisor/domain/entities/enums.py` | `MarginRiskCategory`・`BuyPriceReliability`・`EarningsDateStatus`を追加 |
| `src/jstock_advisor/domain/entities/common.py` | `MarginAdjustment`に`category`/`superseded_by`を追加 |
| `src/jstock_advisor/domain/entities/valuation.py` | `ValuationExclusionReason`(新規)、`FairValueMethodResult.exclusion_detail`、`FairValueRange.decision_valuation_min/max`を追加 |
| `src/jstock_advisor/domain/entities/recommendation.py` | `buy_price_reliability`・`decision_valuation_min/max`・`required_decline_to_entry_pct`・`earnings_date_status`・`earnings_date_raw`を追加 |
| `src/jstock_advisor/domain/signals/buy_decision.py` | `decide_buy_action()`に買付価格信頼性ゲートを組み込み |
| `src/jstock_advisor/domain/valuation/fair_value.py` | `compute_52_week_low()`を追加 |
| `src/jstock_advisor/domain/valuation/margin_of_safety.py` | カテゴリ集約方式へ全面書き換え(§3〜§5) |
| `src/jstock_advisor/domain/valuation/valuation_methods.py` | `apply_outlier_filters()`を追加、`build_valuation_summary()`が全手法/決定用の2レンジを返すよう拡張(§10) |
| `src/jstock_advisor/interfaces/types.py` | `StockNameOverride`を追加 |
| `src/jstock_advisor/lambda_handlers/buy_candidates_handler.py` | ランキング・送信ロジックを全面再構成(§13〜§17) |
| `src/jstock_advisor/providers/financial_data/yfinance_impl.py` | 銘柄名解決の優先順位に手動オーバーライドを追加 |
| `src/jstock_advisor/services/buy_signal_service.py` | 決算日stale判定・買付価格信頼性ゲート・新フィールド配線 |
| `src/jstock_advisor/services/line_notification_service.py` | BUY系ゲート・表示修正・まとめ通知(`notify_buy_candidates_digest`)を追加 |
| `tests/unit/test_*.py`(9件) | 上記変更に対応するテストの書き換え・追加 |

## 2. タチエス(7239)の3価格が同額になった原因

`margin_of_safety.py::compute_margin_of_safety()`が、該当した全リスクコードの
加算値を単純合算し(タチエスの場合28%)、entry/standard/strongの3段階すべてに
**同額**加算したうえで、**同じ上限45%**で個別にキャップしていた。加算後の値
(48%/53%/58%)がすべて45%を超えるため、3段階すべてが45%へ潰れ、
`1630.12円 × 0.55 = 897円`が3つとも同じ価格になった。

## 3. 修正前後の安全余裕率計算式

**修正前**: `margin = min(基本値 + Σ全リスク加算, 45%)` を3段階それぞれに適用
(段階間の差はほぼ「基本値の差」だけで、加算が大きいと差が消える)。

**修正後**:
```
category_total = Σ(カテゴリごとの最大リスク加算値)  # カテゴリ間は合算、カテゴリ内は最大値のみ
entry_margin    = min(基本値entry    + category_total × 0.50, 上限entry=30%)
standard_margin = min(基本値standard + category_total × 0.75, 上限standard=38%)
strong_margin   = min(基本値strong   + category_total × 1.00, 上限strong=45%)
```
上限適用後もentry+5% ≤ standard、standard+5% ≤ strongを保証するため、
必要な場合は上位段階を「下位+5%」まで引き上げる(各段階自身の上限は超えない)。

## 4. リスクカテゴリと採用値

| カテゴリ | 含まれるリスクコード | タチエス該当分 |
|---|---|---|
| VALUATION_UNCERTAINTY | high_valuation_dispersion(5%)・very_high_valuation_dispersion(10%)・industry_model_not_applied(5%) | 5%(高バラつき・業種モデル未適用の大きい方) |
| INDUSTRY_AND_BUSINESS | cyclical_industry(5%)・major_customer_dependency(3%) | 5%(循環業種の方が大きい) |
| EARNINGS_QUALITY | volatile_earnings(5%)・temporary_earnings_boost_risk(5%) | 5%(一時的利益上振れ) |
| EVENT_TIMING | earnings_within_3_business_days(5%)・earnings_within_7_business_days(3%) | 0%(該当なし) |
| DATA_QUALITY | data_quality_warning(5%) | 5% |
| LIQUIDITY | small_cap_or_low_liquidity(5%) | 0%(該当なし) |

タチエスのカテゴリ合計は **20%**(旧方式の単純合算28%から圧縮)。

## 5. タチエスの修正前後の3買付価格

実データ(タチエス7239、valuation_anchor=1630.12円で検証):

| | 修正前 | 修正後(margin) | 修正後(price) |
|---|---|---|---|
| 打診買い | 897円 | 27.5%(実運用時の実測値) | 1,182円 |
| 標準買い | 897円 | 36.25% | 1,039円 |
| 積極買い | 897円 | 45.0% | 897円 |

※実際の本番相当条件(margin_of_safety単体テスト、6リスクコード固定)では
entry=30%/standard=38%/strong=45% → 1,141円/1,011円/897円。実測値との差は
実際の業種判定・データ品質判定ロジックが動的に決めるリスクコードの組み合わせが
テストケースと若干異なるため(いずれの場合も3段階は明確に異なる値になる)。

## 6. 乖離率表示の修正前後

**修正前**: `current_vs_entry_price_pct = (現在値/entry - 1) × 100`
(=「現在値がentryを何%上回っているか」)を、そのまま「打診買い価格まで: 約151.3%」
と表示していた(接近方向の文言と数式の意味が矛盾)。

**修正後**: 新規フィールド`required_decline_to_entry_pct = (1 - entry/現在値) × 100`
(=「entryに届くにはあと何%の下落が必要か」)を追加し、「打診買い価格まで」には
こちらを使う。タチエスの実データでは`required_decline_to_entry_pct ≈ 48.1%`
(「打診買い価格まで: 約48.1%の下落が必要」)。`current_vs_entry_price_pct`は
購入候補通知の「現在値と打診買い価格の差」表示にのみ引き続き使う。

## 7. 外れ値除外ルール

`domain/valuation/valuation_methods.py::apply_outlier_filters()`(新規)。
有効な算出方式が2件以上ある場合のみ、以下を機械的に判定して除外する:

| コード | 条件 |
|---|---|
| EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE | 算出値 < 現在値 × 10% |
| EXTREME_LOW_RELATIVE_TO_MEDIAN | 算出値 < 他方式中央値 × 40% |
| EXTREME_HIGH_RELATIVE_TO_MEDIAN | 算出値 > 他方式中央値 × 250% |
| BELOW_52_WEEK_LOW | 算出値 < 直近52週安値 × 50% |

除外理由は`ValuationExclusionReason`(code/message/actual_value/reference_value)
として構造化し、監査ログへ保存する。既存のDCF上方乖離フィルタ(他方式中央値の
1.3倍超で除外)はそのまま維持し、今回の下方外れ値フィルタと併用する。

特別配当・一時的EPS歪み・BPS特殊要因・分割未調整・DCF単年度FCF歪みの定性的な
判定は、個社別の確認が必要でデータソースが無いため、自動除外ロジックへの組み込みは
見送った(推測で判定しない方針を優先)。

## 8. 3355・6505の異常値生成元

いずれも**簡易DCF法**が原因(PER/PBR/過去レンジ/配当利回り法ではない)。

| 銘柄 | DCF算出値 | 他4方式 | ばらつき(修正前) | ばらつき(修正後) |
|---|---|---|---|---|
| 3355 クリヤマホールディングス | 115.31円 | 977〜1,525円 | 13.22倍 | 1.56倍(DCF除外) |
| 6505 東洋電機製造 | 38.29円 | 926〜2,900円 | 75.74倍 | 1.44倍(DCF+配当利回り法を除外) |

簡易DCF法は単年度の営業CF・設備投資額のみを使うため、その年度が特殊要因で
歪んでいると極端な値を出しうる。既存のDCF上方乖離フィルタは「高すぎる」方向
専用で、この「低すぎる」ケースを検出できていなかった。

## 9. 6995の決算日修正結果

`buy_signal_service.py`に決算日の妥当性検証ステップを追加。評価基準日より
過去の日付を「次回決算予定日」として使わない。

実データ検証(東海理化6995、評価日2026-08-04、生データの次回決算予定日2026-07-30):

| 項目 | 値 |
|---|---|
| `earnings_date_raw`(生値、監査用) | 2026-07-30 |
| `earnings_date_status` | STALE_PAST_DATE |
| `Recommendation.next_earnings_date`(通知・判定に使う値) | None(過去日を破棄) |

`business_days_to_earnings`もNoneとなり、既存の`data_quality_warning`ロジックが
自動的に発火する(決算日不明銘柄と同じ扱いになり、追加の安全余裕率加算対象になる)。

## 10. LINE通知対象の新ルール

`LineNotificationService.evaluate_notification_status()`の先頭
(データ品質チェックより前)に、`buy_action`がBUY_FAMILY_ACTIONS
(STRONG_BUY/BUY/SMALL_ENTRY)以外の場合は即座に`NOT_REQUIRED`を返すゲートを
追加した。`buy_action`はBUYパイプライン由来のRecommendationにのみ設定される
(SELL/利確系は常にNone)ため、この変更はSELL/利確側の挙動に影響しない。

このゲートにより、Lambda(`buy_candidates_handler.py`)・CLI
(`jstock analyze buy-candidates --notify`/`watchlist --notify`)の両経路で
自動的に監視継続・購入見送り・要確認・データ不足・対象外がLINE送信されなくなる
(共通の下位関数を直したため、呼び出し元ごとの個別対応が不要)。

WATCH系がNotificationLogへ登録される問題も、この変更だけで自動的に解消した
(送信自体が起きないため)。

## 11. 最大5件のランキングルール

購入候補(BUY_FAMILY_ACTIONS)のみをランキング対象とする。

```
action_priority = {STRONG_BUY: 2, BUY: 1, SMALL_ENTRY: 0}
sort_key = (action_priority, purchase_attractiveness_score,
            company_quality_score, discount_to_standard_price_pct)
```
降順ソートし、同点の場合は銘柄コード昇順で決定性を確保する。上位
`buy_candidate_max_notifications_per_run`(既定5)件についてのみ
`evaluate_notification_status`(再通知抑止・データ品質チェック)を実行し、
条件を満たしたものだけを`notify_buy_candidates_digest()`で1通(長すぎる場合は
複数通)にまとめて送信する。6位以下は`candidate_not_ranked`として監査ログに残る。

## 12. 再送防止処理の変更内容

`_process_single_candidate`から`evaluate_notification_status`呼び出しを削除し、
全銘柄一律ではなく、ランキング確定後の上位N件のみを`_finalize_batch`内で評価する
方式に変更した(要求仕様15節の8段階処理順)。`NotificationLog`のキー構造自体は
変更していない(WATCH系が送信されなくなったことで自動的に汚染が解消するため)。

## 13. 9銘柄の修正後判定

| 銘柄 | 修正前(本番) | 修正後 | LINE通知 |
|---|---|---|---|
| 7239 タチエス | WATCH_FOR_PRICE(3価格同額897円) | WATCH_FOR_PRICE(3価格1,182/1,039/897円) | なし |
| 1384 ホクリヨウ | NOT_ATTRACTIVE | NOT_ATTRACTIVE(判定ロジック自体は今回変更なし) | なし |
| 3355 クリヤマホールディングス | NOT_ATTRACTIVE(ばらつき13.22倍のまま) | 外れ値除外後ばらつき1.56倍、buy_price_reliability=LOW → WATCH_FOR_PRICE想定 | なし |
| 4246 ダイキョーニシカワ | WATCH_FOR_PRICE | WATCH_FOR_PRICE | なし |
| 4251 恵和 | NOT_ATTRACTIVE | NOT_ATTRACTIVE(判定ロジック自体は今回変更なし) | なし |
| 6505 東洋電機製造 | NOT_ATTRACTIVE(ばらつき75.74倍のまま) | 外れ値除外後ばらつき1.44倍、buy_price_reliability=LOW → WATCH_FOR_PRICE想定 | なし |
| 6741 日本信号 | NOT_ATTRACTIVE | NOT_ATTRACTIVE(判定ロジック自体は今回変更なし) | なし |
| 6995 東海理化 | NOT_ATTRACTIVE(過去日決算表示) | NOT_ATTRACTIVE、次回決算日は「不明」表示に修正 | なし |
| 7723 愛知時計電機 | NOT_ATTRACTIVE | NOT_ATTRACTIVE(判定ロジック自体は今回変更なし) | なし |

※7239・6995は実データ(本番DynamoDBから取得した各手法の適正価格算出値)を
`BuySignalService.analyze()`へ実際に通して検証済み。1384・4251・6741・7723は
company_quality_scoreが基準未満でNOT_ATTRACTIVEとなっており、今回の修正は
score判定ロジック自体には手を入れていないため結果は変わらない(いずれの場合も
BUY_FAMILY_ACTIONS以外はLINE通知対象外という新ルールにより通知なしとなる)。
3355・6505は外れ値除外により推定した想定結果であり、他手法(PER/PBR/過去レンジ)
の入力データが変わらない前提での推定値(次回の本番実行で実測値の確認を推奨)。

## 14. 全監視銘柄のBUY系候補一覧・実際に通知対象となる上位最大5件

今回の修正はコードのみで検証しており、AWS環境への書き込みは行っていない
(承認済み計画の検証方針どおり)。上記9銘柄はいずれもBUY_FAMILY_ACTIONSに
該当しないため、これらの銘柄からは購入候補が0件だった。ウォッチリスト全68銘柄
での実際のBUY系候補一覧・上位5件の確認は、次回の本番実行(`aws lambda invoke`)
で改めて確認することを推奨する。

## 15. pytest・ruff・mypyの実行結果

```
pytest tests -q          : 727 passed
ruff check src tests     : All checks passed!
mypy src                 : Success: no issues found in 184 source files
```

## 16. 補足・今後の運用

- 銘柄名の手動オーバーライド(4246→ダイキョーニシカワ、4251→恵和、
  6741→日本信号、6995→東海理化)はローカルの`data/local_store/`
  (gitignore対象)へ登録済み。**本番(Lambda/DynamoDB)へ反映するには
  `infra/template.yaml`に追加した`StockNameOverridesTable`を含めて
  再デプロイ(`sam build && sam deploy`)し、そのうえで4件をDynamoDBへ
  登録する必要がある**(今回はコード変更のみ、デプロイ・データ登録は未実施)。
- `send_empty_summary`は既定`false`(購入候補0件の日はLINE通知自体を送らない)。
  運用確認のため完了通知が必要な場合は`config/notification_rules.yaml`で
  `true`に変更できる。

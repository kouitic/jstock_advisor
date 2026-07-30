# 買い候補判定パイプライン再設計 完了報告(2026-07-31)

## 1. 発端

本番相当の評価で、買い候補ランキング上位5銘柄のうち4銘柄(タチエス・ダイキョーニシカワ・
ホクリヨウ・愛知時計電機)は現在値が適正価格を19.7%〜54.3%も上回っているにもかかわらず、
唯一適正価格を下回っていた日本新薬と同列の「優先順位の高い銘柄」として通知されていた。

根本原因は3点の複合:

1. `buy_signal_service.py`が手法間バラつきを含む`fair_value_range`を読まず、単一中央値
   `fair_value`のみを使っていた。
2. `buy_price.py`が適正価格に対して95%/90%/85%の固定比率を掛けるだけで、信頼度やリスクに
   応じた調整が一切なかった。
3. `buy_candidates_handler.py`が`total_score`(企業魅力度スコア)のみでランキングしており、
   現在値と買付価格の位置関係を見ていなかった。

ユーザーの指示に基づき、「企業として投資候補になり得ること」(company_quality_score)と
「現在価格で購入すべきこと」(BuyAction)を構造的に分離する3段階パイプラインへ全面再設計した。

## 2. 変更ファイル一覧

### 新規ファイル(19件、計2,234行)

| ファイル | 役割 |
|---|---|
| `config/buy_decision_rules.yaml` | スコア閾値・バラつき判定・決算窓・安全余裕率・割安度カテゴリ上限 |
| `src/jstock_advisor/domain/classification/buy_industry.py` | 9業種分類、専用モデル未実装の正直な記録 |
| `src/jstock_advisor/domain/entities/buy_decision.py` | `BuyDecisionReason`(pydantic) |
| `src/jstock_advisor/domain/scoring/undervaluation_categories.py` | カテゴリ別上限点の割安度スコア |
| `src/jstock_advisor/domain/signals/buy_consistency.py` | BUY推奨の整合性検証(6チェック) |
| `src/jstock_advisor/domain/signals/buy_decision.py` | 価格3段階判定・スコア格下げ・決算調整の中核 |
| `src/jstock_advisor/domain/signals/eps_normalization.py` | 循環業種向けEPS平準化 |
| `src/jstock_advisor/domain/valuation/buy_price_levels.py` | 3段階買付価格算出 |
| `src/jstock_advisor/domain/valuation/margin_of_safety.py` | 動的安全余裕率算出 |
| `src/jstock_advisor/domain/valuation/valuation_confidence.py` | 適正価格信頼度決定 |
| `src/jstock_advisor/domain/valuation/valuation_methods.py` | 手法集計・バラつき判定・valuation_anchor算出 |
| `tests/unit/test_buy_consistency.py` 他7ファイル | 上記の単体テスト |
| `tests/unit/test_buy_signal_service.py` | サービス層直接テスト+5銘柄回帰テスト(§21相当) |

### 変更ファイル(19件、+1,777/-528行)

主な変更点のみ抜粋(全件は`git diff --stat`参照):

- `domain/entities/enums.py`: `BuyAction`(9値)・`BuyIndustrySector`追加
- `domain/entities/common.py`: `BuyPriceLevels`をtentative/aggressive→entry/standard/strong構造へ(legacy remap付き)
- `domain/entities/valuation.py`: `FairValueMethodResult`/`FairValueRange`に統計フィールド追加
- `domain/entities/recommendation.py`: BuyAction・スコア2種・valuation情報等を追加、`recommended`をpropertyへ変更
- `domain/scoring/score.py`: 割安度スコアをカテゴリ上限方式へ委譲
- `domain/signals/buy_signal.py`: 判定ロジックを`buy_decision.py`へ移し、純粋なデータ変換関数のみ残す
- `services/buy_signal_service.py`: `analyze()`を22ステップの3段階パイプラインへ全面書き換え(+580/-大幅改稿)
- `lambda_handlers/buy_candidates_handler.py`: 購入候補/価格待ちの2本立てランキングへ
- `services/line_notification_service.py`: BuyAction別の通知文言、「該当なし」の明示描画
- `services/recommendation_consistency_validator.py`: `buy_consistency.py`呼び出しを追加
- `config/models.py`: `BuyDecisionRulesConfig`他を追加、`RecommendedBuyPrice`(固定比率)を削除
- `domain/valuation/buy_price.py`: **削除**(固定95/90/85%方式は完全廃止)

## 3. 処理フロー(Before/After)

**Before**: スクリーニング → スコア算出 → `fair_value`中央値×固定比率で3価格生成 →
`total_score`でランキング → 上位N件を無条件通知

**After**(22ステップ):

```
1. データ品質検証
2. 投資対象スクリーニング(第1段階) → 不合格ならEXCLUDED
3. 業種分類(9区分、専用モデルは全区分で未実装)
4. 利益/EPS平準化(循環業種のみ)
5. 各方式の適正価格算出(target_yield/per/pbr/historical_range/dcf/industry)
6. 不適用方式・DCF上方乖離の除外
7. 手法間バラつき判定(LOW/MEDIUM/HIGH、閾値1.30/1.60)
8. valuation_anchor算出(信頼度・バラつきに応じてweighted_median/trimmed_mean/percentile_40)
9. 適正価格信頼度決定(業種モデル未適用のためHIGH到達不可、上限MEDIUM)
10. 必要安全余裕率算出(基本値+最大11種のリスク加算、上限45%)
11. 3段階買付価格算出(entry/standard/strong)
12. company_quality_score算出(第2段階: 企業魅力度、カテゴリ上限付き割安度スコア含む)
13. purchase_attractiveness_score算出(第3段階: 現在価格での魅力度、ランキング専用)
14. 現在価格によるBuyAction仮判定(価格条件が必須、満たさなければ即WATCH_FOR_PRICE)
15. スコアによる格下げ(格下げのみ、昇格なし)
16. 決算直前調整(3営業日以内→WATCH_BEFORE_EARNINGS、BUY系のときのみ)
17. バラつき過大時のMANUAL_REVIEW強制(2.00超、BUY系のときのみ)
18. 整合性検証(6チェック、違反時MANUAL_REVIEW)
19-20. ランキング区分確定(buy_candidate / watch_price / excluded)
21. 通知生成(BuyAction別フォーマット)
22. 監査ログ保存(通知されなかった銘柄も含め全件)
```

## 4. BuyAction判定ルール表

| BuyAction | 発生条件 | ランキング区分 |
|---|---|---|
| STRONG_BUY | 現在値 ≤ strong価格 かつ company_quality_score ≥ 70 | buy_candidate |
| BUY | 現在値 ≤ standard価格 かつ score ≥ 60 | buy_candidate |
| SMALL_ENTRY | 現在値 ≤ entry価格 かつ score ≥ 55 | buy_candidate |
| WATCH_FOR_PRICE | 価格条件を満たさない、またはentry未算出(信頼度LOW) | watch_price |
| WATCH_BEFORE_EARNINGS | 価格条件は満たすが次回決算まで3営業日以内 | watch_price |
| MANUAL_REVIEW | バラつき率>2.00でBUY系判定になった、または整合性検証違反 | (除外) |
| NOT_ATTRACTIVE | company_quality_score < 45(価格条件によらず無条件) | (除外) |
| EXCLUDED | 第1段階スクリーニング不合格 | excluded |
| DATA_INSUFFICIENT | スナップショット取得失敗 | (除外) |

**核心原則**: 価格条件(現在値 vs 3段階買付価格)は購入候補になるための必須条件であり、
スコアは格下げにのみ使う。現在値が打診買い価格(entry)を上回っている銘柄は、
company_quality_scoreがどれだけ高くてもBUY系判定にならない。

## 5. 適正価格の集計方法(valuation_anchor)

複数手法(配当利回り法・PER法・PBR法・過去レンジ法・簡易DCF法・業種別モデル[未実装])の
結果から、バラつき率(`valuation_max / valuation_min`)と信頼度に応じて保守的に決定する:

- バラつき率 > 1.60(HIGH): 全手法値の40パーセンタイル
- 信頼度MEDIUM、またはバラつき率 1.30〜1.60(MEDIUM): `min(加重中央値, トリム平均)`
- 信頼度HIGHかつバラつき率 ≤ 1.30(LOW): 加重中央値
- 信頼度LOW(手法不足・バラつき過大等): valuation_anchor自体をNoneにし、自動買付価格を生成しない

業種別モデルは9業種すべてで未実装のため`industry_model_applied`は構造的に常にFalseであり、
適正価格信頼度はHIGHに到達できず上限MEDIUMに固定される(推測で埋めない方針)。

## 6. 買付価格・安全余裕率の算出式

```
買付価格(entry/standard/strong) = valuation_anchor × (1 - 必要安全余裕率)
必要安全余裕率 = 信頼度別基本値 + Σ該当するリスク加算(最大45%で頭打ち)
```

基本値(信頼度別):

| 信頼度 | entry | standard | strong |
|---|---|---|---|
| HIGH(現状到達不可) | 10% | 15% | 20% |
| MEDIUM | 20% | 25% | 30% |

リスク加算(該当分をすべて合算): 決算3営業日以内+5%/決算7営業日以内+3%/バラつき中+5%/
バラつき大+10%/業種モデル未適用+5%/循環業種+5%/小型・低流動性+5%/業績不安定+5%/
一時的業績押上げリスク+5%/主要顧客依存(自動車部品業種)+3%/データ鮮度不安+5%

## 7. 主な設計判断の記録

- **`compute_confidence()`(既存の汎用信頼度エンジン)を使わなかった**: BUYパイプライン用に
  正直に流用しようとすると、未取得の約15項目のシグナルを推測で埋める必要があり、
  「推測で補完しない」という本プロジェクトの一貫した方針に反する。代わりに§9専用ルールを
  実装した新規`determine_valuation_confidence()`を使用(この点は当初計画からの意図的な逸脱)。
- **`recommended: bool`は保存フィールドではなく`@property`化**: `buy_action in BUY_FAMILY_ACTIONS`
  から導出する派生値とし、直接更新できる設計を構造的に排除。
- **`RecommendationType.WATCH_BEFORE_EARNINGS`との衝突を回避**: 同値は利確判定エンジンの
  WATCH抑制表示に既に使われているため、BUYパイプラインは`recommendation_type`を書き換えず、
  `buy_action`のみで決算待ち表示を判別するよう`line_notification_service.py`を実装。

## 8. 5銘柄比較表(本番相当データによる回帰テスト結果)

`tests/unit/test_buy_signal_service.py`にて、実際に報告された現在値・予想配当
(配当利回りから逆算)を用いて検証。適正価格の算出方法自体が新設計に置き換わっているため、
算出される適正価格・買付価格は旧システムの報告値とは一致しない(仕様どおりの挙動)。

| 銘柄 | 現在値 | valuation_anchor | entry価格 | company_quality_score | 業種分類 | 最終BuyAction | ランキング区分 |
|---|---|---|---|---|---|---|---|
| 4516 日本新薬 | 3,495円 | 5,500円 | 3,575円 | 60.74 | PHARMACEUTICAL | WATCH_BEFORE_EARNINGS(価格条件は充足、決算直前のため待機) | watch_price |
| 7239 タチエス | 2,277円 | 1,903円 | 1,180円 | 58.76 | AUTOMOTIVE_PARTS | WATCH_FOR_PRICE(現在値が適正価格を19.7%上回る) | watch_price |
| 4246 ダイキョーニシカワ | 1,027円 | 算出不可(信頼度LOW) | — | 63.54 | AUTOMOTIVE_PARTS | WATCH_FOR_PRICE(手法間バラつき過大により自動買付価格を生成せず) | watch_price |
| 1384 ホクリヨウ | 2,035円 | 1,612円 | 1,048円 | 49.81 | FOOD | WATCH_FOR_PRICE(現在値が適正価格を26.2%上回る) | watch_price |
| 7723 愛知時計電機 | 3,025円 | 1,961円 | 1,373円 | 50.09 | GENERAL_MANUFACTURING | WATCH_FOR_PRICE(現在値が適正価格を54.3%上回る、乖離最大) | watch_price |

**結論**: 5銘柄中、価格条件(現在値 ≤ entry価格)を満たすのは日本新薬のみであり、これは
`raw_buy_action`(決算調整前の価格条件のみによる仮判定)が`SMALL_ENTRY`であることで確認済み。
最終的にWATCH_BEFORE_EARNINGSへ格下げされたのは決算直前ルール(次回決算まで営業日2日)が
理由であり、価格条件の不足が理由ではない。残り4銘柄はすべて価格条件を満たさず
`buy_candidate`ランキングから除外される。これにより、当初の不具合(4銘柄が過大評価状態で
「優先銘柄」として通知される)は構造的に再発しなくなった。

## 9. 購入候補ランキング・価格待ちランキング(5銘柄評価時点)

- **購入候補ランキング(buy_candidate)**: 該当なし(5銘柄中いずれも価格条件を満たしBUY系のまま
  確定したものはない。日本新薬は決算直前ルールでwatch_priceへ移動)
- **価格待ちランキング(watch_price)**: 日本新薬(決算待ち)、タチエス、ダイキョーニシカワ、
  ホクリヨウ、愛知時計電機(価格条件不足、company_quality_score降順)

「購入候補が0件の場合は正しく『購入候補なし』と通知する」というユーザーの要求どおり、
`line_notification_service.py`のバッチサマリーは購入候補0件時に「該当なし」を明示する。

## 10. 全銘柄(ウォッチリスト)への実データ再適用について

計画時点の検証方針(`docs/functional_spec.md`変更履歴および本タスクの承認済みプランに明記)
どおり、今回の検証はAWS環境への書き込み・実データ再取得を伴わず、テストのみで行った。
上記5銘柄は実際に本番相当の評価で問題を引き起こした銘柄そのものであり、新パイプラインが
これらに対して意図どおりに動作することを確認済みである。23銘柄全件の実データによる
再評価には、各銘柄の予想EPS/BPS・四半期営業利益等の実データをProvider経由で再取得する
追加作業が必要となるため、別タスクとして切り出すことを推奨する。

## 11. テスト結果

```
pytest tests -q          : 682 passed
ruff check src tests     : All checks passed!
mypy src                 : Success: no issues found in 182 source files
```

追加・変更したテストファイル: `test_buy_consistency.py`, `test_buy_decision.py`,
`test_buy_industry.py`, `test_buy_signal_service.py`(新規、5銘柄回帰テスト含む),
`test_eps_normalization.py`, `test_margin_of_safety.py`, `test_undervaluation_categories.py`,
`test_valuation_methods.py`(以上新規8ファイル)、
`test_buy_candidates_handler.py`, `test_buy_signal.py`, `test_config_loader.py`,
`test_fair_value.py`, `test_line_notification_service.py`,
`test_recommendation_consistency_validator.py`, `test_audit_service.py`, `test_score.py`
(以上、既存8ファイルを新設計に合わせて更新)。

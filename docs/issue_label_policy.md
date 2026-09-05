# Issue Label 運用ポリシー

## 0. この文書の位置づけ

本文書は、このリポジトリにおける **GitHub Issue の label 運用の正本
(Single Source of Truth)** である。

- Issue の labels は色分けではなく、**開発キュー・品質管理・リリース判断に使う
  正式なメタデータ**として扱う。
- 本ポリシーは**特定の AI・特定の担当者に依存しない Repository Development Policy**
  である。人間・AI エージェントのいずれが、どの製品・どのモデルで作業する場合も、
  同じ定義・同じ手順を適用する。
- 各エージェントの入口ファイル(`CLAUDE.md`、`AGENTS.md` など、エージェントが
  起動時に読み込む規約ファイル)は本文書へ到達するための参照にすぎず、
  **ルール本体を保持しない**。ルールが変わったときに更新するのは本文書であり、
  入口ファイルではない。新しいエージェントを導入する場合は、その入口ファイルから
  本文書を1行参照するだけでよい。
- 会話上の合意や個々のエージェントのメモリは正本ではない。
  それらと本文書が食い違う場合は、本文書を正とし、必要なら本文書を更新する。

---

## 1. 5軸モデル

Issue には次の5つの軸がある。

| 軸 | 問い | ラベル |
|---|---|---|
| **Issue Type** | 何の Issue か | `bug` / `design-defect` / `enhancement` / `investigation` / `calibration` / `tracking` / `not-a-bug` / `accepted-risk` |
| **Priority** | いつ対応するか(投資運用への影響による対応順序) | `priority:P0` / `priority:P1` / `priority:P2` / `priority:P3` |
| **Severity** | 問題が発生した場合の影響度 | `severity:SEV-1` / `severity:SEV-2` / `severity:SEV-3` / `severity:SEV-4` |
| **Release Blocker** | Production release を止めるか | `release-blocker` |
| **Progress Status** | 開発ライフサイクル上どこまで進んだか | `status:未着手` / `status:調査・設計中` / `status:設計済` / `status:開発中` / `status:開発済` / `status:マージ済` / `status:デプロイ済` / `status:本番検証済` |

この5軸とは別に、**判定軸ではない補助 metadata** として waiting label がある
(`waiting:本番検証` / `waiting:人間判断` / `waiting:外部条件`、§8)。

---

## 2. 5軸は独立して判定する

**この5軸を相互に自動推論してはならない。** それぞれ独立した根拠で判断する。

```
P0              ≠  release-blocker
SEV-1           ≠  P0
release-blocker ≠  SEV-1
bug             ≠  必ず release-blocker
```

具体的には、次のような推論をしない。

- 「P0 だから次回 release を止める」— Priority は対応順序であり、release 可否ではない。
- 「SEV-1 だから P0」— 影響度が大きくても、対応順序が最優先とは限らない。
- 「release-blocker だから Severity を引き上げる」— block 条件と影響度は別物。
- 「bug だから release-blocker」— 多くの bug は release を止めない。
- 「P0 だから status:開発中」— Priority は対応順序であり、進捗ではない。
- 「status:デプロイ済 だから release-blocker を解除してよい」— 進捗と block 条件は別物。

---

## 3. Issue Type

Issue には原則として Type を **1つだけ**設定する。

### `bug`

既存の仕様・契約・期待動作に対して、**実装が実際に誤動作していることが確認された** Issue。

- 正しいデータなのに誤判定する
- fail-close すべき経路が fail-open する
- `None` を `0` として扱い誤った結果を出す
- 本来到達すべき安全機構が機能しない

### `design-defect`

コードが**現在の設計どおり動作していても、その設計自体に問題があり**、
機能劣化を生む Issue。

- 機能が成立しない
- 過度に安全側へ倒れて実用不能になる
- 本来の目的を達成できない

「安全性 bug ではないが設計上問題」という場合はこちら。
**`bug` と `design-defect` は原則として同時に付けない。**

### `enhancement`

既存の誤動作修正ではなく、新機能・テスト強化・可観測性向上・開発基盤改善・
新しい仕組みの追加などを目的とする Issue。

### `investigation`

まだ欠陥と確定しておらず、`bug` / `design-defect` / `accepted-risk` / `not-a-bug`
などの結論を出すための調査 Issue。

**調査中の仮説だけを理由に `bug` を付けない。**

### `calibration`

実績データ蓄積後に、閾値・重み・パラメータ・判定水準などを再評価する Issue。
**現在の実装が欠陥であるという意味ではない。**

### `tracking`

親 Issue・監査一覧・追跡表。それ自体を直接修正することが主目的ではなく、
複数の Issue / findings の状態を追跡するもの。

### `not-a-bug`

調査の結果、問題ではないことが確定した Issue。
通常 `state=CLOSED` / `stateReason=NOT_PLANNED` と組み合わせる。
**`bug` とは同時に付けない。**

### `accepted-risk`

問題・リスク自体は存在するが、理由を明示したうえで対応しないと判断したもの。
「昔からそうだから」「たぶん問題ないから」だけでは `accepted-risk` にしない。
**受容理由と残余リスクを Issue へ記録する。**

### 排他関係のまとめ

| 組み合わせ | 可否 |
|---|---|
| `bug` + `design-defect` | 不可 |
| `bug` + `not-a-bug` | 不可 |
| `calibration` + `bug` | 不可(機械的に組み合わせない) |
| Type が2つ以上 | 原則不可 |
| Type が0個 | 不可(OPEN Issue には必ず Type を付ける) |

`calibration` の再評価の結果、現在の閾値自体が仕様違反・欠陥と確定した場合は、
別途 `bug` 化するか Type を再分類する。

---

## 4. Priority

Priority は **対応順序・優先順位**を表す。何を根拠に順序を決めるかは
「**ユーザーの投資運用に対して、その Issue をどの順番で直すべきか**」である。

判定は **投資機能への影響(functional)** と **非機能リスク(non-functional)** を
独立に評価し、高い方を採用する。

```
ISSUE_PRIORITY = MAX(FUNCTIONAL_PRIORITY, NON_FUNCTIONAL_PRIORITY)
                 （P0 > P1 > P2 > P3）
```

| ラベル | 定義 |
|---|---|
| `priority:P0` | 事業継続を脅かす(動かない・データが壊れる・重大な security / privacy 事故・急激な cost runaway 等) |
| `priority:P1` | 投資成果または重要な非機能品質を直接損なう |
| `priority:P2` | 直接的な投資成果・事業継続には影響しないが、補助機能または非機能品質が低下する |
| `priority:P3` | 現在の Production 品質に実害がなく、主に開発・運用・将来改善 |

投資機能だけに着目した短縮表現(入口用)。

```
P0  動かない・データが壊れる
P1  動くが投資判断が狂う
P2  投資判断は概ね正しいが補助機能が狂う
P3  投資機能は正しく、開発・運用を改善する
```

短縮表現は理解の入口であり、**非機能リスクを落とさないこと**。
非機能の判定基準は §4.13〜§4.21 が正本である。

```
PRIORITY_POLICY_VERSION = PRIORITY_POLICY_V2_NFR
```

既存の独立性は維持する。

- **P0 だからといって次回 Production release を必ず止めるわけではない。**
  release 可否は `release-blocker` で別途判断する。
- **Severity が高い = Priority が高い、とは限らない。**
- **Progress Status(§7)は進捗であり、Priority ではない。**

通常の実装 Issue では **Priority を設定することを基本とする**
(設定しない場合の扱いは §10 を参照)。

### 4.1 `priority:P0`(functional) — System continuity / Data integrity

正常な投資判断処理そのものを実行・継続できなくする、または Production の
永続データを破損・消失させる問題。

```
判定質問
  この Issue を放置すると、Jstock Adviser が正常に投資判断処理を継続できないか、
  または Production データが壊れるか？
```

代表例。

```
EventBridge 等から主要処理が起動しない
BUY / holdings 等の主要バッチが広範囲に完走不能
通常入力で主要 Lambda が恒常的に異常終了
Production データの破損・消失 / Production state の不可逆な不正更新
破損データにより次回以降も処理不能
必須認証・権限等により主要機能が利用不能
1 件の通常データで主要 collection 全体が読めなくなる等、
  normal Production 経路で広範囲停止する
```

**潜在的な型上の可能性だけで P0 にしない。** `PRODUCTION_REACHABILITY`(§4.6)を
必ず確認する。例えば「戻り型は `None` を許すが、current implementation では
`None` を返す経路が存在しない」なら、それだけでは P0 にしない。

### 4.2 `priority:P1`(functional) — Direct investment return impact

システム自体は正常終了するが、ユーザーが受け取る投資判断・価格・重要通知を
誤らせ、投資リターンを低下させ得る問題。

```
判定質問
  処理は正常終了するが、この Issue のためにユーザーが違う売買行動を
  取る可能性があるか？
```

代表例。

```
本来 BUY すべき銘柄を候補から落とす / BUY すべきでない銘柄を BUY とする
SELL / PARTIAL / FULL / HOLD / WATCH 等を誤判定
買値・売値・利確価格等を誤る
本来届くべき重要な売買通知が届かない
本来発生すべきでない売買通知が発生する
通知本文の Action / 価格 / 投資判断材料が誤る
stale / incorrect data を normal Production 経路で使い投資判断を変え得る
score defect により、ユーザーが参照する Action / category を有意に変える
投資スタイルを構造的に過小評価し、BUY 機会損失を継続的に起こし得る
```

```
ACTUAL_FINANCIAL_LOSS_REQUIRED_FOR_P1 = NO
```

実損を Production で観測する必要はない。code flow / Production read-only evidence /
historical comparison / shadow calculation / Action delta / notification delta の
いずれかで合理的に確認できればよい。

**誤投資判断を Production で意図的に発生させる failure injection は禁止。**

### 4.3 `priority:P2`(functional) — Supporting investment function quality

直接の BUY / SELL 判断・重要通知には原則影響しないが、投資支援の補助機能・
分析・監視・説明・振り返り等を低下させる問題。

```
判定質問
  これを直さなくても通常の BUY / SELL 判断は基本的に変わらないが、
  分析・監視・振り返り・説明等の品質が低下するか？
```

代表例。

```
定点評価の集計不具合 / weekly improvement review の不具合
calibration / backtest / retrospective metrics の品質低下
watchlist 追加・削除・cooldown 等の不整合
監査情報・説明情報の欠落
同じ正しい通知の単純な重複 / 表示件数と実送信件数の乖離
Action・価格は正しいが理由説明だけ不正確
VALIDATION / manual-only path の不具合
通常 scheduler では到達しない運用上の問題
```

**subsystem 名だけで P2 にしない。** 例えば watchlist の bug でも、
「有望銘柄が監視対象にならない → BUY 通知が継続的に届かない」まで因果が
確認できるなら P1 である。

### 4.4 `priority:P3`(functional) — Development / Operations

現在の Production 投資判断には実質的影響がなく、主に開発・テスト・保守・
運用効率・将来リスクを改善するもの。

```
判定質問
  現在の Production 投資機能は正しく、主に作る側・運用する側を改善する問題か？
```

代表例。

```
test-only flaky / test determinism / CI 改善 / テストコード品質
refactoring / docs 改善 / 開発ガバナンス / 開発者向け可観測性
現在未使用経路の latent defect / 将来仕様変更時のみ顕在化するもの
運用手順改善 / コード可読性・保守性
```

### 4.5 subsystem-based priority の禁止

```
SUBSYSTEM_BASED_PRIORITY_FORBIDDEN = YES
```

次のような決め方をしてはならない。

```
notification だから P1
watchlist だから P2
test だから P3
```

必ず次の順で因果を追う。

```
ROOT_CAUSE
  -> PRODUCTION_REACHABILITY
  -> DOWNSTREAM_EFFECT
  -> USER_INVESTMENT_EFFECT
```

### 4.6 Production reachability

```
PRODUCTION_REACHABILITY_REQUIRED = YES
```

Priority 判定時に、最低でも次のいずれかへ分類する。

| 値 | 意味 |
|---|---|
| `NORMAL_RECURRING` | 通常の定期実行経路で繰り返し到達する |
| `MANUAL_ONLY` | 手動起動・VALIDATION 等でのみ到達する |
| `CONDITIONAL` | 特定条件が揃ったときのみ到達する |
| `LATENT` | 現 implementation では到達経路が存在しない |
| `NOT_REACHABLE` | 構造上到達しない |

normal recurring へ到達しない問題は通常 Priority が低くなるが、
**「機械的に必ず1段下げる」ルールにはしない。** 到達したときの影響と合わせて判断する。

### 4.7 通知の扱い

```
重要通知の欠落                    -> P1 候補
誤 Action / 誤価格 / 誤売買内容    -> P1 候補
誤った売買通知の発生               -> P1 候補
同じ正しい通知の単純重複           -> P2 候補
```

ただし大量重複によって重要通知が実質的に埋没するなら P1 として再評価する。

### 4.8 score / category の扱い

内部 score の delta だけでは P1 にしない。次のいずれかが有意に変わるなら P1 候補。

```
final Action / ユーザー表示 category / 売買強度 / 推奨価格 / 重要通知内容
```

Production 実データまたは shadow で Action / category delta が確認された場合、
**「LINE 通知件数は同じ」だけを理由に P2 へ下げない。**

### 4.9 複数 finding を持つ Issue

```
Issue Priority = 最も高い Priority となる ACTIVE finding
```

次の finding は含めない。

```
RESOLVED / MOVED_TO_OTHER_ISSUE / OUT_OF_SCOPE / NO_LONGER_REPRODUCIBLE
```

過去に P1 相当の finding が存在していても、それが解消済みなら
**残っている active finding だけで再評価する。**

### 4.10 同一 Priority 内の順序

同じ Priority の中での実装順序は、次の観点で比較する。

```
1. Production reachability
2. 発生頻度
3. 影響銘柄 / 処理件数
4. 投資 Action への距離
5. 通知欠落 / 誤通知
6. workaround の有無
7. 修正の独立性・安全性
```

`priority:P1.1` のような細分 label は作らない。順序は queue / comment で管理する。

### 4.11 Priority は起票時から固定ではない

```
PRIORITY_REEVALUATION_ON_NEW_EVIDENCE = REQUIRED
```

次のいずれかが判明したら `PRIORITY_REEVALUATION_REQUIRED = YES` として再評価する。

```
1. Production reachability が変わった
2. Action delta が判明した
3. notification delta が判明した
4. batch-wide failure が実到達と判明した
5. latent -> normal recurring と判明した
6. normal recurring -> not reachable と判明した
7. high-impact finding が resolved / moved / out-of-scope になった
8. security exposure が判明した
9. privacy exposure が判明した
10. data recoverability の状況が判明した
11. AWS / resource の cost anomaly が判明した
12. capacity / resource exhaustion が判明した
13. compliance requirement が判明した
14. compensating control が追加・除去された
```

Priority を変更したら、GitHub へ根拠を durable comment として書き戻す。

```
PRIORITY_REEVALUATED       = YES
POLICY_VERSION             = PRIORITY_POLICY_V2
OLD_PRIORITY / NEW_PRIORITY
PRODUCTION_REACHABILITY
DIRECT_INVESTMENT_IMPACT
RATIONALE
```

### 4.12 判定できない場合

証拠を複数方向から確認しても Priority を確定できない場合は、推測で埋めず
`PRIORITY_RECONCILIATION_REQUIRED` として不足している証拠を明記する。
定量調査が必要なら `PHASE_A_PRIORITY_EVIDENCE_REQUIRED` として次タスク候補にする。

---

### 4.13 非機能リスクの評価軸

`PRIORITY_POLICY_V2` は投資機能への影響を中心に定義していたため、
security / privacy / compliance / data protection / cost / reliability 等の
非機能リスクを Priority へ反映できない gap があった。本節以降がその是正である。

```
PRIORITY_POLICY_VERSION = PRIORITY_POLICY_V2_NFR
```

最低限、次を正式な評価軸として扱う。

| dimension | 対象 |
|---|---|
| `SECURITY` | credential / 権限 / 認証 / 攻撃面 |
| `PRIVACY` | 個人情報・投資情報・秘密情報の露出 |
| `COMPLIANCE` | 法令・契約・利用規約 |
| `DATA_PROTECTION` | 復元可能性・backup・deletion protection |
| `COST` | AWS running cost・課金の増加 |
| `RELIABILITY` | 可用性・失敗率 |
| `CAPACITY` | resource 枯渇・上限到達 |
| `PERFORMANCE` | 遅延・処理時間 |

`PERFORMANCE` は独立軸として扱ってよいが、遅延が通知遅延や投資機会損失へ
直接届く場合は **FUNCTIONAL_IMPACT 側の P1** としても評価できる。

### 4.14 functional / non-functional の MAX 規則

```
ISSUE_PRIORITY = MAX(FUNCTIONAL_PRIORITY, NON_FUNCTIONAL_PRIORITY)
                 （P0 > P1 > P2 > P3）
```

両者を**独立に**評価し、高い方を採用する。

**security issue だから自動的に P0、cost issue だから自動的に P0 とはしない。**
必ず reachability / blast radius / immediacy を評価する(§4.18)。

### 4.15 `priority:P0` の非機能条件

§4.1(system continuity / data integrity)に加え、次のいずれかに該当し、かつ
**CURRENT / IMMINENT / NORMAL_REACHABLE** であるものを P0 とする。

#### security

```
active credential compromise
active unauthorized access
internet から重大資産へ認証なし・実質無防備で到達でき、現在攻撃可能
AWS account / Production data / secrets 全体へ高確率・低障壁で到達できる重大 exposure
即時 containment が合理的に必要
```

**「blast radius が大きい」だけで自動 P0 にしない。**
`ATTACK_REACHABILITY` / `EXPLOITABILITY` / `CURRENT_EXPOSURE` /
`COMPENSATING_CONTROLS` を確認する。

#### privacy

```
個人情報・投資情報・秘密情報が現在 PUBLIC に露出している
継続的に漏洩している
即時の封じ込めが必要
```

#### compliance

```
法令・契約・利用規約への重大違反が現在発生しており、
システム停止・利用停止・重大是正を直ちに要する
```

推測で法的結論を出さない。明確な contract / regulation の evidence がある場合のみ。

#### cost runaway

```
UNCONTROLLED_COST_RUNAWAY
  AWS cost が異常な速度で増加している
  runaway loop / recursive invoke / 無制御な resource 生成等により、
    放置時間に比例して損失が急増する
  日単位・時間単位で無視できない追加 cost が発生している
  budget を短期間で大幅に超過する合理的見込みがある
  停止しない限り増加し続ける
```

**「少し高い」「最適化の余地がある」「不要 resource が月数百円」は P0 にしない。**

cost の P0 判定では、可能な範囲で次を評価する。

```
CURRENT_COST_RATE / BASELINE_COST_RATE / MULTIPLIER / ABSOLUTE_COST /
GROWTH_RATE / EXPECTED_24H_COST / EXPECTED_30D_COST /
SELF_TERMINATING / HUMAN_ACTION_REQUIRED_TO_STOP
```

金額の閾値は現時点で固定しない。予算感・運用規模の判断が必要な場合は
`HUMAN_DECISION_REQUIRED` として報告する。

### 4.16 `priority:P1` の非機能条件

§4.2(direct investment return impact)は維持したうえで、次を追加する。

```
security      現在の悪用証拠は無いが、normal operating state で credential compromise /
              unauthorized access / major blast radius へ直接到達できる
              例) 広範権限の長期 credential の恒久利用

privacy       PUBLIC 露出は現在確認されていないが、通常運用で重大な PII leakage が
              合理的に起こり得る。protection boundary が実質成立していない

data
protection    authoritative data を失った場合に復元不能。
              current data は正常だが、通常の運用ミス等から irreversible loss へ
              直接到達する

cost          継続的で有意な不要 cost。P0 ほど急激ではないが、放置期間に応じて
              明確な経済損失になる。monthly running cost へ大きな割合で影響する

reliability   major functionality が高頻度または通常経路で失敗し得るが、
              現時点で全面停止ではない
```

考え方は「**今すぐ incident containment が要るほどではないが、放置すると
ユーザーの投資成果・資産・秘密・費用・継続運用へ直接重大な損失を与え得る**」。

### 4.17 `priority:P2` / `priority:P3` の非機能条件

P2(§4.3 に加えて)。

```
security hardening だが current exploitability が低い
audit trail 不足 / retention 設計不足
minor privacy exposure risk
recovery 改善だが authoritative data への direct loss path が遠い
中程度・緩慢な cost inefficiency
observability 不足 / operational reliability 改善
```

例: CloudWatch Logs の retention 未設定で cost が緩やかに増える場合は原則 P2。
ただし**実測で cost が急騰しているなら P0 / P1 へ**。

P3(§4.4 に加えて)。

```
current Production exposure なし / latent hardening
cost 最適化の効果がごく小さい
future architecture improvement / minor operational convenience
```

### 4.18 非機能 Issue の評価記録

該当する項目について、最低限次を記録する。

```
NON_FUNCTIONAL_DIMENSION = SECURITY | PRIVACY | COMPLIANCE | DATA_PROTECTION |
                           COST | RELIABILITY | CAPACITY | PERFORMANCE | NONE

REACHABILITY = NORMAL_RECURRING | PUBLICLY_REACHABLE | INTERNAL_REACHABLE |
               MANUAL_ONLY | CONDITIONAL | LATENT | NOT_REACHABLE

BLAST_RADIUS = SINGLE_RECORD | SINGLE_FUNCTION | SINGLE_WORKFLOW |
               APPLICATION_WIDE | AWS_ACCOUNT_WIDE | PUBLIC_DATA_EXPOSURE | OTHER

IMMEDIACY = ACTIVE | IMMINENT | ONGOING | POTENTIAL | LATENT

COMPENSATING_CONTROLS = <summary>
```

§4.6 の `PRODUCTION_REACHABILITY` は機能面の到達性、本節の `REACHABILITY` は
非機能面の到達性を表す。両方に該当する Issue では両方を記録してよい。

### 4.19 MAX 規則の適用例

```
A  BUY 判定への影響なし + AWS account-wide の credential exposure = P1
   -> Issue = P1

B  投資機能への影響なし + 無制御な recursive Lambda で cost が急騰中 = P0
   -> Issue = P0

C  投資機能への影響なし + CloudWatch Logs の retention 未設定、
   cost 増加は緩慢 = P2
   -> Issue = P2

D  test flaky のみ = P3
   -> Issue = P3

E  BUY 通知の欠落 = P1 + 非機能影響 = P3
   -> Issue = P1
```

### 4.20 Severity とは引き続き独立

Priority が非機能を含むようになっても、Severity・release-blocker・
Progress Status との独立性(§2)は変わらない。

```
security / privacy / cost の Issue でも Priority は P0 / P1 になり得る
Severity は「問題が発生した場合の影響度」であり Priority とは別に判定する
```

なお現在の Severity ladder(§5)は機能欠陥を前提とした表現であり、
security / privacy / cost のカテゴリを直接表現できない。

```
SEVERITY_POLICY_GAP = 既知（本節では Priority 側のみ是正し、Severity policy は変更しない）
```

### 4.21 公開時の開示

非機能 Issue、特に security の Priority 根拠を public な GitHub へ書く際は、
攻撃手順・credential identifier・secret の実値・過度に具体的な resource identifier・
非公開のアーキテクチャ詳細を**追加公開しない**。

各 Issue の `PUBLIC_MINIMAL` / `PUBLIC_SANITIZED` 方針を維持する。
Priority の根拠は「広範な credential」「account-wide の blast radius」
「現時点では conditional」程度の抽象度で足りる。

---

### 4.22 Priority を meta-priority として使わない

```
PRIORITY_LABEL_NOT_USED_AS_META_PRIORITY = YES
```

Priority は **その Issue 自身の影響と、修理の緊急度**を表す metadata である。
sprint の順序・freeze 状態・governance 上の重要度を符号化するためだけに使わない。

```
BAD   governance Issue を「今スプリントで最優先だから」という理由だけで
      priority:P0 にする
GOOD  その Issue 自身の影響（continuity / data / investment / 非機能）で
      Priority を決め、sprint の順序は別の仕組みで表す
```

sprint / freeze / stabilization の状態は、**それを定める Issue と policy が
独立に保持する**。Priority label へ代替させると
「Priority = 投資運用と事業継続への影響」という定義が曖昧になり、
queue 決定と判定の一貫性が失われる。

同様に、Progress Status(§7)や waiting(§8)も Priority の代わりに使わない。

---

## 5. Severity

| ラベル | 意味 |
|---|---|
| `severity:SEV-1` | 重大 — 誤った投資判断・データ破壊に直結 |
| `severity:SEV-2` | 高 — 安全機構の無効化・仕様違反 |
| `severity:SEV-3` | 中 — 機能低下・可観測性の欠如 |
| `severity:SEV-4` | 低 — 軽微・表示のみ |

Severity は **発生した場合の影響度**であり、対応順序ではない。
したがって「SEV-1 だから自動的に P0」とはしない。逆も同様。

---

## 6. Release Blocker

Production release を**実際に止める** Issue にだけ `release-blocker` を設定する。
これは Priority / Severity とは完全に別軸である。

- `bug` + `priority:P0` + `severity:SEV-2` でも、release を止める必要がなければ
  `release-blocker` は付けない。
- `design-defect` + `priority:P1` + `severity:SEV-3` でも、
  特定機能を含む release を止めるなら `release-blocker` を付けてよい。

### 条件付き release-blocker

`release-blocker` が条件付きの場合、**label だけでは条件を表現できない。**

Issue 本文または最新の status comment へ、**何を含む release を止めるのか**を明記する。

```
BLOCKER_FOR_PRODUCTION_DEPLOYMENT_OF_#xx
NEXT_PRODUCTION_RELEASE_BLOCKER
```

`release-blocker` が付いていても「すべての Production release を止める」とは
限らない。**release 判断時は label の有無だけでなく、必ず block 条件を確認する。**

### blocking target の必須記録

`release-blocker` を付与する場合、**Issue 本文または最新の durable status comment
へ次の構造化情報を必ず記録する。** label だけでは block 対象を表現できないためである。

```
BLOCKER_MODE              = DEFECT_BLOCK | VERIFICATION_HOLD
BLOCKING_TARGET_TYPE      = ISSUE | COMMIT | RELEASE_CANDIDATE | PRODUCTION_NEXT
BLOCKING_TARGET           = 具体的対象
BLOCK_REASON              = 理由
BLOCKER_SCOPE             = 何を含む release を止めるのか
BLOCKER_REMOVAL_CONDITION = 解除条件
BLOCKER_ADDED_AT          = 付与日
```

必要に応じて次も記録する。

```
REMEDIATION_COMMIT           = sha | PENDING
PRODUCTION_VERIFICATION_PLAN = Issue の該当 section / comment への参照
```

**GitHub label 自体に値を持たせようとしない。** label は `release-blocker` の
存在だけを示し、詳細な target は Issue の durable record で管理する。
5軸モデル(§1)は変更しない。

#### 必須記録が不足している場合は fail-closed とする

`release-blocker` が付いているにもかかわらず、上記の必須記録のいずれかが
不足している場合は次のように扱う。

```
BLOCKER_METADATA_COMPLETE = NO
RELEASE_DECISION          = INSUFFICIENT_EVIDENCE
```

**必須記録が不足している `release-blocker` を、blocker が無いものとして
扱ってはならない。** release 可否を `INSUFFICIENT_EVIDENCE` とし、
blocking target / scope を確定するまで Production release へ進んではならない。

記録が無いことは「その blocker が release を止めない」ことの根拠にならない。
**label を無視して release することは禁止**である。
不足を解消する方法は、当該 Issue へ必須記録を追加して blocking target と
scope を確定させることであって、blocker を無視することではない。

### BLOCKER_MODE の定義

**`DEFECT_BLOCK`**

修正がまだ release artifact へ入っていないため、欠陥を未修正のまま Production へ
出すことを禁止する状態。

```
BLOCKER_MODE         = DEFECT_BLOCK
BLOCKING_TARGET_TYPE = PRODUCTION_NEXT
意味                  = remediation commit を含まない release は禁止
```

remediation commit が merge され、**その修正を Production へ入れる release 自身**を
この blocker で禁止してはならない。

**`VERIFICATION_HOLD`**

remediation commit は main / release candidate へ入っているが、
Production Verification が未完了の状態。

```
意味 = remediation release そのものの deploy は許容する
      deploy 後、Issue 定義の mandatory verification + ChatGPT PASS +
      human approval まで blocker を維持する
      この期間は、当該 verification を未完了のまま
      さらに次の通常 Production release へ進むことを禁止する
```

### remediation release の自己 block 禁止

```
BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER
```

`release-blocker` は「**問題を未解消のまま通過する release**」を止めるための
ものであり、「**その blocker 自身を解消するための remediation release**」を
永遠に禁止するものではない。

remediation release を許可する条件は次のとおりで、**すべて**満たすこと。

```
remediation fix が merge 済み
release scope に当該 fix が含まれる
Production Verification Plan が定義済み
blocker は deploy だけでは解除しない
unrelated piggyback 禁止ルールを満たす
exact release candidate SHA について人間承認
ChangeSet CREATE / EXECUTE は別途人間承認
```

これは「OPEN blocker があっても無視してよい」というルールでは**ない**。
release 可否は、各 blocker の `BLOCKING_TARGET` / `BLOCKER_SCOPE` を確認して
判定する。grouped release 側の条件は
[docs/development_workflow.md](development_workflow.md) §9 が正本。

### 既存 blocker の移行

本節の導入だけを理由に、既存 Issue の blocker metadata を一括書き換えしない。
現在 OPEN の `release-blocker` は、**次回の status update 時に新フォーマットへ
同期する**方針とする。既存の履歴を破壊しない。

### Production-target defect の release-blocker lifecycle

現行 Production に実害が出ている欠陥(Production-target defect)については、
`release-blocker` を次の lifecycle で扱う。

```
blocker 付与
  → 修正の merge
  → Production deploy
  → Immediate Verification
  → mandatory verification(Issue が定義したもの)
  → ChatGPT review
  → human approval
  → blocker 解除
```

重要な点は次のとおり。

- **deploy しただけでは解除しない。merge しただけでも解除しない。**
  Issue が `MANDATORY_FOR_RELEASE_BLOCKER_REMOVAL` と定義した verification が
  完了して初めて解除の判断ができる。
- **`Issue close` と `release-blocker 解除` は別判断である。**
  verification 完了前に Issue を close しない一方、blocker を解除しても
  後続 Phase が残るなら Issue は OPEN のままでよい。逆に、Issue を close しても
  blocker が別条件で残ることもありうる。
- 未完了の verification が
  **`OPTIONAL_POST_RELEASE_OBSERVATION` だけになった場合は、解除しうる。**
  この場合、自然な障害発生を待つことを必須とせず、事前に定義した代替証拠
  (unit / contract tests、CI、Immediate Verification、正常系 natural evidence)
  と ChatGPT review PASS、人間判断をもって解除可否を決める。
  分類の定義と代替証拠の要件は
  [docs/development_workflow.md](development_workflow.md) 7節が正本。
- **Issue 自身が自然な negative-path observation を Acceptance Criteria として
  明示している場合、これを勝手に `OPTIONAL` へ格下げしない。**

この lifecycle は、§6 冒頭の「`release-blocker` は Production release を
**実際に止める** Issue にだけ設定する」という意味を変更するものではない。
解除の手順を明確にするものである。

---

## 7. Progress Status

Progress Status は「**この Issue が開発ライフサイクル上どこまで進んだか**」だけを
表す軸である。Type / Priority / Severity / Release Blocker から自動推論しない。

### 7.1 8つの状態

| label | 意味 |
|---|---|
| `status:未着手` | Issue は登録済みだが、Phase A の調査・設計にまだ着手していない |
| `status:調査・設計中` | Phase A / investigation / design を実施中。設計は未確定 |
| `status:設計済` | Phase A 完了。実装方針が確定している。コード実装は未開始 |
| `status:開発中` | branch 上でコード・設定・docs 等の実装を開始しており、implementation complete に未到達 |
| `status:開発済` | implementation complete。必要な branch test / PR / CI まで到達しているが main へ未 merge(原則 OPEN PR + implementation complete + CI green) |
| `status:マージ済` | main へ merge 済み。Production へ反映すべき変更があるが、まだ deploy されていない |
| `status:デプロイ済` | Production 反映済み。ただし Issue が要求する必須 Production verification が未完了 |
| `status:本番検証済` | Issue 固有の必須 verification が完了し、技術的には close 可能な状態 |

`status:設計済` は Human decision 待ちでも成立する。設計が完了しているなら status は
`設計済` のままとし、待ち理由は §8 の waiting label で補助表現する。

### 7.2 排他制約

```
STATUS_LABEL_COUNT_PER_OPEN_ISSUE = 1（0 個禁止 / 2 個以上禁止）
```

status を遷移させるときは、**旧 status label を remove して新 status label を add** する
(置換)。履歴目的で複数の status を残さない。履歴は Issue State Snapshot が保持する。

```
status:設計済  --(implementation start)-->  status:開発中
```

### 7.3 進捗の測り方(複数 Phase を持つ Issue)

1つの修正対象を段階的に進める Issue(Phase A / B-1 / B-2 / …)では、
**到達した最も進んだ工程**を status とする。後続 Phase が残っていることは、
Issue が OPEN であること自体と Issue State Snapshot が表す。

umbrella / tracking Issue のように「完了」が単一の工程で定義できない Issue では、
到達点ではなく **その Issue の現在の活動段階**を status とする(活動中の tracking Issue は
`status:調査・設計中`)。

### 7.4 Production 変更を伴わない Issue

すべての Issue が Production lifecycle を通るわけではない。

```
test-only / docs-only / governance / investigation / tracking /
accepted-risk / not-a-bug
```

これらでは、不要な `status:マージ済` / `status:デプロイ済` を経由する必要はない。
その Issue 固有の完了条件(main CI green、deterministic verification、
documentation verification 等)を満たした時点で `status:本番検証済` へ進めてよい。

```
status:本番検証済 = 「Issue 固有の最終 verification 完了」を含む広義の final verified state
```

名称に「本番」が含まれるが、**Production 変更が存在しない Issue にも適用される**。
これは承認済みの 8 段階名称を維持したうえでの定義であり、名称の読み替えではなく
定義の明文化である。

### 7.5 GitHub state との関係

GitHub の `state=CLOSED` が Issue 完了そのものを表すため、`status:完了` という label は
新設しない。

```
CLOSED_ISSUE_STATUS_LABEL_POLICY = KEEP_FINAL_STATUS
```

close 時は最後の status label をそのまま残す。過去 Issue の一覧でも
「どこまで実証されて close されたか」が判別できるためである。
既存の CLOSED Issue への一括 backfill は必須としない。

---

## 8. waiting metadata(補助状態)

waiting label は Progress Status とは独立した補助軸であり、**判定軸ではない**。

| label | 意味 |
|---|---|
| `waiting:本番検証` | 実装 / merge / deploy 等は進んでいるが、自然実行・所定時刻・Production 観測等を待っている |
| `waiting:人間判断` | 技術調査・設計等は完了しているが、Human decision / approval がないと次工程へ進めない |
| `waiting:外部条件` | 外部サービスの事象、データ蓄積、自然障害の発生、外部情報の到着等を待っている |

```
WAITING_LABEL_COUNT_PER_ISSUE = 0 個以上（複数併用可。ただし必要最低限）
```

### 8.1 status と waiting の違い

```
status  = どこまで完了したか
waiting = なぜ今進んでいないか
```

両者は併用する。

```
status:デプロイ済 + waiting:本番検証
status:設計済     + waiting:人間判断
status:デプロイ済 + waiting:外部条件
```

### 8.2 waiting:人間判断 を付けない場合

merge 承認・Production ChangeSet EXECUTE 承認のように、**全 Issue 共通の短時間 gate**
ごとに機械的に付け外ししない。Issue が実質的に Human decision blocked になっている
場合にのみ使う。

### 8.3 重複を避ける

主因が明確なら 1 つに絞る。例えば「自然な障害事象の発生待ち」が主因の Issue では
`waiting:外部条件` を優先し、`waiting:本番検証` を重ねない。

---

## 9. Issue State Snapshot との関係

Progress Status label は **derived metadata** であり、Issue State Snapshot の代替ではない。

| | 役割 |
|---|---|
| GitHub label | 人間が Issue 一覧で現在地を把握するための粗い状態 |
| Issue State Snapshot | AI / review / gate 判断に使う詳細な SSoT |

```
SSoT 優先順位
  Issue State Snapshot / GitHub factual state（PR / merge / deploy evidence）
    ↓
  status label
```

### 9.1 label 同期の運用契約

```
STATE_TRANSITION_WRITEBACK_REQUIRED = YES（既存）
STATUS_LABEL_WRITEBACK_REQUIRED     = YES（新規）

WORKER_STATE_WRITE_OWNER        = ACTOR_WHO_CHANGED_STATE（既存）
WORKER_STATUS_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE（新規）
CHATGPT_STATE_READ_OWNER        = CHATGPT（既存・変更なし）
```

state を変更した当人が、同じ作業の中で status label も同期する。
同期対象となる state transition は次のとおり。

| transition | status |
|---|---|
| Issue created | `status:未着手` |
| PHASE_START | `status:調査・設計中` |
| PHASE_COMPLETE | `status:設計済` |
| IMPLEMENTATION_START | `status:開発中` |
| IMPLEMENTATION_COMPLETE(未 merge) | `status:開発済` |
| PR_MERGED(Production deploy が必要) | `status:マージ済` |
| PRODUCTION_DEPLOYED(verification 未了) | `status:デプロイ済` |
| PRODUCTION_VERIFIED | `status:本番検証済` |
| ISSUE_CLOSED | 最終 status を維持(`KEEP_FINAL_STATUS`) |

`OWNER_CHANGE` は status の変更理由にならない。

### 9.2 stale / 不整合 label の扱い

Assignment read barrier では、latest Issue State Snapshot と current status label の
整合を確認する。食い違う場合は次のように扱う。

```
例: snapshot は IMPLEMENTATION_START、label は status:未着手
  -> STATUS_RECONCILIATION_REQUIRED
```

このとき、**stale または missing な label だけを理由に既存の事実を捨てない。**
GitHub comments / PR / merge / deploy evidence から reconcile し、
事実に合わせて label を直す。逆はしない。

判定に足る証拠が得られない場合は、推測で label を付けず
`STATUS_RECONCILIATION_REQUIRED` として Issue 番号と不足証拠を報告する。

---

## 10. 推測して埋めない

Progress Status も推測で埋めない。証拠が得られない場合は
`status:未着手` を便宜的に付けず、`STATUS_RECONCILIATION_REQUIRED` として
Issue 番号と不足証拠を報告する(判定手順は §9.2)。

**最重要原則: label を「埋めること」を目的にしない。**

目的は、Issue の性質・対応優先順位・影響度・release 可否を、
**人間と AI の双方が同じ意味で判断できる状態にすること**である。

判断するための十分な根拠がない場合、**無理に label を付けない。**
特に既存 Issue について、「本文に Priority / Severity が書いていない」という理由だけで、
コード内容から勝手に P0/P1 や SEV-2 等を決めてはならない。

不明なものを推測して label 付けするより、
**「未確定」と明示して判断を求めることを優先する。**

### 未設定の2種類を区別する

`severity:*` が付いていない状態には、**意味の異なる2種類**がある。混同してはならない。

| 状態 | 意味 | 記載 |
|---|---|---|
| **N/A(適用対象外)** | Severity という**評価軸自体が適用されない**。欠陥ではない Issue(`enhancement` / `tracking` / `calibration` など) | 本文へ `Severity: N/A` と**確定判断として**記載する |
| **未確定(TRIAGE_REQUIRED)** | Severity を**評価すべき** Issue だが、根拠不足でまだ決定できない | `SEVERITY_TRIAGE_REQUIRED` として報告し、判断を求める |

Priority についても同じ区別を維持する
(「適用対象外」と「未確定」を将来的に混同しない)。
ただし通常の実装 Issue では Priority を設定することを基本とする。

### 報告用語(GitHub label ではない)

次の語は **GitHub の label ではなく、AI から人間への status / report vocabulary** である。
**label として作成・付与してはならない。**

| 用語 | 使う場面 |
|---|---|
| `PRIORITY_SEVERITY_TRIAGE_REQUIRED` | Priority と Severity の双方が未確定 |
| `SEVERITY_TRIAGE_REQUIRED` | Severity のみ未確定 |
| `PRIORITY_TRIAGE_REQUIRED` | Priority のみ未確定 |
| `LABEL_CONSISTENCY_DECISION_REQUIRED` | 本文と labels が矛盾し、どちらが最新の確定判断か人間の判断が要る |

---

## 11. label 化しないもの

### Phase を label 化しない

同じ Issue で Phase A / Phase B-1 / Phase B-2 / Production verification 等が
進行する場合でも、**Phase ごとの label を追加しない。**
Phase は Issue comment で管理する。

### 細粒度の一時的 status を label 化しない

§7 の Progress Status(8段階)と §8 の waiting metadata は label 化する。
それより細かい次のような一時的状態は、引き続き label 化しない。

```
IMPLEMENTATION_REVIEW_REQUIRED   PR_CREATED      PR_CI_GREEN
MERGE_READY                      PRODUCTION_PENDING
HANDOFF                          PAUSED          WAITING_REVIEW
```

これらは **Issue comment / PR state / GitHub Actions / Git branch** を
truth source とする。labels は Progress Status の粒度までに留める。

### Issue label を PR へコピーしない

Issue labels は Issue の分類情報である。
`priority:P0` / `severity:SEV-2` / `bug` 等を PR へ機械的にコピーしない。
PR label が必要な場合は、PR 独自の目的に応じて別途判断する。

---

## 12. tracking Issue の扱い

tracking Issue では、**子 Issue の Priority / Severity を親へ機械的に伝播しない。**

配下に P0 / SEV-2 や P1 / SEV-3 の Issue が存在しても、
tracking Issue 自身に `priority:P0` / `severity:SEV-2` を付けない。

**親 Issue 自身に Priority / Severity が明示的に定義されている場合のみ**設定する。

---

## 13. 運用フロー

### 13.1 新規 Issue 作成時

Issue 登録は「本文を書く → labels を設定する」までを**1つの作業**とする。
Issue だけ登録して labels を後回しにしない。

```
duplicate 検索
  ↓
Issue 作成
  ↓
5軸判定(Type / Priority / Severity / release-blocker 要否 / Progress Status)
  ↓
labels 設定
  ↓
Label consistency 確認
```

本文にも次のように記載し、**labels と一致させる**
(labels だけを truth source にしない)。

```
Classification: BUG
Priority:       P1
Severity:       SEV-2
Release blocker: NO
Progress status: 未着手
```

新規 Issue の Progress Status は `status:未着手` から始める。

### 13.2 Issue 着手時

実装開始前に必ず **Issue 本文 / 最新コメント / labels** を確認する。
このとき latest Issue State Snapshot と current status label の整合も確認する
(§9.2)。

矛盾がある場合、**そのまま実装を開始しない。**
明らかな label 同期漏れなら修正する。判断が必要なら
`LABEL_CONSISTENCY_DECISION_REQUIRED` として報告する。
status label と snapshot が食い違う場合は
`STATUS_RECONCILIATION_REQUIRED` として扱い、事実に合わせて label を直す。

着手報告には次を含める。

```
Labels: ...
Label consistency: PASS / FAIL
Status label: <現在の status:>
```

FAIL なら実装開始前に解消する。

着手して Phase A を開始したら、その作業の中で status label を
`status:未着手` から `status:調査・設計中` へ置換する
(`STATUS_LABEL_WRITEBACK_REQUIRED = YES`、§9.1)。

### 13.3 investigation 完了時

調査後は結論に合わせて labels を再評価する。

| 結論 | 操作 |
|---|---|
| CONFIRMED_BUG | `investigation` を外す → `bug` を付ける → Priority / Severity を確定 → release-blocker 要否を判断 |
| DESIGN_DEFECT | `investigation` を外す → `design-defect` を付ける |
| NOT_A_BUG | `investigation` を外す → `not-a-bug` を付ける → 必要に応じて `NOT_PLANNED` で close |
| ACCEPTED_RISK | `investigation` を外す → `accepted-risk` を付ける → 受容理由・残余リスクを記録 |

分類が変わった場合、**Issue 本文の過去の判断を削除しない。**
取り消し線などで「当初そう判断したが、調査によって撤回した」という
**監査証跡を残す**(実例は §14 の #79)。

### 13.4 Issue close 前

close する前に最終 Label consistency check を行う。

- Type は最終結論と一致しているか
- Priority は本文と一致しているか
- Severity は本文と一致しているか(N/A と未確定を取り違えていないか)
- `release-blocker` を残すべきか
- `bug` + `not-a-bug` になっていないか
- `investigation` のまま結論済みになっていないか
- `accepted-risk` の根拠が記録されているか
- status label が到達した最終工程を表しているか(通常は `status:本番検証済`)

矛盾を解消してから close する。close 時に status label は削除せず、
最終 status をそのまま残す(`KEEP_FINAL_STATUS`、§7.5)。
完了報告には次を含める。

```
Final labels: ...
Label consistency: PASS
Final status: <status:...>
```

### 13.5 Production release 前

OPEN な `release-blocker` を検索する。

ただし **`release-blocker` label がある = 必ず今回 release 禁止、とは限らない。**
各 Issue の本文 / 最新コメントの block 条件を確認し、
今回の release 対象 commit に block 対象機能が含まれるかを判定する(§6)。

### 13.6 別 bug を発見した場合

作業中の Issue とは別の bug / design-defect を発見した場合:

```
既存 Issue 検索 → 重複有無確認 → 無ければ新規 Issue 登録
  → Type 設定 → Priority 検討 → Severity 検討
  → release-blocker 要否検討 → Parent / Related 記載
  → 現在の Issue では原則修正しない
```

**opportunistic fix は禁止。**

---

## 14. 実例

以下は本ポリシーの適用例である。
**Issue 番号はあくまで「例」であり、ポリシーの定義そのものを
個別 Issue の事情へ依存させない。**

| Issue | labels | ポイント |
|---|---|---|
| #81 | `bug` `priority:P0` `severity:SEV-2` `release-blocker` | 判定軸がすべて立つ例 |
| #82 | `design-defect` `priority:P1` `severity:SEV-3` `release-blocker` | 安全性 bug ではないが、特定機能を含む release を止める**条件付き blocker** |
| #85 | `enhancement` `priority:P0` | P0 でも `release-blocker` を付けない例。Severity は N/A |
| #49 | `tracking` | 子 Issue に P0 / SEV-2 があっても親へ伝播しない |
| #73 | `tracking` `priority:P2` | 親自身の本文に Priority 明示があるため設定した例 |
| #79 | `not-a-bug`(CLOSED / NOT_PLANNED) | 調査結果に応じて再分類し、本文に撤回の監査証跡を残した例 |
| #87 | `enhancement` `priority:P2` | Severity **N/A**(適用対象外)であり、未確定ではない例 |

---

## 15. 現行 label 一覧

| label | description |
|---|---|
| `bug` | Something isn't working |
| `design-defect` | 設計上の欠陥(安全性バグではないが機能が成立しない) |
| `enhancement` | New feature or request |
| `investigation` | 調査してbug/仕様/許容リスクの結論を出す(欠陥確定前) |
| `calibration` | 実績データ蓄積後の閾値・パラメータ再評価(欠陥ではない) |
| `tracking` | 親Issue・追跡表(それ自体は修正対象ではない) |
| `not-a-bug` | 調査の結果、欠陥ではないと確定 |
| `accepted-risk` | 既知だが対応しないと判断したリスク |
| `priority:P0` | 最優先で対応する |
| `priority:P1` | 高 |
| `priority:P2` | 中 |
| `priority:P3` | 低 |
| `severity:SEV-1` | 重大(誤った投資判断・データ破壊に直結) |
| `severity:SEV-2` | 高(安全機構の無効化・仕様違反) |
| `severity:SEV-3` | 中(機能低下・可観測性の欠如) |
| `severity:SEV-4` | 低(軽微・表示のみ) |
| `release-blocker` | 次回Production releaseの前に解消が必要 |
| `status:未着手` | Progress Status: 登録済みだがPhase A調査・設計に未着手 |
| `status:調査・設計中` | Progress Status: Phase A / 調査・設計を実施中(設計確定前) |
| `status:設計済` | Progress Status: Phase A完了。実装方針が確定、コード実装は未開始 |
| `status:開発中` | Progress Status: branch上で実装中(implementation complete未到達) |
| `status:開発済` | Progress Status: implementation complete。PR/CIまで到達、main未merge |
| `status:マージ済` | Progress Status: mainへmerge済み。Production反映が必要だが未deploy |
| `status:デプロイ済` | Progress Status: Production反映済み。必須verificationが未完了 |
| `status:本番検証済` | Progress Status: Issue固有の最終verification完了。技術的にclose可能 |
| `waiting:本番検証` | 補助metadata: 自然実行・所定時刻・Production観測を待っている |
| `waiting:人間判断` | 補助metadata: Human decision / approvalがないと次工程へ進めない |
| `waiting:外部条件` | 補助metadata: 外部事象・データ蓄積・外部情報の到着を待っている |

`release-blocker` の実際の適用範囲は、Issue 本文の block 条件も確認すること(§6)。

label description を変更する場合は、**意味が他軸と重ならないこと**を確認する
(例: `priority:P0` の説明に「次回リリース前に着手」と書くと
Release Blocker 軸と混同されるため不可)。

---

## 16. 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-08-30 | 初版作成(#87)。4軸モデル、Type 8種と排他関係、Priority / Severity の定義、条件付き release-blocker、推測禁止と報告用語、Severity の「N/A」と「TRIAGE_REQUIRED」の区別、Phase / status を label 化しない方針、tracking への非伝播、運用フロー、実例を規定。 |
| 2026-09-03 | §6 へ blocking target semantics を追加(#122)。`release-blocker` 付与時の必須記録(`BLOCKER_MODE` / `BLOCKING_TARGET_TYPE` / `BLOCKING_TARGET` / `BLOCK_REASON` / `BLOCKER_SCOPE` / `BLOCKER_REMOVAL_CONDITION` / `BLOCKER_ADDED_AT`)を定め、必須記録が不足する場合は fail-closed(`BLOCKER_METADATA_COMPLETE=NO` / `RELEASE_DECISION=INSUFFICIENT_EVIDENCE`)として blocker を無視した release を禁止した。`BLOCKER_MODE` の `DEFECT_BLOCK` / `VERIFICATION_HOLD` を定義し、`BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER`(remediation release を自身の blocker で禁止しない)と、その許可条件7点を明文化した。既存 blocker の metadata は一括書き換えせず次回 status update 時に同期する。**既存の4軸独立性・条件付き blocker・Production-target defect の lifecycle・Issue close と blocker 解除の分離はいずれも変更していない** |
| 2026-09-05 | 第5軸 **Progress Status** を追加(#122)。`status:` 8種(未着手 / 調査・設計中 / 設計済 / 開発中 / 開発済 / マージ済 / デプロイ済 / 本番検証済)を定義し、OPEN Issue には常に1つだけ付与する排他制約(`STATUS_LABEL_COUNT_PER_OPEN_ISSUE = 1`)を規定した。判定軸ではない補助 metadata として `waiting:` 3種(本番検証 / 人間判断 / 外部条件)を追加し、「status = どこまで完了したか」「waiting = なぜ今進んでいないか」の区別を明文化した。Progress Status は derived metadata であり Issue State Snapshot の代替ではないこと(SSoT 優先順位は snapshot / GitHub factual state が上位)、`STATUS_LABEL_WRITEBACK_REQUIRED = YES` と `WORKER_STATUS_LABEL_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE`、state transition と status の mapping、不整合時の `STATUS_RECONCILIATION_REQUIRED` を規定した。close 時は最終 status を残す(`CLOSED_ISSUE_STATUS_LABEL_POLICY = KEEP_FINAL_STATUS`)。Production 変更を伴わない Issue(test-only / docs-only / governance / investigation / tracking 等)はマージ済・デプロイ済を経由せず、Issue 固有の最終 verification 完了をもって本番検証済としてよい。**既存の Type / Priority / Severity / Release Blocker の定義と独立性は変更していない** |
| 2026-09-05 | **Priority Policy V2** を正本化(#122)。Priority の判定根拠を「ユーザーの投資運用に対して、その Issue をどの順番で直すべきか」と定義し、一行定義(P0 動かない・データが壊れる / P1 動くが投資判断が狂う / P2 投資判断は概ね正しいが補助機能が狂う / P3 投資機能は正しく開発・運用を改善する)と各段の判定質問・代表例を規定した。`SUBSYSTEM_BASED_PRIORITY_FORBIDDEN = YES`(subsystem 名だけで Priority を決めず ROOT_CAUSE -> PRODUCTION_REACHABILITY -> DOWNSTREAM_EFFECT -> USER_INVESTMENT_EFFECT まで追う)、`PRODUCTION_REACHABILITY_REQUIRED = YES`(NORMAL_RECURRING / MANUAL_ONLY / CONDITIONAL / LATENT / NOT_REACHABLE。到達しないものを機械的に1段下げる規則にはしない)、`ACTUAL_FINANCIAL_LOSS_REQUIRED_FOR_P1 = NO`(実損の Production 観測は不要。failure injection は引き続き禁止)を追加した。通知の欠落・誤 Action は P1 候補、正しい通知の単純重複は P2 候補(埋没するなら P1 再評価)。内部 score delta だけでは P1 にせず、final Action / 表示 category / 売買強度 / 推奨価格 / 重要通知内容が有意に変わる場合を P1 候補とする。複数 finding を持つ Issue は ACTIVE finding のみで最も高い Priority を採用し、resolved / moved / out-of-scope は含めない。同一 Priority 内の順序は 7 観点で比較し、細分 label は作らない。`PRIORITY_REEVALUATION_ON_NEW_EVIDENCE = REQUIRED` と再評価トリガー 7 種、判定不能時の `PRIORITY_RECONCILIATION_REQUIRED` / `PHASE_A_PRIORITY_EVIDENCE_REQUIRED` を規定した。**Type / Severity / Release Blocker / Progress Status の定義と 5軸の独立性は変更していない** |
| 2026-09-05 | Priority へ**非機能リスク**を正式に含めた(#122、`PRIORITY_POLICY_V2_NFR`)。`ISSUE_PRIORITY = MAX(FUNCTIONAL_PRIORITY, NON_FUNCTIONAL_PRIORITY)` を規定し、SECURITY / PRIVACY / COMPLIANCE / DATA_PROTECTION / COST / RELIABILITY / CAPACITY / PERFORMANCE を評価軸として追加した。P0 の定義を「事業継続を脅かす」へ拡張し、active な credential compromise・認証なしで攻撃可能な公開露出・現在進行の PUBLIC な個人情報漏洩・重大な compliance 違反・`UNCONTROLLED_COST_RUNAWAY` を P0 条件として明文化した。**security / cost であることだけを理由に自動 P0 にはせず**、ATTACK_REACHABILITY / EXPLOITABILITY / CURRENT_EXPOSURE / COMPENSATING_CONTROLS、および cost では CURRENT_COST_RATE / MULTIPLIER / GROWTH_RATE / SELF_TERMINATING 等の評価を要求する(金額閾値は固定せず、必要なら HUMAN_DECISION_REQUIRED)。P1 へ「広範権限の長期 credential」「protection boundary が成立していない privacy」「authoritative data が復元不能」「継続的で有意な不要 cost」等を、P2 へ「exploitability の低い hardening」「audit trail / retention 不足」等を追加した。非機能 Issue には NON_FUNCTIONAL_DIMENSION / REACHABILITY / BLAST_RADIUS / IMMEDIACY / COMPENSATING_CONTROLS の記録を求め、MAX 規則の適用例 5 件を示した。再評価トリガーへ security / privacy / recoverability / cost / capacity / compliance / compensating control の 7 項目を追加した。public な GitHub へ security の根拠を書く際に攻撃手順・identifier・secret 実値を追加公開しない方針を明記した。**Severity policy は変更しておらず、security / privacy / cost を表現できない `SEVERITY_POLICY_GAP` は既知として記録するに留めた。** Type / Severity / Release Blocker / Progress Status の定義と 5軸の独立性は変更していない |
| 2026-09-05 | §4.22「Priority を meta-priority として使わない」を追加した(#122)。`PRIORITY_LABEL_NOT_USED_AS_META_PRIORITY = YES` とし、sprint の順序・freeze 状態・governance 上の重要度を符号化するためだけに Priority label を使わないこと、sprint / freeze / stabilization の状態はそれを定める Issue と policy が独立に保持することを明文化した。governance Issue を「今スプリントで最優先だから」という理由だけで P0 にしない。**判定基準そのもの(§4.1〜§4.21)は変更していない** |

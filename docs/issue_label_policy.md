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

## 1. 4軸モデル

Issue には次の4つの軸がある。

| 軸 | 問い | ラベル |
|---|---|---|
| **Issue Type** | 何の Issue か | `bug` / `design-defect` / `enhancement` / `investigation` / `calibration` / `tracking` / `not-a-bug` / `accepted-risk` |
| **Priority** | いつ対応するか(対応順序) | `priority:P0` / `priority:P1` / `priority:P2` / `priority:P3` |
| **Severity** | 問題が発生した場合の影響度 | `severity:SEV-1` / `severity:SEV-2` / `severity:SEV-3` / `severity:SEV-4` |
| **Release Blocker** | Production release を止めるか | `release-blocker` |

---

## 2. 4軸は独立して判定する

**この4軸を相互に自動推論してはならない。** それぞれ独立した根拠で判断する。

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

| ラベル | 意味 |
|---|---|
| `priority:P0` | 最優先で対応する |
| `priority:P1` | 高 |
| `priority:P2` | 中 |
| `priority:P3` | 低 |

Priority は **対応順序・優先順位**を表す。

- **P0 だからといって次回 Production release を必ず止めるわけではない。**
  release 可否は `release-blocker` で別途判断する。
- **Severity が高い = Priority が高い、とは限らない。**

通常の実装 Issue では **Priority を設定することを基本とする**
(設定しない場合の扱いは §7 を参照)。

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

## 7. 推測して埋めない

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

## 8. label 化しないもの

### Phase を label 化しない

同じ Issue で Phase A / Phase B-1 / Phase B-2 / Production verification 等が
進行する場合でも、**Phase ごとの label を追加しない。**
Phase は Issue comment で管理する。

### 一時的 status を label 化しない

次のような一時的状態は原則として label 化しない。

```
IMPLEMENTATION_REVIEW_REQUIRED   PR_CREATED      PR_CI_GREEN
MERGE_READY                      PRODUCTION_PENDING
HANDOFF                          PAUSED          WAITING_REVIEW
```

これらは **Issue comment / PR state / GitHub Actions / Git branch** を
truth source とする。labels は比較的安定した分類情報に限定する。

### Issue label を PR へコピーしない

Issue labels は Issue の分類情報である。
`priority:P0` / `severity:SEV-2` / `bug` 等を PR へ機械的にコピーしない。
PR label が必要な場合は、PR 独自の目的に応じて別途判断する。

---

## 9. tracking Issue の扱い

tracking Issue では、**子 Issue の Priority / Severity を親へ機械的に伝播しない。**

配下に P0 / SEV-2 や P1 / SEV-3 の Issue が存在しても、
tracking Issue 自身に `priority:P0` / `severity:SEV-2` を付けない。

**親 Issue 自身に Priority / Severity が明示的に定義されている場合のみ**設定する。

---

## 10. 運用フロー

### 10.1 新規 Issue 作成時

Issue 登録は「本文を書く → labels を設定する」までを**1つの作業**とする。
Issue だけ登録して labels を後回しにしない。

```
duplicate 検索
  ↓
Issue 作成
  ↓
4軸判定(Type / Priority / Severity / release-blocker 要否)
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
```

### 10.2 Issue 着手時

実装開始前に必ず **Issue 本文 / 最新コメント / labels** を確認する。

矛盾がある場合、**そのまま実装を開始しない。**
明らかな label 同期漏れなら修正する。判断が必要なら
`LABEL_CONSISTENCY_DECISION_REQUIRED` として報告する。

着手報告には次を含める。

```
Labels: ...
Label consistency: PASS / FAIL
```

FAIL なら実装開始前に解消する。

### 10.3 investigation 完了時

調査後は結論に合わせて labels を再評価する。

| 結論 | 操作 |
|---|---|
| CONFIRMED_BUG | `investigation` を外す → `bug` を付ける → Priority / Severity を確定 → release-blocker 要否を判断 |
| DESIGN_DEFECT | `investigation` を外す → `design-defect` を付ける |
| NOT_A_BUG | `investigation` を外す → `not-a-bug` を付ける → 必要に応じて `NOT_PLANNED` で close |
| ACCEPTED_RISK | `investigation` を外す → `accepted-risk` を付ける → 受容理由・残余リスクを記録 |

分類が変わった場合、**Issue 本文の過去の判断を削除しない。**
取り消し線などで「当初そう判断したが、調査によって撤回した」という
**監査証跡を残す**(実例は §11 の #79)。

### 10.4 Issue close 前

close する前に最終 Label consistency check を行う。

- Type は最終結論と一致しているか
- Priority は本文と一致しているか
- Severity は本文と一致しているか(N/A と未確定を取り違えていないか)
- `release-blocker` を残すべきか
- `bug` + `not-a-bug` になっていないか
- `investigation` のまま結論済みになっていないか
- `accepted-risk` の根拠が記録されているか

矛盾を解消してから close する。完了報告には次を含める。

```
Final labels: ...
Label consistency: PASS
```

### 10.5 Production release 前

OPEN な `release-blocker` を検索する。

ただし **`release-blocker` label がある = 必ず今回 release 禁止、とは限らない。**
各 Issue の本文 / 最新コメントの block 条件を確認し、
今回の release 対象 commit に block 対象機能が含まれるかを判定する(§6)。

### 10.6 別 bug を発見した場合

作業中の Issue とは別の bug / design-defect を発見した場合:

```
既存 Issue 検索 → 重複有無確認 → 無ければ新規 Issue 登録
  → Type 設定 → Priority 検討 → Severity 検討
  → release-blocker 要否検討 → Parent / Related 記載
  → 現在の Issue では原則修正しない
```

**opportunistic fix は禁止。**

---

## 11. 実例

以下は本ポリシーの適用例である。
**Issue 番号はあくまで「例」であり、ポリシーの定義そのものを
個別 Issue の事情へ依存させない。**

| Issue | labels | ポイント |
|---|---|---|
| #81 | `bug` `priority:P0` `severity:SEV-2` `release-blocker` | 4軸がすべて立つ例 |
| #82 | `design-defect` `priority:P1` `severity:SEV-3` `release-blocker` | 安全性 bug ではないが、特定機能を含む release を止める**条件付き blocker** |
| #85 | `enhancement` `priority:P0` | P0 でも `release-blocker` を付けない例。Severity は N/A |
| #49 | `tracking` | 子 Issue に P0 / SEV-2 があっても親へ伝播しない |
| #73 | `tracking` `priority:P2` | 親自身の本文に Priority 明示があるため設定した例 |
| #79 | `not-a-bug`(CLOSED / NOT_PLANNED) | 調査結果に応じて再分類し、本文に撤回の監査証跡を残した例 |
| #87 | `enhancement` `priority:P2` | Severity **N/A**(適用対象外)であり、未確定ではない例 |

---

## 12. 現行 label 一覧

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

`release-blocker` の実際の適用範囲は、Issue 本文の block 条件も確認すること(§6)。

label description を変更する場合は、**意味が他軸と重ならないこと**を確認する
(例: `priority:P0` の説明に「次回リリース前に着手」と書くと
Release Blocker 軸と混同されるため不可)。

---

## 13. 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-08-30 | 初版作成(#87)。4軸モデル、Type 8種と排他関係、Priority / Severity の定義、条件付き release-blocker、推測禁止と報告用語、Severity の「N/A」と「TRIAGE_REQUIRED」の区別、Phase / status を label 化しない方針、tracking への非伝播、運用フロー、実例を規定。 |

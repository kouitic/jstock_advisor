# 開発ワークフロー・運用ガバナンス

**この文書の位置づけ**

本リポジトリにおける開発の進め方(lane / WIP / 実装パイプライン / テスト方針 /
記録先 / 検証 / release 判断 / 人間承認の境界)の**正本(SSoT)**である。

AI 非依存のリポジトリ運用ポリシーであり、特定の AI エージェントやセッションに
依存しない。[CLAUDE.md](../CLAUDE.md) は本文書への入口にすぎない。

label の 4 軸モデル(Issue Type / Priority / Severity / Release Blocker)そのものは
[docs/issue_label_policy.md](issue_label_policy.md) が正本である。本文書は label の
意味を定義せず、**release-blocker の lifecycle**についてのみ同文書と接続する。

Production の具体的な運用手順(deploy 手順・テーブル追加時の注意・
read-only 観測での副作用確認等)は [docs/operations_manual.md](operations_manual.md)
が正本である。本文書はそれらの手順を複製しない。

**ルールを変更する場合は本文書を更新する。**

---

## 0. 現在のモード: Stabilization Sprint

2026-09-02 より、開発運用を **STABILIZATION_SPRINT / PIPELINE_MODE** とする
(Issue #122、人間承認済み)。

```
期間            約10営業日
新機能          原則停止
P2 / P3 修正    原則停止
P0              即対応
P1              最優先で着手
queue reorder   原則1日1回
割込み          P0 のみ即割込み可
```

安定化を最優先し、既存の未解消 P0/P1 を短期で殲滅することを目的とする。
Sprint 終了後は 1 節以降の lane 定義のみを残し、「P2/P3 原則停止」は解除する。

---

## 1. Lane(A)

Issue を次の 4 lane へ割り当てる。上位 lane を優先する。

| Lane | 対象 |
|---|---|
| **Lane0** | Production incident / P0 |
| **Lane1** | P1 の `bug` / `design-defect` |
| **Lane2** | Production Verification Wait(実装・deploy 済みで検証待ち) |
| **Lane3** | P2 / P3 / `enhancement` |

- **P0 は即時割込み可。** 他 lane の作業中でも中断して着手してよい。
- Lane2 の Issue は**新規実装候補から除外する**(実装は完了しているため)。
- queue の並び替えは原則 1 日 1 回に限る(頻繁な組み替えで着手が分散するのを防ぐ)。
  P0 の割込みはこの制限の対象外。

Issue の実装状況は次の区分で表現する。

| 区分 | 意味 |
|---|---|
| `CODE_WORK_REQUIRED` | 実装が必要。implementation WIP の対象 |
| `PRODUCTION_VERIFICATION_ONLY` | 実装・deploy 済み。検証の完了待ち |
| `HUMAN_DECISION_BLOCKED` | 人間の判断・外部前提の確定待ち |
| `ALREADY_IMPLEMENTED` | 対応済み。close 判断のみ残る |
| `NOT_READY` | 依存 Issue の未完了等により着手できない |

---

## 2. WIP 制限(B)

AI エージェント 1 体あたり:

```
IMPLEMENTATION_WIP <= 1
INVESTIGATION_WIP  <= 2
```

- **同一 Issue の parallel implementation を禁止する。**
  1 つの Issue に対する implementation owner は常に 1 体
  (`one Issue / one implementation owner`)。
  他エージェントが担当する Issue の branch / PR を操作しない。
- **PR CI 待ち / review 待ち / Production verification 待ちは、
  implementation WIP から解放してよい。** これらの待ち時間を
  AI の待機理由にしない。次の Issue へ進む。
- **ただし同一 Issue へ修正が必要になった場合は、最優先で戻る。**
  review 指摘・CI 失敗・verification 失敗が発生したら、
  新しく着手した作業より優先して対応する。

---

## 2.5 指示プロトコル(B')

複数の AI エージェントへ並行して作業を依頼する際、**どの指示に対する回答か**が
曖昧になると、古い指示への回答を次工程の根拠にしてしまう事故が起きる。
本節はその防止のための統制である。

### 2.5.1 INSTRUCTION_ID

指示側から各エージェントへ出す**すべての作業指示に一意な ID を付与する**。

```
形式   <ASSIGNEE>-<YYYYMMDD>-<連番>
例     TARO-20260904-001
       JIRO-20260904-001
```

作業者は**回答時に、対応した INSTRUCTION_ID を必ず明記する**。

回答が次のいずれかに該当する場合、その回答を**自動的には次工程の根拠にしない**。
まず指示と回答の対応関係を確認する。

```
INSTRUCTION_ID が無い
別の ID が付いている
WITHDRAWN 済みの ID への回答である
```

### 2.5.2 指示の状態

各 INSTRUCTION_ID を最低限次の 3 状態で管理する。

```
PENDING     指示済み、回答未受領
ANSWERED    回答を受領した
WITHDRAWN   撤回済み(緊急差し替え等)。以後この ID への回答は無効
```

### 2.5.3 直列化は「作業者ごと」— 全体を止めない

```
PER_WORKER_SERIALIZATION = YES
GLOBAL_SERIALIZATION     = NO
```

**同一作業者**について、直前の通常指示が `PENDING` の間は次の通常指示を追加しない。
新しい通常指示を出す前に、その作業者の直前指示が `ANSWERED` または `WITHDRAWN`
であることを確認する。

**他の作業者の `PENDING` は、新規指示の発行を妨げない。** キューは作業者ごとに独立である。

```
誤り   「太郎が作業中なら次郎にも指示できない」
正しい 「太郎が PENDING なら太郎への次の通常指示を出さない。次郎へは出してよい」
```

#### 例 A — 別 worker への並行指示は可

```
太郎   TARO-20260904-001 = PENDING
次郎   JIRO-20260904-001 = ANSWERED

-> JIRO-20260904-002 を次郎へ発行してよい
-> TARO-20260904-002 を太郎へ発行してはいけない
   (太郎の 001 が ANSWERED / WITHDRAWN になるまで待つ)
```

### 2.5.4 緊急差し替え(EMERGENCY SUPERSEDE)

緊急時に限り、同一作業者の `PENDING` 指示が存在していても新指示へ差し替えてよい。
その場合、新指示に次を**必ず明記する**。

```
EMERGENCY                   = YES
SUPERSEDES                  = <旧 INSTRUCTION_ID>
PREVIOUS_INSTRUCTION_STATUS = WITHDRAWN
```

旧指示はその時点で無効になる。**その後に旧指示への遅延回答が届いても、
有効な完了報告として扱わない。**

#### 例 B — 緊急差し替え

```
太郎   TARO-20260904-003 = PENDING(Production 自然検証の待機中)

Production で新たな障害を検知したため差し替える:

INSTRUCTION_ID              = TARO-20260904-004
EMERGENCY                   = YES
SUPERSEDES                  = TARO-20260904-003
PREVIOUS_INSTRUCTION_STATUS = WITHDRAWN

-> 以後 TARO-20260904-003 への回答が届いても完了報告として扱わない
-> 太郎は TARO-20260904-004 に対してのみ回答する
```

### 2.5.5 回答フォーマット

作業報告の**冒頭**に最低限次を記載する。

```
INSTRUCTION_ID     = <対応した ID>
ASSIGNEE           = <TARO | JIRO>
INSTRUCTION_STATUS = ANSWERED
```

**複数の古い指示の結果を 1 つの回答へ混在させない。**
1 指示 1 回答を原則とし、対応関係を一意に保つ。

### 2.5.6 適用範囲

本節は **AI エージェントへの作業指示の統制**であり、Production release 手順そのもの
ではない。9 節(grouped release)・10 節(人間承認の境界)の要求を緩和しない。
とくに merge / Production deploy / ChangeSet / manual invoke 等の人間承認は、
INSTRUCTION_ID の有無にかかわらず従来どおり必要である。

---

## 3. 実装パイプライン(C)

標準の流れ:

```
implementation
  → targeted pytest
  → related regression
  → ruff
  → mypy
  → local commit
  → remote branch push
  → ChatGPT remote diff review
  → PASS
  → PR
  → required CI
  → MERGE_READY
  → HUMAN MERGE APPROVAL
  → merge
  → main CI
```

- **push 前の ChatGPT review は不要。**
  push は **review checkpoint** として扱い、レビュワーが GitHub 上の確定 diff を
  直接参照できるようにする。
- push は通常 push とし、**force push を使わない**
  (remote branch 公開後の履歴 rewrite を避けるため、rebase ではなく
  `git merge --no-ff origin/main` で最新 main を取り込む)。
- **merge には人間の明示承認が必要**(9 節)。
- merge method は通常の merge commit。squash / rebase merge / auto-merge /
  `--admin` は使用しない。承認対象の exact SHA に対してのみ成立させるため
  `--match-head-commit` を指定する。

### レビュー対象の指定

branch の分岐点が古い場合、`git diff origin/main..HEAD` は**他 Issue の変更を
削除差分として表示する**。レビュー依頼時は必ず merge-base を明示する。

```
正しい   git diff <merge-base>..<HEAD>
誤り     git diff origin/main..HEAD   ← 他 Issue の変更が削除として混入する
```

### Issue の自動 close を避ける

PR 本文では、Issue に後続 Phase が残る場合や Production 自然検証を close 条件と
する場合、`Fixes` / `Closes` を使わず **`Refs #xxx`** を使う。
`Fixes #xxx` は merge 時に Issue を自動 close するため、検証前に close されてしまう。

---

## 3.5 時間意味論変更ゲート(C')

Issue #52 / #143 / #148 は、いずれも「時刻・営業日・市場セッションの扱い」に
起因した。本節はその再発を上流で止めるためのゲートである(Issue #145)。

**大多数の PR には何も課さない。** トリガに該当する変更にのみ適用する。

### 3.5.1 トリガ判定

次の diff があるときだけ `TIME_SEMANTICS_IMPACT = YES` となる。
判定はファイルパスと diff の有無で決まり、著者・レビュワーの主観に依存しない。

```
T1  domain/market_session.py / business_calendar.py / jst.py / price_freshness.py
T2  provider が返す日付・時刻の決定ロジック
    (as_of_date / fetched_at / bar date / 取得窓の start・end / timezone 変換 /
     外部ライブラリの日付境界の使い方)
T3  時刻由来値を受け取って業務分岐する consumer
T4  test / mock / fixture の clock または期待日
```

T4 を含めるのは、**Issue #143 が「テスト期待値の変更」として現れた**ためである。

### 3.5.2 control identifiers

```
C-BM  固定 clock の境界マトリクス(全境界)
C-BS  分岐する状態のみの固定 clock テスト
C-MG  mutation guard(緩める方向の変更で必ず落ちること)
C-CO  affected cohort の組み合わせ実行(同一プロセス)
C-CS  clock 固定化の影響範囲分析(どの cohort に属するかの特定と記録)
C-PC  provider contract テスト
C-EL  外部ライブラリの境界仕様の独立根拠(version を必ず記録)
C-TZ  timezone / 取得窓の証拠
C-MD  mock 期待値が provider contract 由来であること(リテラルの期待日で固定)
```

### 3.5.3 決定表

| TRIGGER | REQUIRED_CONTROLS |
|---|---|
| **T1** | `C-BM` `C-MG` `C-CO` |
| **T2** | `C-PC` `C-EL` `C-TZ` `C-MD` |
| **T3** | `C-BS` |
| **T4** | `C-CS` `C-CO` + 変更対象が属する層(T1/T2/T3)の control |

**T1-T4 は階層ではない。** 番号の大小に上下関係はなく、それぞれが独立した
control の集合である。T1 は T2 を包含しない(共有ヘルパを変えても provider
契約の証拠は得られない)。

複数トリガに該当する場合:

```
REQUIRED_CONTROLS = 該当する全トリガの control の union
```

「より強いトリガを1つ選ぶ」方式は採らない。control が欠落するためである。

```
例  T1 + T2  ->  C-BM C-MG C-CO C-PC C-EL C-TZ C-MD
    T2 + T4  ->  C-PC C-EL C-TZ C-MD C-CS C-CO
    T3 のみ  ->  C-BS
```

### 3.5.4 control の内容

**`C-BM`(T1 のみ)** — 次の境界を固定 clock で網羅する。

```
寄り付き直前 / 寄り付きちょうど / 立会中 / 大引け1分前 / 大引けちょうど /
大引け後 / 非営業日 / 連休明け / UTC-JST 日跨ぎ / 真の未来日
```

業務上存在しない境界は N/A としてよいが、**理由を明記する**。無言の省略は不可。

**`C-BS`(T3)** — 実際に分岐する状態のみでよい。**無関係な全境界は不要。**
最も件数が多いのは T3 であり、ここへ全境界を課さないことが本ゲートを軽く保つ要点である。

**`C-CO` / `C-CS`(T4)** — 1 ファイルだけ clock を固定して他モジュールとの整合を
壊さないこと。固定化後、単独実行だけでなく **cohort の組み合わせ実行**で回帰が
無いことを確認する。cohort は `tests/unit/test_time_semantics_guard.py` の
registry に定義されている。

Issue #143 は単独実行と全件 CI では確認したが、**部分集合の組み合わせ実行を
確認しなかった**ため Issue #148 を露出させた。

**順序依存 cohort** — cohort のメンバを揃えるだけでは不十分な場合がある。
共有 module-global state による汚染には**方向**があるためである。

```
実測(Issue #148)
  integration -> handler   handler 側 11 件が失敗する
  handler -> integration   失敗しない
```

pytest の収集順(アルファベット)は `handler -> integration` であり、
**full CI ではこの汚染方向を通らない**。したがって順序依存が既知、または
合理的に疑われる cohort では `DECLARED_ORDER_CASES` を宣言し、その順序で実行する。

```
全順列は要求しない(cohort が 5 件なら 120 通りとなり組合せ爆発する)。
宣言された「既知・高リスクな汚染方向」のみを対象とする。
```

宣言した順序で既知の失敗が出る場合は `KNOWN_FAILURE_ISSUE` に owner Issue を
記載する。ただし **「red だが既知」で済ませない。**

```
期待する失敗集合 と 実際の失敗集合 を比較し、一致することを確認する。
新規の失敗が 1 件でもあれば、それは当該変更の regression として扱う。
```

order case は `tests/unit/test_time_semantics_guard.py` に宣言し、
同ファイルが metadata の健全性(登録済みモジュールのみ / cohort 外を参照しない /
重複が無い / 順序依存と宣言した cohort が order case を持つ)を検証する。
**実行そのものは自動化しない**(1 回あたり数分を要するため、CI を重くしない)。


**`C-EL`(T2)** — 「このコードがそう書いてあるから、実装者はそう理解している
はず」は**根拠にしない**。official docs / installed source / upstream source /
read-only experiment のいずれかで確認し、**version とともに記録する**。

**`C-MD`(T2)** — mock の期待値を判定側 helper から自己参照的に導出した
assertion は、恒真になりうるため provider contract の証拠と認めない。

```
不可  assert snapshot.as_of_date <= latest_plausible_bar_date(now, calendar)
      (mock が helper ちょうどで打ち切る実装なら失敗しえない)

可    局面ごとにリテラルの期待日を固定する
      ("PRE_OPEN_0800", _BUSINESS_DAY, 8, 0, _PREV_BUSINESS_DAY)
```

### 3.5.5 証拠の扱い

```
主証拠  固定 clock の境界マトリクス / mutation guard /
        provider の取得窓分析 / 外部ライブラリ境界の独立根拠
補助    CI がどの時刻(局面)で回ったか
```

**CI が「今の時刻で」green であることを correctness の主証拠にしない。**
Issue #143 では、大引け後に回った CI の green が false green であった。

導入しないもの:

```
CI の複数時刻実行 / 時刻別 matrix job / sleep・wait / 現在時刻依存テスト
```

検出確率を上げるだけで非決定性そのものは残るため、対策として誤りである。
正しい対策はテストを時刻非依存にすることに帰着する。

`full pytest` のローカル必須化も行わない(4節の方針を変えない)。

### 3.5.6 registry

時刻に敏感なテストモジュールは
`tests/unit/test_time_semantics_guard.py` の registry に登録する。
registry 自身の健全性(登録漏れ・削除・化石化した例外)も同ファイルで検証される。

```
FORBIDDEN         wall clock 呼び出しがあれば FAIL
ALLOWED_EXISTING  既存の残存リスク。rationale と owner Issue が必須。
                  負債が解消され呼び出しが 0 件になったら FAIL し、
                  FORBIDDEN への更新を促す
```

**全テストファイルを走査する方式は採らない。** 市場セッションに接触しない
wall clock の使用は正当であり、リスクベースの登録制とする。

### 3.5.7 参照 contract(Issue #52)

日足 bar の妥当性について確定した契約。ドキュメント上の参照であり、
本節が Production の挙動を定めるものではない。

```
寄り付き前          当日付の bar は存在し得ない        -> fail-close
立会中(未確定)     当日付の bar は存在しうる          -> 正常
大引け後(確定)     当日付の bar は存在する            -> 正常
非営業日            当日付の bar は存在し得ない        -> fail-close
翌日以降の日付      その時点で存在し得ない            -> fail-close
```

### 3.5.8 PR での宣言

`.github/PULL_REQUEST_TEMPLATE.md` に従い、非該当なら
`TIME_SEMANTICS_IMPACT = NO` の1行のみでよい。該当する場合は
`TRIGGERS` / `REQUIRED_CONTROLS` / `EVIDENCE` を記録する。

`NO` の宣言は免罪符ではない。変更内容と矛盾する場合、レビュワーはこれを FAIL とする。
PR 本文を CI で自動解析する仕組みは導入しない。

#### order-sensitive cohort に該当する場合

order case の実行は**自動化しない**(CI を重くしないため)。手動実行である以上、
実行が忘れられると metadata だけが残り、ゲートが実質的に働かなくなる。
したがって **実行証拠を PR review の必須項目とする**。

`EVIDENCE` へ最低限:

```
ORDER_CASES_EXECUTED = <実行した order case 名>
ORDER_CASE_RESULTS   = <各 case の結果>
```

該当する order case が `KNOWN_FAILURE_ISSUE` を持つ場合はさらに:

```
EXPECTED_FAILURE_SET = <期待する失敗集合の根拠>
ACTUAL_FAILURE_SET   = <実際の失敗集合の根拠>
FAILURE_SET_MATCH    = YES | NO
```

`FAILURE_SET_MATCH = NO`(= 既知失敗と一致しない)場合、差分は当該変更の
regression として扱う。**「red だが既知」で済ませない。**

期待する失敗集合のテスト名一覧を registry へハードコードすることは要求しない
(owner Issue の解消により変動するため)。実行時の出力を根拠として示せばよい。

**order-sensitive でない cohort には、この追加記録を求めない。**

---

## 4. ローカルテスト方針(D)

原則、ローカルでは次のみを実行する。

```
targeted pytest        変更した箇所を直接covertするテスト
related regression     変更が影響しうる周辺(-k で絞り込む)
ruff check src tests
mypy src
```

```
LOCAL_FULL_PYTEST_DEFAULT = FORBIDDEN
FULL_SUITE_AUTHORITY      = PR_CI
```

**全体回帰の正本は PR CI(required checks)である。**
local full suite を既定の手順にしない。1 回 20 分規模を要し、
各 worker が同じ全体テストを何度も繰り返すことになるため、
着手速度を大きく損なう。

**これは品質基準を下げるルールではない。**
ローカルでは狭く速く検証し、CI で広く検証する、という分担である。
全体回帰そのものは PR CI で必ず実行される。

### full suite を local 実行してよい場合(例外)

**suite 全体でしか確認できない具体的な理由がある場合**に限り実行してよい。

```
test order dependency
global state pollution
fixture lifecycle の問題
import-time side effect
collection の問題
CI の full-suite failure の再現
共通 test infrastructure の変更
suite-wide interaction の確認が Issue の主目的である
```

例外を使う場合は、作業報告等へ必ず次を明示する。

```
LOCAL_FULL_PYTEST_EXCEPTION = YES
REASON                      = <具体的理由>
```

```
「念のため」「安全のため」「一応」は理由にならない。
```

これらは**何を確認したいのかを述べていない**ため、例外の要件を満たさない。
上記の例のように、`suite 全体でなければ観測できない事象`を名指しする。

docs のみの変更では pytest / mypy / ruff を機械的に回す必要はない(11 節)。

---

## 5. GitHub への永続化(E)

次は**会話上の報告だけで完結させず、GitHub へ永続化する**。

- investigation(調査結果・再現・root cause)
- design decision(採用案と不採用案、その理由)
- verification result(Immediate / Natural / Negative-path)
- human decision(承認内容と承認対象の exact 識別子)
- merge / deploy result(SHA・CI 結果・ChangeSet ARN 等)

**会話のみを SSoT にしない。** セッションが変わっても同じ前提で作業を継続できる
ことを要件とする。

永続化先は原則として対象 Issue のコメント。運用ルール自体の変更は本文書へ。

**いつ・誰が・どの形式で current state を書き戻すかは 6.5節が正本である。**

---

## 6. 現況判断と status freshness(F)

Issue の現況は、次の 3 つを**総合して**判断する。

1. 最新の確定 status comment
2. current labels
3. Issue body

**どれか 1 つが常に正しいと決めない。** とくに「labels が Acceptance Criteria より
常に正しい」といった単純な優先順位を置かない。3 者が矛盾する場合は、
勝手に推測して実装を進めず、**どれが最新の確定判断かを確認する**
([CLAUDE.md](../CLAUDE.md) の既存ルール)。

Issue body に旧状態の記載が残ることは許容する。**historical evidence として
保持してよい。**

### close 前の最小同期

close する前に、**現況と矛盾する次の箇所のみ**を同期する。

- status(Issue state / verification 状態 / Production 反映状態)
- Production 状態の記載(旧 SHA・旧 deploy gate 等)
- Acceptance Criteria の checkbox

同期は**訂正の追記**によって行い、旧記載そのものを削除しない。
「上記は当時の状態であり、現況は次のとおり」と分かる形にする。

**Problem / Evidence / Root cause / 旧判断・旧 AC の取り下げ記録 /
accepted residual risk 等の歴史的記録を無闇に削除しない。**

本節は現況の**判断**を定める。書き戻しの発火条件・snapshot の形式・
新規割当前の read barrier は **6.5節**が正本である。

---

## 6.5 Issue state の同期(F')

6節は「現況をどう**判断**するか」を定める。本節は「現況をどう**書き戻し**、
新しい作業を割り当てる前にどう**読み直す**か」を定める。

```
ISSUE_STATE_SSOT = GITHUB_ISSUE

STATE_TRANSITION_WRITEBACK_REQUIRED             = YES
HANDOFF_IS_NOT_A_SUBSTITUTE_FOR_STATE_WRITEBACK = YES
WORK_COMPLETE_REQUIRES_SSOT_WRITEBACK           = YES
ASSIGNMENT_READ_BARRIER_REQUIRED                = YES
LATEST_VERIFIABLE_GITHUB_STATE_WINS             = YES

WORKER_STATE_WRITE_OWNER = ACTOR_WHO_CHANGED_STATE
CHATGPT_STATE_READ_OWNER = CHATGPT
```

state を変えた者が書き、次の割当を出す者が読む。**どちらか一方だけでは成立しない。**

### 6.5.1 なぜ handoff だけでは足りないか

handoff は「別の Issue / 別の担当へ移る時」にしか発火しない。しかし state は
その途中でも変わる。branch を push した、PR を作った、merge された、main CI が
落ちた、deploy した——これらは handoff を伴わずに起きる。

```
Issue 本文・snapshot   実装未着手
remote branch          実装済み commit が push 済み
  -> 「新規実装」として指示すると二重実装になる
```

したがって writeback の trigger は handoff ではなく **state transition** とする。

---

### 6.5.2 State transition writeback(WRITE 側)

次を writeback の trigger とする。

```
PHASE_START                     PHASE_COMPLETE
IMPLEMENTATION_START            IMPLEMENTATION_COMPLETE
BRANCH_PUSHED
PR_CREATED                      PR_REVIEW_PASS            PR_MERGED
MAIN_CI_PASS                    MAIN_CI_FAIL
PRODUCTION_DEPLOYED             PRODUCTION_VERIFIED       PRODUCTION_VERIFICATION_FAILED
BLOCKED                         UNBLOCKED
HUMAN_DECISION_REQUIRED         HUMAN_DECISION_RESOLVED
OWNER_CHANGE                    HANDOFF
```

`HANDOFF` は trigger の 1 つにすぎない。**handoff が無くても state が変われば
writeback が必要である。**

#### comment を増やしすぎないための batching

全 trigger で無条件にコメントすると Issue がノイズ化し、かえって最新 snapshot を
見失う。trigger を 2 種へ分ける。

```
ASSIGNMENT_VISIBLE    他の worker が現況を誤判断しうる transition
                      IMPLEMENTATION_START / BRANCH_PUSHED / PR_CREATED / PR_MERGED /
                      MAIN_CI_FAIL / PRODUCTION_DEPLOYED / PRODUCTION_VERIFIED /
                      PRODUCTION_VERIFICATION_FAILED / BLOCKED / UNBLOCKED /
                      HUMAN_DECISION_REQUIRED / HUMAN_DECISION_RESOLVED /
                      OWNER_CHANGE / HANDOFF

BATCHABLE             同一作業単位の内部的な進行
                      PHASE_START / PHASE_COMPLETE / IMPLEMENTATION_COMPLETE /
                      PR_REVIEW_PASS / MAIN_CI_PASS
```

```
BATCHABLE          作業完了時の 1 snapshot へ集約してよい
ASSIGNMENT_VISIBLE 次の作業割当より前に必ず durable 化する
```

原則として **1 作業単位(1 INSTRUCTION_ID)につき snapshot は 1 件**とし、
その中で複数 transition をまとめて記録する。ただし作業が長く、途中で
`ASSIGNMENT_VISIBLE` な transition が発生し、それが次の割当より前に見えない
状態になる場合は、その時点で追加の snapshot を出す。

```
COMMENT_SPAM_MINIMIZED      = YES   1 作業単位 1 snapshot を原則とする
STALE_STATE_WINDOW_BOUNDED  = YES   未書き戻しの ASSIGNMENT_VISIBLE transition が
                                    ある間は次の割当を出さない
```

---

### 6.5.3 ISSUE_STATE_SNAPSHOT contract

current state の記録は自由文だけにせず、機械的に読める固定キーを含める。

```
ISSUE_STATE_SNAPSHOT

DISCLOSURE          = PUBLIC_SANITIZED

STATE_ID            = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>
SUPERSEDES_STATE_ID = <previous STATE_ID|NONE>
STATUS_AS_OF        = <ISO-8601>
ISSUE               = #<number>

CLASSIFICATION      = <issue type>
PRIORITY            = <P0|P1|P2|P3>
SEVERITY            = <SEV-1|SEV-2|SEV-3|SEV-4|N/A>
RELEASE_BLOCKER     = <YES|NO>

PHASE               = <current phase>
IMPLEMENTATION      = <state>
OWNER               = <TARO|JIRO|USER|NONE>

BRANCH              = <name|NONE>
BRANCH_HEAD         = <sha|NONE>
PR                  = <number|NONE>
PR_STATE            = <state|NONE>
MERGED_TO_MAIN      = <YES|NO>

MAIN_SHA            = <sha|N/A>
MAIN_CI             = <state|N/A>
PRODUCTION_DEPLOYED = <YES|NO|PARTIAL|UNKNOWN|N/A>
PRODUCTION_VERIFIED = <YES|NO|PARTIAL|UNKNOWN|N/A>

CURRENT_BLOCKER     = <value|NONE>
NEXT_ACTION         = <value|NONE>
NEXT_ACTION_ALLOWED = <YES|NO>
```

`N/A` を許容する。Production へ到達していない Issue へ Production 行を
`UNKNOWN` として並べると、確認していないのか該当しないのかが区別できない。

snapshot は **current labels を記録するもの**であり、label の分類基準ではない。
Type / Priority / Severity / Release Blocker の 4軸モデルの正本は
[issue_label_policy.md](issue_label_policy.md) であり、本節はそれを変更しない。

#### STATE_ID の方式

単調増加の手動整数は、**TARO と JIRO が並行して snapshot を出した場合に
同じ番号を取り合って壊れる**。番号の採番自体に排他が要るため採用しない。

```
STATE_ID = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>

例  20260904T081341882304Z-JIRO-JIRO-20260904-020-a1b2c3d4
    20260904T081341882304Z-CHATGPT-MANUAL-9f72c1ab
    20260904T081342001234Z-USER-MANUAL-4d8e01f7
```

各成分の責務を分ける。**一意性を担うのは NONCE だけである。**

```
timestamp        人間が概ね時系列を読むための監査補助 / correlation 補助
                 collision resistance の根拠にはしない
ACTOR            発行主体の識別
                 TARO / JIRO / CHATGPT / USER
                 既存運用と整合する actor を追加してよいが、
                 無制限の自由文字列にはしない
INSTRUCTION_ID   作業の correlation
  | MANUAL       指示 ID を持たない操作は MANUAL とする
                 collision resistance の根拠にはしない
NONCE            STATE_ID の collision resistance を担保する識別成分
```

##### collision resistance は NONCE が担う

```
STATE_ID の collision resistance は独立生成の NONCE によって担保し、
timestamp / ACTOR / INSTRUCTION_ID 単独の一意性には依存しない。
```

依存させてはならない理由は 2 つある。

```
INSTRUCTION_ID   「必ず一意である」は運用規律にすぎない。
                 本プロジェクトでは既に instruction ID の collision が発生している。
                 規律は破られうるものであり、破られた時に状態追跡まで
                 壊れる設計にしない。

timestamp+ACTOR  衝突確率を下げるだけで一意性を保証しない。
                 同一 ACTOR が同一 microsecond 内に 2 つの snapshot を生成すれば
                 両者は同一になる。
```

本節が目的とするのは「通常は衝突しない」ことではなく、**運用規律が破られても
state tracking が壊れないこと**である。したがって一意性の担保は、既存要素から
独立した NONCE へ寄せる。

###### NONCE の生成

```
用いてよい   UUID4(フル)
             UUID4 由来の短縮表現(8 桁以上の hex 等)
             十分に長い random hex token

禁止         timestamp 派生
             INSTRUCTION_ID 派生
             ACTOR 派生
```

既存要素から決定論的に導出した値は、その要素が衝突した時に同時に衝突するため、
collision 耐性が増えない。**独立に生成すること。**

短縮表現は数学的な絶対一意を保証するものではない。本 contract が要求するのは
`collision-resistant unique identifier` であり、絶対一意の証明ではない。

timestamp を microseconds まで固定桁で保持するのは、監査時に時系列を読みやすく
するためであり、一意性のためではない。

##### 順序と「最新」の判定

```
timestamp prefix  人間が概ね時系列を読むための補助
                  識別 / correlation / 監査補助に使う
```

**STATE_ID の辞書順を「最新 snapshot」判定の唯一の truth source にしない。**

```
LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER
```

「最新 snapshot」は Issue のコメント列で最後に現れる `ISSUE_STATE_SNAPSHOT`
とする。actor のクロックずれや後から追記された snapshot があっても、
GitHub のコメント順が正本である。

##### SUPERSEDES_STATE_ID

直前の snapshot の `STATE_ID` を設定する。一致しない場合は
`STATE_DRIFT_DETECTED = YES` として扱う。

```
不一致 = 必ず不正、ではない
         parallel write / race / stale read の検出シグナルとして扱う
         reconciliation で current state を再構成する
```

#### append-only

```
古い snapshot を削除・改変しない
訂正は新しい snapshot の追加で行う
```

これは 6節「旧記載そのものを削除しない」と同じ原則である。監査履歴を残す。

#### 証拠の採用順序

snapshot も絶対視しない。**証拠の採用順序の正本は
[chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 3.6節**であり、
ChatGPT に限らず全 actor へ適用する。序列を本節へ複製しない
(複製すると正本の改訂時に本節が stale になり、本節が防ごうとしている事故を
本節自身が起こす)。

```
snapshot          IMPLEMENTATION = NOT_STARTED
remote branch     より新しい実装 commit が存在する
  -> CURRENT_VERIFIABLE_STATE が優先
  -> STATE_DRIFT_DETECTED = YES / STATUS_RECONCILIATION_REQUIRED = YES
```

---

### 6.5.4 Assignment Read Barrier(READ 側)

新しい Issue または別 Phase へ作業者を割り当てる**前**に実行する。
実行主体は ChatGPT(`CHATGPT_STATE_READ_OWNER = CHATGPT`)。

```
1   Issue current state / labels
2   最新の ISSUE_STATE_SNAPSHOT
3   その snapshot より後の Issue comments
4   関連する open / closed / merged PR
5   関連する remote branch
6   current main SHA
7   branch / PR commit が main に含まれるか
8   main CI
9   Production release state          (関連する場合)
10  Production verification state     (関連する場合)
```

**記憶・会話要約・古い Issue 記述だけを根拠に新規実装を指示しない。**

#### applicability(不要な確認を強制しない)

全 Issue で AWS / Production を確認させると、到達しない Issue にまでコストが出る。

```
Production 未到達の Issue          9 / 10 は N/A
branch が存在しないと確認済み       5 / 7 は N/A(存在しないことの確認自体は実施する)
PR が存在しないと確認済み           4 は N/A(同上)
docs のみの Issue                  8 / 9 / 10 は N/A
```

ただし **implementation state を判断する Issue では 1〜5 を原則必須**とする。
「branch は無いだろう」という推測で 5 を省略しない。ここを省略したことが
本節を設けた直接の原因である。

---

### 6.5.5 ASSIGNMENT_BASELINE

新しい指示には、read barrier で確認した baseline を持たせる。

```
ASSIGNMENT_BASELINE

ISSUE_STATE_AS_OF     = <time>
CURRENT_MAIN_SHA      = <sha>
LATEST_STATE_SNAPSHOT = <url|NONE>
RELATED_BRANCHES      = <list|NONE>
RELATED_PRS           = <list|NONE>
IMPLEMENTATION_STATE  = <state>
```

指示文を肥大化させない。branch / PR / Production が明らかに該当しない単純な
Issue では `N/A` / `NONE` を許容する。**baseline を書かないことは許容しない**
(確認したうえで該当なし、と、確認していない、は別である)。

---

### 6.5.6 State Freshness Gate

次のいずれかに該当する場合、新規 implementation を開始してはならない。

```
最新 snapshot が存在しない
snapshot が current GitHub evidence より古い
snapshot と branch / PR / main が矛盾する
Phase 状態が複数 source で矛盾する
owner が不明である
```

```
ISSUE_STATE_FRESHNESS_GATE             = FAIL
STATUS_RECONCILIATION_REQUIRED         = YES
NEW_IMPLEMENTATION_INSTRUCTION_ALLOWED = NO
```

まず **read-only の reconciliation** を行い、snapshot を現況へ同期する。
完了後に implementation gate を再評価する。

#### P0 例外(適用範囲は最小)

```
条件   P0 の Production incident で、即時の被害抑止が必要な場合に限る
緩和   read barrier を最小確認(Issue state / labels / 最新 snapshot /
       current main SHA)へ縮小し、freshness gate FAIL でも
       被害抑止のための指示を出してよい
記録   P0_BARRIER_REDUCED = YES と理由を残す
復旧   被害抑止の完了後、通常作業を再開する前に full reconciliation を行う
```

```
緩和しないもの
  Human Gate / merge 承認 / Production approval / exact ChangeSet approval
  PRODUCTION_DEPLOYMENT_EXECUTOR / DEPLOY_OPERATION_DELEGATION_TO_JIRO
  Production failure injection 禁止
  release-blocker lifecycle
```

**P0 例外は「読む手間を減らす」ものであり、「承認を飛ばす」ものではない。**

---

### 6.5.7 Work Complete Gate

作業の完了条件を変更する。実装・テスト・push・報告だけでは完了としない。

```
WORK_COMPLETE = TECHNICAL_WORK_COMPLETE
                AND REQUIRED_SSOT_WRITEBACK_COMPLETE
```

例えば branch push まで行った作業は、`IMPLEMENTATION` / `BRANCH` /
`BRANCH_HEAD` / `NEXT_ACTION` を snapshot へ同期してから `ANSWERED` とする。

不要な snapshot は強制しない。

```
SSOT_WRITEBACK_REQUIRED =
    state が変化した
    OR
    既存の state 記載が stale だと判明した
```

```
read-only の調査で state が変化せず、既存記載も stale でなかった
  -> snapshot 不要(報告のみでよい)
read-only の調査だが、既存記載が stale だと判明した
  -> 訂正の snapshot が必要
```

---

### 6.5.8 handoff の責務(再定義)

```
ISSUE_STATE_SNAPSHOT   current state の主要記録
HANDOFF                次担当への補足情報
```

handoff へ current state 全体を再コピーしない。二重管理になり、両者が食い違う。

```
HANDOFF minimum

LATEST_STATE_SNAPSHOT   = <url>
WHY_HANDOFF             = <reason>
NEXT_RECOMMENDED_ACTION = <action>
SPECIAL_CAUTION         = <notes|NONE>
```

handoff を残す既存の運用は維持する。そのうえで
`HANDOFF_IS_NOT_A_SUBSTITUTE_FOR_STATE_WRITEBACK = YES` を明記する。

---

### 6.5.9 人間が state を変えた後の責務

merge は `MERGE_EXECUTOR = USER` であり(10節)、作業 AI だけでは全 transition を
書き戻せない。

```
worker が PR 作成 -> USER が GitHub 上で merge
  -> PR_MERGED / MAIN_CI_PASS を worker は書き戻せない
```

```
NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES
```

ChatGPT は post-merge の gate で次を確認する。

```
merge commit / current main SHA / main CI / Issue state の writeback
```

snapshot が無ければ、ChatGPT 自身が記録するか、worker へ reconciliation を
指示して同期させてから次工程へ進む。ChatGPT が直接 Issue を書き換える運用を
必須にはしない。**要点は「誰かがやるだろう」を禁止し、next gate owner を
明示することである。**

---

### 6.5.10 State drift audit

queue reorder は原則 1 日 1 回(0節)。これに合わせて軽量な drift audit を行う。

```
P0 / P1 の open Issue      必須
P2 / P3                    全件走査しない。次のいずれかに該当するものだけ
                             queue 候補として着手を検討している
                             remote branch または open PR が存在する
                             直近で human decision を待っている
```

**Stabilization Sprint の速度を落とさないため、P2 / P3 の全件重走査は行わない。**

検出対象の例:

```
Issue は NOT_STARTED だが branch が存在する
Issue は PR 未 merge だが PR が merged
Issue は Production 未 deploy だが deploy 済みの evidence がある
Issue は verification pending だが PASS evidence がある
labels が最新の triage と不一致
同一 finding の owner が複数 Issue に存在する
```

検出時:

```
STATE_DRIFT_DETECTED           = YES
IMPLEMENTATION_START           = BLOCKED
STATUS_RECONCILIATION_REQUIRED = YES
```

---

## 7. Negative-path verification(G)

negative path(異常系)の検証要求を 2 種へ分類する。

### `MANDATORY_FOR_RELEASE_BLOCKER_REMOVAL`

その検証が完了するまで `release-blocker` を解除できない。
Issue 本文の Production Verification Plan に明示する。

### `OPTIONAL_POST_RELEASE_OBSERVATION`

自然な障害発生を待つことを必須としない。次の**事前に定義された代替証拠**が
揃っていることをもって判断してよい。

- unit / contract tests(mutation 検証で検出力を確認したもの)
- CI required checks
- Immediate Verification(deploy 直後の read-only 確認)
- 正常系の natural evidence(自然実行が正常終端していること)

そのうえで **ChatGPT review PASS + 人間判断**により解除可否を決める。

### 分類を勝手に変えない

**Issue 自身が「自然な negative-path observation」を Acceptance Criteria として
明示している場合、これを勝手に `OPTIONAL` へ変更しない。**
分類の変更には Issue の更新と人間の判断が要る。

### Production failure injection は禁止

Production で `AccessDenied` / provider failure / Lambda failure 等を
**人工的に発生させない**。必要と判断される場合は別途人間の明示承認を得る。

---

## 8. AWS / CloudWatch の pagination(H)

**「存在しない」「件数が少ない」ことを根拠に Production の欠陥や正常性を
主張する場合、pagination の完了を必須とする。**

対象例:

- CloudWatch Logs `filter_log_events`(`nextToken`)
- DynamoDB `Scan` / `Query`(`LastEvaluatedKey`)
- CloudFormation の list 系 API(`NextToken`)
- Lambda の list 系 API(`NextMarker`)

**単一 page を全件と解釈しない。** 最初の 1 ページだけを見て
「0 件」「1 件しかない」と判定してはならない。

件数を報告する場合は、pagination を完了させたうえでの件数であることを示す。
上限を設けて打ち切った場合は、**打ち切った事実と上限値を明記する**
(黙って truncate した結果を全件のように報告しない)。

なお、検索条件そのものの取りこぼしにも注意する。名前・文字列パターンで
絞り込む場合、対象が別表現で記録されている可能性を確認する
(例: CloudFormation の Processed テンプレートでは、テーブル名の実体ではなく
論理 ID で参照されている)。

---

## 9. Production grouped release(I)

複数 Issue をまとめて release する場合、次を**すべて**満たすこと。

```
SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0
  (release scope 外に OPEN な release-blocker が無い)
included Issues がすべて merge 済み
各 Issue の Production Verification Plan が既知
先行する mandatory verification に未完了が無い
included Issues が相互に競合しない
release inventory を再構築済み(baseline → target の全 commit を列挙し、
  意図しない Issue 由来の commit が混入していないことを確認)
人間が exact release candidate(SHA)を承認
```

### release scope 内の blocker remediation は release へ含められる

第1条件は「**release scope 外**に OPEN な release-blocker が無いこと」である。
`OPEN な release-blocker = 0` ではない。

`release-blocker` の lifecycle は
[docs/issue_label_policy.md](issue_label_policy.md) §6 のとおり

```
blocker 付与 → 修正の merge → Production deploy → Immediate Verification
  → mandatory verification → ChatGPT review → human approval → blocker 解除
```

であり、**Production deploy が blocker 解除の前提**である。したがって
「OPEN blocker が 1 件でもあれば deploy できない」と適用すると循環し、
Production-target defect を恒久的に remediate できなくなる。

release scope 内の `release-blocker` については、次を**すべて**満たす場合に
release へ含めてよい。

```
今回の release がその blocker の remediation を含む
その修正が merge 済み
その Issue の Production Verification Plan が定義済み
deploy 後も blocker を維持する(deploy では解除しない)
mandatory verification + ChatGPT review + human approval まで解除しない
```

これは「OPEN blocker を無視してよい」という緩和ではない。
**解除条件は一切緩めない。** deploy はあくまで verification の前提であり、
deploy 自体が blocker を解除する根拠にはならない。

release scope 外に OPEN な `release-blocker` がある場合は、その blocker の
block 条件を確認し、今回の release を止めるものかどうかを判断する。
止めるものであれば grouped release へ進まない。

**`release-blocker` label の有無だけで release 可否を判断しない。**
各 blocker の `BLOCKER_MODE` / `BLOCKING_TARGET` / `BLOCKER_SCOPE` を確認する。
これらの記録形式と `DEFECT_BLOCK` / `VERIFICATION_HOLD` の定義、および
`BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は
[docs/issue_label_policy.md](issue_label_policy.md) §6 が正本である。

### release inventory の追跡要件

Production release 監査では、baseline → target の**全 commit** について
次を追跡できること。

```
ISSUE
PR
COMMIT
BLOCKER_MODE
BLOCKING_TARGET
VERIFICATION_STATUS
```

Issue へ辿れない commit が baseline → target に含まれる場合は、
その commit の由来を確定させるまで release へ進まない。

### blocker remediation release への piggyback は禁止

Production が**障害状態**にあり、それを解消するための release
(`BLOCKER_REMEDIATION_RELEASE`)には、**無関係な変更を相乗りさせない**。

この場合の release scope は「その障害の修正のみ」であることを、
baseline → target の commit 列挙で確認する。
`単に main が最新だから OK` としない。

`BLOCKER_REMEDIATION_RELEASE`(Production 障害の解消専用)と、
複数の Production-target blocker をまとめて解消する通常の grouped release は
別物である。前者は scope を障害修正のみに限定する。後者は上記の条件を
満たす限り複数 Issue を含めてよいが、**いずれの場合も
release scope 外の変更を便乗させない**点は共通である。

---

## 9.5 すべての変更を Issue 起点とする

```
NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE
```

理由を問わず、**挙動・構成・運用・契約へ影響する変更は必ず GitHub Issue を
起点とする。**

対象:

```
application code
infrastructure
IAM
config / threshold
schema
CI/CD
operational behavior
Production-affecting docs
governance rules
```

変更理由が次のいずれであっても Issue は必須である。

```
bug fix / design correction / requirement addition / requirement change
refactor / security improvement / operations improvement
```

**「小さい変更だから」「ついでだから」を Issue 省略の理由にしない。**

### Issue 省略を許す唯一の例外

純粋な doc-only であり、次の**両方**を満たすものだけ Issue なしを許容してよい。

```
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

例: typo 修正 / 表現だけの修正 / 壊れたリンク修正 / Markdown formatting。

この場合も PR 本文へ次を記録する。

```
ISSUE_EXCEPTION       = DOC_ONLY_NON_BEHAVIORAL
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

**判断が曖昧なら Issue を作る側へ倒す。**
**governance rule の変更はこの例外に含まれない。** 本文書のような workflow 変更は
Issue 必須である。

### Issue Type は既存体系を維持する

新しい Type label を不用意に追加しない。
[docs/issue_label_policy.md](issue_label_policy.md) §3 の

```
bug / design-defect / enhancement / investigation
calibration / tracking / not-a-bug / accepted-risk
```

を維持する。`requirement-change` / `refactor` / `security` / `operations` /
`governance` 等は、現時点では**新しい Type 軸を作らず**、
既存 Type + Issue 本文の Reason / Scope で表現する。

```
新要件            -> enhancement
セキュリティ改善   -> bug / design-defect / enhancement を実態で判定
governance 変更   -> enhancement / tracking 等を実態で判定
```

4軸モデル(Issue Type / Priority / Severity / Release Blocker)を崩さない。

### commit / PR と Issue の追跡

Issue 必須の変更では、**PR が必ず Issue を参照する。**

```
Refs #xx    基本。Production Verification pending の Issue では
            merge 時点で自動 close させないため必ずこちらを使う
Fixes #xx   Issue 全体をその PR で完了させる場合のみ
```

commit 単位でも、どの Issue 由来かを release inventory から追跡可能にする。
**main への直接 push は禁止**(既存方針を維持)。

---

## 10. 人間承認の境界(J)

次の操作には**人間の明示承認が必要**である。Sprint による高速化でこれらを
緩和しない。

```
merge
Production deploy
ChangeSet creation
ChangeSet execution
manual Production Lambda invocation
Production data write
migration / backfill
failure injection
```

補足:

- **ChangeSet の作成も Production 操作として扱う**(execute だけではない)。
- ChangeSet の execute 承認は **exact ChangeSet ARN に対してのみ有効**。
  ChangeSet を再作成した場合、その承認は失効する。新しい ARN を提示して
  再承認を得る。
- **Issue close と release-blocker 解除は別判断**である
  ([docs/issue_label_policy.md](issue_label_policy.md) §6)。
- 承認は**その操作・その対象に限る**。ある文脈での承認を別の文脈へ拡張しない。
- 作業指示の**宛先が異なる**場合(他エージェント宛の承認)、その承認をもって
  操作しない。宛先を確認する。
- **deploy 実作業を誰が行うか**は
  [chatgpt_collaboration_protocol.md](chatgpt_collaboration_protocol.md) 1.5節が正本
  (`PRODUCTION_DEPLOYMENT_EXECUTOR`)。**担当の集約は承認の省略を意味しない。**
  本節の人間承認は担当者が誰であっても従来どおり必要である。

### 併せて緩和しないもの

```
one Issue / one implementation owner
hidden-write 確認([CLAUDE.md](../CLAUDE.md) / operations_manual 18節)
duplicate Issue 確認(起票前に必ず duplicate search)
release-blocker lifecycle(issue_label_policy §6)
Production failure injection 禁止
```

---

## 11. docs のみの変更(DOC_ONLY_CHANGE)

変更が Markdown / ドキュメントのみの場合:

```
実施する    git diff --check(空白・行末の破損確認)
            PII / secret の混入確認
            相対リンク・パスの整合確認
            Markdown 構造の確認(見出し階層・表・コードフェンス)
実施不要    pytest / mypy(コード変更が無いため)
            ruff(Markdown のみなら対象外)
```

報告では `DOC_ONLY_CHANGE=YES` を明示する。

本節は doc-only 変更の**検証手順**を定めるものであり、**Issue の要否とは別**である。
Issue なしで進められるのは §9.5 の `ISSUE_EXCEPTION=DOC_ONLY_NON_BEHAVIORAL`
(`NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE`)を満たす場合に限る。
**governance rule の変更は doc-only であっても Issue 必須**である。

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-02 | 新規作成(Issue #122)。Stabilization Sprint の lane / WIP 制限 / 実装パイプライン / ローカルテスト方針 / GitHub 永続化 / status freshness / negative-path 分類 / AWS pagination / grouped release / 人間承認の境界を正本化した。既存の Human merge approval・Human Production approval・exact ChangeSet approval・release-blocker lifecycle・one Issue / one implementation owner・hidden-write 確認・Production failure injection 禁止・duplicate Issue 確認はいずれも緩和していない |
| 2026-09-03 | §9 の grouped release 第1条件を「OPEN な release-blocker = 0」から「release scope 外に OPEN な release-blocker が無い」へ修正(Issue #122)。release-blocker lifecycle(docs/issue_label_policy.md §6)では Production deploy が blocker 解除の前提であるため、旧記述では Production-target defect の remediation release が循環して実施不能になる不整合があった。あわせて release scope 内 blocker を release へ含めるための条件(remediation を含む / merge 済み / Verification Plan 定義済み / deploy 後も blocker 維持 / mandatory verification + ChatGPT review + human approval まで解除しない)を明文化し、`BLOCKER_REMEDIATION_RELEASE` と通常の grouped release の区別を補足した。**解除条件は一切緩和していない** |
| 2026-09-03 | release-blocker の blocking target semantics を導入(Issue #122)。`BLOCKER_MODE`(`DEFECT_BLOCK` / `VERIFICATION_HOLD`)・`BLOCKING_TARGET` 等の必須記録と `BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は docs/issue_label_policy.md §6 を正本とし、本文書 §9 はそれを参照する。§9 の第1条件を `SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0` として明示し、release inventory の追跡要件(ISSUE / PR / COMMIT / BLOCKER_MODE / BLOCKING_TARGET / VERIFICATION_STATUS)を追加した。あわせて §9.5 `NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE`(挙動・構成・運用・契約へ影響する変更は Issue 必須。例外は `NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE` の doc-only のみで、governance 変更は例外に含まない)・既存 Issue Type 体系の維持・Refs/Fixes の使い分け・main 直接 push 禁止の再確認を追加した。**既存の解除条件・piggyback 禁止・人間承認境界・4軸独立性はいずれも緩和していない** |
| 2026-09-03 | §3.5「時間意味論変更ゲート」を新設(Issue #145)。Issue #52 / #143 / #148 の再発を上流で止めるため、時刻・営業日・市場セッション・timezone・外部ライブラリの日付境界に触れる変更へのみ適用するゲートを定めた。トリガ T1-T4 をファイルパスと diff の有無で客観判定し、control(C-BM / C-BS / C-MG / C-CO / C-CS / C-PC / C-EL / C-TZ / C-MD)を決定表で対応づける。**T1-T4 は階層ではなく独立した control 集合**であり、複数該当時は union を要求する(「より強いトリガを1つ選ぶ」方式は control が欠落するため採らない)。最も件数の多い consumer 変更(T3)には分岐する状態のみを課し、全境界マトリクスは共有ヘルパ変更(T1)に限定する。トリガ非該当の PR には追加負担を課さない。外部ライブラリの境界仕様はコードからの推測を根拠とせず version つきの独立根拠を要求し、mock の期待値を判定側 helper から自己参照的に導出した assertion は provider contract の証拠と認めない。CI の実行時刻は補助証拠に留め、**CI の複数時刻実行・時刻別 matrix job・sleep/wait・現在時刻依存テストは導入しない**(検出確率を上げるだけで非決定性が残るため)。full pytest のローカル必須化も行わない(§4 の方針は不変)。あわせて .github/PULL_REQUEST_TEMPLATE.md と tests/unit/test_time_semantics_guard.py(registry と registry 自身の健全性検証)を追加した。**判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない**(開発運用ゲートの追加のみ) |
| 2026-09-04 | §2.5「指示プロトコル(B')」を新設(Issue #122)。複数の AI エージェントへ並行して作業を依頼する際、どの指示に対する回答かが曖昧になり、古い指示への回答を次工程の根拠にしてしまう事故を防ぐための統制。(1)指示側は全作業指示へ一意な `INSTRUCTION_ID`(`<ASSIGNEE>-<YYYYMMDD>-<連番>`)を付与し、作業者は回答冒頭へ同一 ID・ASSIGNEE・INSTRUCTION_STATUS を記載する。ID が無い回答・別 ID の回答・撤回済み ID への回答は自動的には次工程の根拠にせず、まず対応関係を確認する。(2)指示状態を `PENDING` / `ANSWERED` / `WITHDRAWN` で管理する。(3)**直列化は作業者ごと**とする(`PER_WORKER_SERIALIZATION=YES` / `GLOBAL_SERIALIZATION=NO`)。同一作業者の直前の通常指示が PENDING の間は次の通常指示を追加しないが、**他の作業者の PENDING は新規指示の発行を妨げない**。「一方が作業中なら他方にも指示できない」という誤読を避けるため、別 worker への並行指示可の例を明記した。(4)緊急時のみ `EMERGENCY=YES` / `SUPERSEDES` / `PREVIOUS_INSTRUCTION_STATUS=WITHDRAWN` を明記して差し替えてよく、撤回済み指示への遅延回答は有効な完了報告として扱わない。差し替えの例も明記した。(5)複数の古い指示の結果を 1 つの回答へ混在させない。あわせて CLAUDE.md へ入口となる記載を追加した。**本節は AI への作業指示の統制であり、9節(grouped release)・10節(人間承認の境界)の要求はいずれも緩和していない**(merge / Production deploy / ChangeSet / manual invoke 等の人間承認は INSTRUCTION_ID の有無にかかわらず従来どおり必要)。コード・Production 挙動の変更なし |
| 2026-09-04 | §4「ローカルテスト方針」を明確化(Issue #122)。`LOCAL_FULL_PYTEST_DEFAULT = FORBIDDEN` / `FULL_SUITE_AUTHORITY = PR_CI` を明示した。従来も「理由なく local full suite を毎回実行しない」としていたが、既定の可否が曖昧で、各 worker が同じ 20 分規模の全体テストを繰り返す運用が残っていた。**全体回帰の正本は PR CI** とし、ローカルでは targeted tests / related regression / ruff / mypy に絞る。**品質基準を下げるルールではなく、ローカルは狭く速く・CI は広く、という分担である。**そのうえで full suite の local 実行を完全禁止にはせず、test order dependency / global state pollution / fixture lifecycle / import-time side effect / collection の問題 / CI の full-suite failure 再現 / 共通 test infrastructure の変更 / suite-wide interaction の確認が主目的、といった **suite 全体でしか観測できない具体的理由**がある場合の例外とし、`LOCAL_FULL_PYTEST_EXCEPTION = YES` と `REASON` の明示を求める。「念のため」「安全のため」「一応」は何を確認したいのかを述べていないため理由として認めない。10節(人間承認の境界)・11節(DOC_ONLY_CHANGE)は変更していない。コード・Production 挙動の変更なし |
| 2026-09-04 | §6.5「Issue state の同期(F')」を新設(Issue #157)。GitHub を SSoT としながら、変化する current state を**いつ・誰が・どの形式で書き戻すか**が未定義であったため、古い Issue 記述から実装状態を誤認したまま次の作業指示が出る事故が繰り返し発生していた。実際に、Issue 側は実装未着手を示す一方で remote branch には実装済み commit が push されており、新規実装として指示が出かけた事例がある。**「handoff コメントを丁寧に書く」だけでは解消しない**ため、WRITE 側と READ 側の双方を統制する。(1)writeback の trigger を handoff ではなく **state transition** とし、`PHASE_*` / `IMPLEMENTATION_*` / `BRANCH_PUSHED` / `PR_*` / `MAIN_CI_*` / `PRODUCTION_*` / `BLOCKED` / `HUMAN_DECISION_*` / `OWNER_CHANGE` / `HANDOFF` を列挙した(`HANDOFF` は trigger の1つにすぎない)。(2)Issue のノイズ化を避けるため trigger を `ASSIGNMENT_VISIBLE` と `BATCHABLE` へ二分し、原則 **1 作業単位 1 snapshot**へ集約する一方、他 worker が現況を誤判断しうる transition は次の作業割当より前に必ず durable 化する(`COMMENT_SPAM_MINIMIZED` と `STALE_STATE_WINDOW_BOUNDED` の両立)。(3)機械可読な `ISSUE_STATE_SNAPSHOT` contract を定義した。**手動連番の `STATE_VERSION` は TARO / JIRO の並行 write で番号を取り合うため採用せず**、`STATE_ID = <YYYYMMDDTHHMMSSffffffZ>-<ACTOR>-<INSTRUCTION_ID|MANUAL>-<NONCE>` とした。**collision resistance は独立生成の NONCE が担い、timestamp / ACTOR / INSTRUCTION_ID 単独の一意性へは依存させない。** INSTRUCTION_ID の「必ず一意」は運用規律にすぎず(本プロジェクトでは既に instruction ID collision が発生している)、timestamp + ACTOR も同一 ACTOR が同一 microsecond 内に 2 つの snapshot を生成すれば衝突するため、いずれも一意性の根拠にしない。NONCE は UUID4 由来等の独立生成値とし、**timestamp / INSTRUCTION_ID / ACTOR からの決定論的導出は禁止**する(元の要素が衝突した時に同時に衝突し、耐性が増えないため)。要求するのは `collision-resistant unique identifier` であり絶対一意の数学的保証ではない。timestamp を microseconds まで固定桁で保持するのは監査時の可読性のためであり一意性のためではなく、INSTRUCTION_ID は correlation 情報として保持する。timestamp prefix は識別・correlation・監査補助に用い、**STATE_ID の辞書順を「最新 snapshot」判定の唯一の truth source にしない**(`LATEST_SNAPSHOT_TRUTH_SOURCE = GITHUB_COMMENT_ORDER`)。`SUPERSEDES_STATE_ID` の不一致は並行 write の検出シグナルとして扱い、**不一致 = 必ず不正とはしない**(reconciliation で current state を再構成する)。旧 snapshot は append-only で保持する。Production 未到達 Issue のために `N/A` を許容し、未確認と該当なしを区別する。(4)割当前の **Assignment Read Barrier**(10項目)を定義し、applicability により Production / branch / PR が該当しない Issue へ無駄な確認を強制しない。ただし implementation state を判断する Issue では Issue / labels / snapshot / 後続コメント / PR / **remote branch** の確認を原則必須とした。(5)`ASSIGNMENT_BASELINE` を指示へ持たせる(該当なしは `N/A` 可、ただし**未記載は不可**)。(6)`ISSUE_STATE_FRESHNESS_GATE = FAIL` の間は新規 implementation 指示を禁止し、先に read-only reconciliation を行う。P0 Production incident に限り read barrier を最小確認へ縮小する例外を設けたが、**Human Gate / merge 承認 / Production approval / exact ChangeSet approval / deploy 実行者 / failure injection 禁止 / release-blocker lifecycle はいずれも緩和しない**(読む手間を減らす例外であり承認を飛ばす例外ではない)。(7)`WORK_COMPLETE = TECHNICAL_WORK_COMPLETE AND REQUIRED_SSOT_WRITEBACK_COMPLETE` とし、state が変化した場合または既存記載が stale と判明した場合にのみ writeback を要求する(read-only 調査へ不要な snapshot を強制しない)。(8)handoff を「次担当への補足情報」へ再定義し、current state の主要記録は snapshot とした。(9)USER による merge 等の後は `NEXT_CHATGPT_GATE_OWNS_RECONCILIATION = YES` とし、「誰かがやるだろう」を禁止した。(10)drift audit を定義し、P0/P1 open Issue を必須、P2/P3 は queue 候補・branch/PR 保有・human decision 待ちのみとして**全件重走査を行わない**(Stabilization Sprint の速度を落とさない)。証拠の採用順序は chatgpt_collaboration_protocol.md 3.6節を正本として参照し複製していない。あわせて §5 / §6 へ正本の所在を1行ずつ追加した。**docs のみの変更であり、判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない** |

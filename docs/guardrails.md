# ガードレール

生成コードとエージェントを安全に動かすための制限の一覧（#16、design-doc の Security / Privacy / Logging）。
「何を守るか → どこで実装しているか → どう確認しているか」の順に書く。

## 1. LLM の認証と課金

| 守ること | 実装 | 確認 |
|---|---|---|
| 意図せず従量課金や別の接続先にならない | `adp.llm.settings.LLMSettings.subprocess_env`: 認証・接続先の環境変数（`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK` / `VERTEX` / `FOUNDRY` / `GATEWAY` など）を空にして Claude Code を起動する。リポジトリの `.env` に `ANTHROPIC_API_KEY` を書いたときだけ API キーを使う | `tests/llm/test_settings.py`。空の値でログイン認証になることを実機で確認（#8） |
| ユーザーや組織の設定を持ち込まない | `setting_sources=[]`、`--strict-mcp-config`（CLAUDE.md、ユーザー設定、claude.ai のコネクタを読み込まない） | #8 のレビューで CLI 引数を確認 |

**対象外**: 組織の管理ポリシー（managed settings の `apiKeyHelper` など）は CLI が常に読み込むため、アプリからは制御できない。

## 2. インタビューエージェント

| 守ること | 実装 |
|---|---|
| ファイル操作やコマンド実行ができない | 組み込み tool を無効にする（`tools=[]`）。許可するのは BEAM 編集とソースカタログの MCP tool だけ |
| ソースデータやユーザー入力の中の指示に従わない | カタログの結果は `<source_data>` タグで囲み、データとして扱うよう明示する（閉じタグは無害化）。プロンプトにも明記 |
| 不正な成果物を作らない | tool による編集は「コピー → 変更 → 全体の再検証 → 差し替え」。検証に失敗した変更は反映しない |
| 成果物から任意の SQL を差し込まない（v2） | ソースの式（`SourceRef.expr`）は、そのテーブルの列だけを使う 1 行ごとの式に限る（`{ }`（Jinja）と `\`（Jinja の文字列のエスケープ）を拒否し、sqlglot で解析してサブクエリ・他のテーブル・集計・ウィンドウ関数・複数の文を拒否。#46。関数は DuckDB の名前の許可リスト（`adp.beam.models.ALLOWED_FUNCTIONS`。条件・NULL・型変換・数値・日付・文字列の基本的なもの）だけで、`getenv`・`read_csv`・`current_setting`・`query` などは拒否。AND / OR・比較などの演算子は関数として扱わない。#79）。決まった手順で作る生成物を書くときも、生成器が出す形（`ref` / `source` / `dim_key` / `unknown_key` / `Dimension`）以外の Jinja があれば書かない（自己修復エージェントの書き込みは `WorkspaceGuard` が `env_var`・`run_query` などを拒否する）（BEAM の説明文などから `env_var`・`run_query` を差し込ませない。`adp.generation.project.unsafe_jinja`）。確定前に設計の課題（結合の経路が無いなど）も「確定までに残っている課題」に出して直させる |
| 取り込みのエージェント（v3）が勝手に書き換えない | 使える tool はソースカタログの読み取りと `submit_proposal`（オントロジーへの追加案を出すだけ）。組み込み tool は無い。案は既存のオントロジーに「追加だけ」で適用し、型・参照・源泉との照合（関係の列は一方の源泉にあるもう一方のキー）を通ったものだけ受け付ける。ファイルを書くのは確定後のジョブ（決まった手順）で、ユーザーが画面で確認・修正した案を使う。図（Mermaid）のラベルは `%%{init}%%`・コメント・構文の文字を実体参照にする（#67）。確認: `tests/onboarding/test_onboarding.py`、`tests/ontology/test_ontology.py` |

## 3. ソースカタログ（DuckDB）

| 守ること | 実装 | 確認 |
|---|---|---|
| ソースを書き換えない、外部のファイルに触れない | `read_only=True` かつ `enable_external_access=False` で接続 | `tests/catalog/test_catalog.py`（悪意のあるテーブル名でファイルが作られない） |
| SQL インジェクション | 呼び出し側の名前は識別子のみ許可し、対象スキーマに限定。DB から読んだ名前も必ず `_q()` でクォートする | 同上 |
| エラーメッセージからの情報漏れ | DuckDB の例外は定型文に置き換えて返す | 同上 |
| 画面からの参照（源泉のプロファイル、#62） | `GET /sources/{table}/profile` もエージェントと同じ Catalog（同じ識別子・スキーマの制限、読み取り専用）を通す。DuckDB の例外は定型文の 503。NULL 以外の値がすべて違う列（ID・メールアドレスなど）は、画面で上位の値を出さない | `tests/test_datasets.py`、`SourceProfile.test.tsx` |

## 4. 生成エージェント（自己修復）

| 守ること | 実装 | 確認 |
|---|---|---|
| 作業はリポジトリ内の作業ツリーだけ | `adp.pipeline`: `.adp/worktrees/<branch>` で作業する。開発中のチェックアウトや DuckDB には触れない（検証用の DB はコピーして使う） | `tests/test_pipeline.py` |
| ファイル操作は作業ディレクトリ内だけ | `adp.generation.guard.WorkspaceGuard`（PreToolUse フック）。`~` を展開し、パスを解決して作業ディレクトリの外なら拒否。Glob のパターンも検査 | `tests/generation/test_repair.py`、実機確認（#14） |
| 書き換えられるファイルを限定 | 書けるのは .sql / .yml / .md だけ。`dbt_project.yml`・`profiles.yml`・`packages.yml` と Python ファイルは不可。チャート（`charts/`、`chart_specs/`）はパイプラインが書き出すので、エージェントは書けない（#42）。クレンジングの定義（`cleansing/`）と、定義から生成したクレンジング層（先頭に `adp.cleansing により生成` の印がある stg）も書けない（#68）。書けるパスは ASCII だけにし、ファイル名・ディレクトリ名は大文字・小文字を区別せずに判定する（macOS のファイルシステムで `Charts/` や `chartſ/`（ſ は s と同じに扱われる）から迂回させない）。書き込み後の内容に `env_var` / `environ` / `getenv` を含むものは不可（環境変数の秘密情報の持ち出し防止。Edit は既存の内容に置換を適用した結果で判定）。Python モデルがあれば検証で失敗 | 同上 |
| 実行できるコマンドを限定 | Bash は `dbt`（parse / build / compile / test / ls）と `mf`（validate-configs / query / list）だけ。オプションも許可リスト（パスを取るものは不可）、シェルのメタ文字は禁止。`permission_mode=dontAsk` で許可リスト外の操作は確認せず拒否 | 同上 |
| dbt から外部のファイル・ネットワークに触れない | `warehouse/profiles.yml` で DuckDB の `enable_external_access: false`（read_text / COPY TO を禁止。モデルや hook から戻せない） | `test_duckdb_external_access_is_disabled` |
| dbt Charts から外部に触れない（v2） | `dbt_charts.yml` は書き換え不可（独自の duckdb ソースで外部アクセスを戻せるため）。ソースは dbt プロファイル経由の `warehouse` だけにし、DuckDB の外部アクセス禁止が効く。ボードの元ファイルは許可リストで検査し（SQL・extends・ボードの入れ子・HTML・地図・`type: http`・URL は不可。絞り込みは構造化した filters だけで、値に Jinja の記号は不可。`charts/` には生成したボード以外を置けない）、生成時と `make dbt-build` で拒否する（#41 / #42）。`dct serve` は Host を検証しないので使わない。描画画面は backend のプロセス内で描き（ADR 0003）、画面からは指標名・切り口名・粒度・期間だけを受け取る（Semantic Layer の一覧と照合し、ボードは同じ許可リストを通す）。プロセス内の MetricFlow は SQL を作るだけで DB に接続しない | `test_dbt_charts.py`、`test_charts.py`、`test_chart_view.py` |
| クレンジングの定義から任意の SQL を入れない（v3） | 定義は構造化された項目だけ（Pydantic で余計なキーを拒否）。識別子は英小文字、型変換は許可した型、タイムゾーンは名前の形式、値は文字列リテラルとしてエスケープし、`{` `}` `\` と制御文字（Jinja・エスケープ）を拒否。生成物は決定的（LLM を使わない） | `tests/cleansing/test_cleansing.py` |
| テストを消して通さない | 修正前後で `dbt ls` のテスト一覧（名前と重大度）を比べ、削除・無効化・重大度の低下があれば失敗にする。プロンプトでも禁止 | `test_weakening_tests_is_detected` |
| 終わらない処理 | 検証の dbt / mf は 600 秒、git / gh は 300 秒で打ち切る。エージェント自体は時間ではなく手番数（修正 40、インタビュー 30）で制限する | — |
| ログやファイルの中の指示に従わない | 検証ログは `<verification_log>` で囲んで渡し（閉じタグと色のエスケープは無害化）、プロンプトで「データとして扱う」と明示する。万一従っても、上の許可リストの範囲でしか操作できない | — |
| 直らないものを無理に通さない | Sonnet 2 回 → Opus 1 回で直らなければ draft PR にして人に渡す | `tests/generation/test_repair.py` |

## 5. git / GitHub

| 守ること | 実装 |
|---|---|
| 正しいアカウントで push・PR 作成する | origin の所有者（例: daikono313）の gh トークンを使う。gh のアクティブなアカウントには依存しない |
| 既存のブランチを壊さない | 新しいブランチだけを作る（ローカルかリモートに同名があれば `-2` などを付ける）。force push はしない。既存ファイルは上書きしない（生成は全部書くか何も書かないか）。push 前に失敗したら作業ブランチを片付ける |
| PR を作れないのに push しない | 最初に gh のトークンを確認してから作業を始める |
| 公開リポジトリの PR に余計なものを載せない | PR 本文の LLM・ユーザーの文章は、`#数字` や `@名前` を無効化し、ローカルの絶対パスを伏せる。本文はファイル経由で渡し、上限で切る |
| 生成 PR を勝手にマージしない | 生成 PR は人がレビューしてマージする（design-doc の Non-goals） |
| 一覧で PR の状態を読むだけにする（v3） | 一覧画面（#73）は `gh pr view <url> --json state` で読むだけ。URL は GitHub の PR の形（`https://github.com/<owner>/<repo>/pull/<n>`）に限り、オプションとして解釈されない。待つのは 15 秒まで（残りは「不明」）、結果はキャッシュする |

## 6. 秘密情報とログ

| 守ること | 実装 |
|---|---|
| 秘密情報をリポジトリに入れない | `.env` と `.adp/` は git の追跡対象外。`scripts/check_secrets.sh`（CI と `make lint`） |
| 使用量を把握する | `.adp/logs/llm.jsonl` に、用途・認証方式・モデル・所要時間・入力（キャッシュ込み）と出力のトークン数・概算コストを記録する。中断・異常終了したターンも記録する。10MB を超えたら `.jsonl.1` に退避する |

### 会話の本文が残る場所

`llm.jsonl` には本文を書かないが、次の場所には会話（プロンプト・回答・ソースデータのサンプル・検証ログ）がそのまま残る。いずれもローカルのみで、git の追跡対象外。

| 場所 | 内容 | 保持 |
|---|---|---|
| `.adp/app.db` | セッションの会話と BEAM 成果物 | 削除しない限り残る |
| `~/.claude/projects/<作業ディレクトリ>/*.jsonl` | Claude Code CLI が保存する各エージェント実行の全記録（インタビューの再開に必要なので無効にできない） | Claude Code の設定に従う |
| `.adp/evals/<日時>/*.json`、`.adp/eval-*.log` | eval の会話・tool 呼び出し・成果物 | 削除しない限り残る |

実データを扱う場合は、これらの保存先も機密データとして扱う。

### 構造化ログ（design-doc の Logging）

design-doc にある `.adp/logs/` へのイベントの構造化ログ（セッション開始・確定、tool 呼び出し、生成の各段階など）は MVP では作らない。生成の各段階は `.adp/app.db` のジョブ（steps）に、LLM の使用量は `llm.jsonl` に残る。

## 7. ネットワーク

- API サーバーは既定で `127.0.0.1` で起動する（MVP はローカルの 1 ユーザー。認証は無い）。外部に公開しない。
- DNS リバインディング対策として、Host が `localhost` / `127.0.0.1` 以外の要求は拒否する（TrustedHostMiddleware）。
- **受け入れているリスク**: 認証が無いので、同じ PC 上の他のプロセスは API を呼べる。

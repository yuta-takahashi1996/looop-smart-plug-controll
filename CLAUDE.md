# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクトの現状

Phase 5（Dry Run、[docs/15-roadmap.md](docs/15-roadmap.md)）まで実装済み。
- 料金予報：`app/providers/looop.py` がでんき予報の `/api/prices` から電源料金単価を取得し、`app/services/forecast.py` が検証・保存する。でんき予報の区分（日和・注意報・警報）も `level` として保存する。請求単価は「電源料金単価＋`price_adjustments` の加算額」で都度算出し、表示にのみ使う（[docs/06-forecast-provider.md](docs/06-forecast-provider.md) §6.5）。
- SwitchBot：`app/clients/switchbot.py`（状態取得・turnOn/turnOff）。`app/services/controller.py` の `PlugMonitor` はプラグ状態の読み取りのみで、コマンドはまだ送信しない（Control Service 本体は Phase 6）。実機の確認は `scripts/switchbot_check.py`。
- 定期ジョブ：`app/services/scheduler.py`（APScheduler）。`APP_ENV=test` では起動しない。
- スケジュール：`app/services/optimizer.py` がでんき予報の注意報・警報から `control_schedules` を生成する（`plan_day` は DB 非依存の計画、`regenerate` は手動変更・開始済みを残して作り直す、`update_schedule` は手動変更の検証付き更新）。予報の取得後・設定保存時・画面からの再生成で再生成する。
- 実行：`app/services/controller.py` の `ControlService` が毎分の制御サイクル（`run_cycle`）で有効なスケジュールを実行し、`control_logs` に記録する。実行時の安全判定は `app/services/safety.py`。現在は Dry Run のみ（`DRY_RUN=false` でもコマンドは送らない。実制御は Phase 6）。
- 画面：すべて実データ（モックは削除済み）。手動のプラグ操作・自動制御の開始／停止ボタンは Phase 6 で有効にする。

`docs/` 配下の各ファイルは単なる背景説明ではなく正式な仕様書なので、何かを組み立てる前に必ず読むこと。ディレクトリ構成・DBスキーマ・環境変数・制御ロジックまで詳細に定義されている。実装時は以下の構成に従い、新たな構成を考案しないこと。

## 開発コマンド

```bash
uv sync                                  # 依存関係のインストール（Python 3.12、.venv）
uv run pytest                            # テスト全体
uv run pytest tests/test_api.py::test_healthz   # 単一テスト
scripts/tailwind.sh                      # app/static/css/app.css を生成（初回は bin/ に Standalone CLI を取得）
scripts/tailwind.sh --watch              # テンプレート編集中の監視ビルド
uv run alembic upgrade head              # マイグレーション適用（DATABASE_URL、既定は data/app.db）
uv run alembic revision --autogenerate -m "..."  # マイグレーション作成
uv run uvicorn app.main:app --reload --port 8080 # ローカル起動（事前に tailwind.sh で CSS 生成）
uv run python scripts/hash_password.py   # WEB_PASSWORD_HASH の生成（ログインに必須。端末から実行）
uv run python scripts/switchbot_check.py devices|status          # SwitchBot の確認（読み取りのみ）
uv run python scripts/switchbot_check.py verify-onoff --confirm  # 実機の ON/OFF 確認（プラグが実際に切り替わる）
uv run python scripts/jackery_check.py [devices|status|raw]      # ポータブル電源の残量確認（読み取りのみ）

cp .env.example .env                     # 初回のみ
docker compose up -d --build             # http://127.0.0.1:8080
docker compose logs -f
docker compose down
```

- 依存の追加は `uv add <pkg>`（開発用は `uv add --dev <pkg>`）で行い、`uv.lock` もコミットする。
- `app/static/css/app.css` は生成物のためコミットしない（Docker ビルド時に生成）。HTMX / Chart.js は `app/static/js/` に同梱している（宅内LANでCDNに依存しないため）。
- テストは `tests/conftest.py` でインメモリ SQLite（`DATABASE_URL=sqlite://`）を使う。外部通信はしない（Looop は `FakeProvider` を `app.state.forecast_provider` に差し替え、HTTP クライアントは `httpx.MockTransport` を使う）。
- DB の日時は `UTCDateTime`（`app/repositories/database.py`）で UTC に揃えて保存し、テンプレートでは `|local("%H:%M")` フィルターで表示用のタイムゾーンへ変換する。
- コンテナは起動時に `alembic upgrade head` を実行してから uvicorn を起動する。
- 共有の `Jinja2Templates` は `app/templating.py` にある。HTMX の部分テンプレートと Jinja マクロ（`ui.html`）は `app/templates/components/` に置く。
- 画面ルーターは `app/main.py` で `require_login` 依存付きで登録される。POST エンドポイントには `Depends(verify_csrf)` を付ける（HTMX は `<body hx-headers>` で `X-CSRF-Token` を自動送信、通常フォームは hidden の `csrf_token`）。
- ログインには `WEB_PASSWORD_HASH` と `SESSION_SECRET` が必要（未設定時はログイン不可）。接続元は `ALLOWED_NETWORKS` で制限される。テストでは `tests/conftest.py` の `logged_in_client` fixture を使う。

## Pull Request 作成規約

- タイトルは対応する Issue のタイトルに合わせる（例：Issue「Phase 1: 環境構築」→ PR「Phase 1: 環境構築」）。
- アサインはリポジトリオーナー（GitHub アカウント `yuta-takahashi1996`）にする（`gh pr create --assignee yuta-takahashi1996`）。git の `user.name`（`yutaman04`）は GitHub のアカウント名ではないため指定できない。
- 本文は以下のテンプレートに従う。対応 Issue は「概要」に `Closes #<番号>` で記載する。

```markdown
## 概要

## 実装内容

## 確認したこと

## レビュー観点

## その他
```

## システム概要

Looopでんきは30分単位で電力単価が変動する市場連動型プランを提供している。本システムはLooopの料金予報を自動取得し、蓄電池の充電と商用電源（グリッド）利用を切り替えるタイミングを判断し、SwitchBotスマートプラグのON/OFFを自動で切り替える。これにより手動でのタイマー設定作業を不要にする。Web UIは初期リリースの必須機能（後付けの追加機能ではない）であり、現在の料金・制御状態・スケジュール・推定節約額を表示し、手動操作も可能にする。

## 想定アーキテクチャ（[docs/03-architecture.md](docs/03-architecture.md)より）

単一プロセスのモノリス構成（FastAPIがWeb UIとスケジューラーの両方を同一プロセス内で提供し、スケジューラーの二重起動を防ぐ）。

```
Looop予報ページ → Forecast Provider → Forecast Service → SQLite
                                              │
                                              ▼
                                Optimization Engine ← System Settings
                                              │
                                              ▼
                                   Control Scheduler
                                              │
                                              ▼
Web UI（手動操作） ──────────────→  Control Service  ──→ SwitchBot Client → SwitchBot API
                                              │
                                              ▼
                                       Control Logs → SQLite
```

重要な設計ルール：**自動制御・手動制御（Web UI経由）のいずれも単一のControl Serviceを経由する**。これにより、スケジューラーとユーザー操作が競合することを防いでいる。

`app/` 配下の各モジュールの責務（予定）：
- `providers/looop.py` — でんき予報ページが利用する `GET /api/prices?select_area=01` から電源料金単価を取得する（公式APIはない。[docs/06-forecast-provider.md](docs/06-forecast-provider.md) §6.2）。お知らせ一覧の取得も担う。取得方式が変更された場合に差し替えやすいよう独立させる。
- `services/forecast.py` — 取得した料金データを信頼する前に検証・正規化する（エリア一致、日付の正しさ、48コマ全取得、連続性、重複・欠損なし）。
- `services/optimizer.py` — でんき予報の注意報・警報と設定値から OFF スケジュールを生成し、有効・無効を判定する（下記「スケジュールの考え方」参照）。
- `services/scheduler.py` — 定期ジョブ（料金再取得、スケジュール実行）のタイミングを管理する。
- `services/controller.py` — Control Service本体。AUTO/MANUAL/SAFETYのすべての状態変更を一元管理する唯一の経路。
- `services/safety.py` — データや通信が信頼できない場合のフェイルセーフ動作を担保する。
- `clients/switchbot.py` — SwitchBot OpenAPI v1.1クライアント（`GET /devices/{id}/status`、`POST /devices/{id}/commands` では明示的な `turnOn`/`turnOff` のみを使用し、**`toggle`は使用しない**。toggleは期待状態との照合ができないため）。

## 制御状態（[docs/07-control-logic.md](docs/07-control-logic.md) §7.3）

状態：`GRID`（プラグON）、`BATTERY`（プラグOFF）、`SAFE`（安全側への復帰を強制）、`MANUAL`（ユーザー優先。自動制御は停止するが安全制御は継続）。

```
[*] → SAFE → GRID ⇄ BATTERY
異常検知時: GRID/BATTERY → SAFE
手動操作時: GRID/BATTERY → MANUAL
自動制御再開時: MANUAL → SAFE
```

中核となる安全方針（[docs/14-safety.md](docs/14-safety.md) §14.1）：**節電よりも安定した電源供給を優先する**。具体的には、料金予報の取得失敗やデータ欠損時は新規の自動OFFを禁止する（不正確なデータでOFFを計画しない）。ON/OFFコマンド送信後は必ず状態を再取得し期待状態と照合する（照合できなければ成功扱いにしない）。スケジュール失効または運用終了時刻を過ぎた場合はON復帰を強制する（残量を取得できている間の、有効なでんき警報のスケジュール中を除く）。

## スケジュールの考え方（[docs/07-control-logic.md](docs/07-control-logic.md) §7.1・§7.4〜7.6）

- OFF スケジュールは、でんき予報の区分（`forecast_prices.level`）の注意報・警報が連続する区間から生成する。独自の価格差による最適化はしない。
- でんき警報のスケジュールは常に有効、でんき注意報は `AUTO_ENABLE` が有効なときのみ自動で有効。それ以外はユーザーが個別に有効にしない限り実行しない。ユーザーは個別に有効・無効を切り替え、他のスケジュールと矛盾しない範囲で時間を変更できる。手動変更は再生成で上書きしない。
- 制御の判断・推定節約額は**電源単価**で行う。請求単価（電源単価＋`price_adjustments`）は画面表示専用（電源単価／請求単価の切り替え表示）。
- ポータブル電源（Jackery 1000 New）の残量は Jackery クラウドの非公式 API から取得する（`app/clients/jackery.py`、[docs/08-switchbot-api.md](docs/08-switchbot-api.md) §8.4）。残量と消費電力（出力W）はスケジュール開始時と OFF 中5分ごとに取得し、次の取得時の予測残量が `BATTERY_MIN_PERCENT`（初期値 5%）を下回る前に、でんき警報中でも ON に戻す（[docs/07-control-logic.md](docs/07-control-logic.md) §7.7、Phase 6 で実装）。取得専用アカウントを使う（同時ログイン1台の制限のため）。
- 注意報のスケジュールは `MAX_CONTINUOUS_OFF_MINUTES`、`MAX_DAILY_OFF_MINUTES`、運用時間で保守的に制限する。警報のスケジュールは、残量を取得できる間に限りこれらの制限を超えてよく（`MIN_SWITCH_INTERVAL_MINUTES` は適用）、残量を取得できない間は注意報と同じ制限を実行時に適用する。

## データモデル（SQLite、[docs/10-database.md](docs/10-database.md)）

7テーブル構成：`forecast_prices`（30分単位の電源料金単価）、`price_adjustments`（請求単価への加算額。適用期間付き）、`control_schedules`（予定されたON/OFF計画。理由と推定節約額を含む）、`control_logs`（実際の全操作履歴。`AUTO`/`MANUAL`/`SAFETY`のいずれかに分類し、操作前・要求・確認後の状態を記録）、`system_settings`（key/value。**秘匿情報は保存しない** — 認証情報は環境変数のみで管理する）、`system_events`、`daily_summaries`。

## Dry Runモード

`DRY_RUN=true` が必須の初期値（[docs/13-environment-variables.md](docs/13-environment-variables.md)、[docs/14-safety.md](docs/14-safety.md) §14.3）。Dry Run中はパイプライン全体（予報取得→最適化→スケジュール生成）は動作するが、SwitchBotへの実コマンドは送信されず、「実行予定だった操作」と推定節約額のみをログに記録する。Dry Runの解除は、デプロイ時の環境変数変更だけでなく、Web UI上での明示的な確認操作を必要とする。

## 従うべきディレクトリ構成（[docs/11-directory-structure.md](docs/11-directory-structure.md)）

```
app/
├── main.py, config.py
├── api/            # dashboard.py, forecast.py, control.py, history.py, settings.py
├── providers/       # base.py, looop.py
├── clients/          # switchbot.py, jackery.py
├── services/         # forecast.py, optimizer.py, scheduler.py, controller.py, safety.py
├── models/            # forecast.py, schedule.py, settings.py
├── repositories/      # database.py
├── templates/          # Jinja2 + HTMXの部分テンプレート、components/
└── static/              # css/, js/
tests/                    # test_forecast.py, test_optimizer.py, test_controller.py, test_safety.py, test_api.py
data/  logs/
```

## 技術スタック（[docs/02-tech-stack.md](docs/02-tech-stack.md)）

Python 3.12以降、FastAPI、Jinja2 + HTMX（SPAビルドステップなし。サーバーサイドレンダリング＋部分更新）、Tailwind CSS、Chart.js、APScheduler、httpx、Pydantic、SQLAlchemy + Alembic、SQLite、pytest、Docker Compose。

## 運用上の注意点

- 初期リリースのWeb UIは宅内LAN限定とし、認証を実装する。状態変更リクエストにはCSRF対策が必要。SwitchBotの認証情報をブラウザに送信してはならない。
- SwitchBotの認証情報（`SWITCHBOT_TOKEN`、`SWITCHBOT_SECRET`、`SWITCHBOT_DEVICE_ID`）は環境変数のみで管理し、コミットや`system_settings`への保存を禁止する。
- 画面更新のたびにSwitchBot APIを直接呼び出さず、SQLiteに保存済みのデータを参照する（[docs/05-web-ui.md](docs/05-web-ui.md) §5.9）。
- 正常終了時はプラグをONへ復帰させるべきだが、クラッシュやホスト停止時はこれを保証できないため、本アプリに依存しないプラグ側のフォールバックスケジュールを本番運用前に用意しておく想定（[docs/09-scheduling.md](docs/09-scheduling.md) §9.3、[docs/14-safety.md](docs/14-safety.md) §14.4）。

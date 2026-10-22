# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクトの現状

Phase 1（環境構築、[docs/15-roadmap.md](docs/15-roadmap.md)）完了。FastAPI アプリの骨格、設定読み込み（`app/config.py`）、SQLAlchemy + Alembic（テーブルは未作成）、Jinja2/Tailwind/HTMX の基盤、Docker Compose 構成が存在する。Web UI は仮のダッシュボード（`/`）と `/healthz` のみ。

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

cp .env.example .env                     # 初回のみ
docker compose up -d --build             # http://127.0.0.1:8080
docker compose logs -f
docker compose down
```

- 依存の追加は `uv add <pkg>`（開発用は `uv add --dev <pkg>`）で行い、`uv.lock` もコミットする。
- `app/static/css/app.css` は生成物のためコミットしない（Docker ビルド時に生成）。HTMX / Chart.js は `app/static/js/` に同梱している（宅内LANでCDNに依存しないため）。
- テストは `tests/conftest.py` でインメモリ SQLite（`DATABASE_URL=sqlite://`）を使う。
- コンテナは起動時に `alembic upgrade head` を実行してから uvicorn を起動する。
- 共有の `Jinja2Templates` は `app/templating.py` にある。

## Pull Request 作成規約

- タイトルは対応する Issue のタイトルに合わせる（例：Issue「Phase 1: 環境構築」→ PR「Phase 1: 環境構築」）。
- アサインはリポジトリオーナー（`yutaman04`）にする（`gh pr create --assignee yutaman04`）。
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
- `providers/looop.py` — Looopの料金予報ページを取得する（公式APIの利用可否は未確認のため [docs/06-forecast-provider.md](docs/06-forecast-provider.md) §6.2 で調査が必要）。取得方式が変更された場合に差し替えやすいよう独立させる。
- `services/forecast.py` — 取得した料金データを信頼する前に検証・正規化する（エリア一致、日付の正しさ、48コマ全取得、連続性、重複・欠損なし）。
- `services/optimizer.py` — 料金予報と設定値から、固定の閾値ではなく価格差（下記「経済性」参照）に基づいてON/OFF計画を生成する。
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

中核となる安全方針（[docs/14-safety.md](docs/14-safety.md) §14.1）：**節電よりも安定した電源供給を優先する**。具体的には、料金予報の取得失敗やデータ欠損時は新規の自動OFFを禁止する（不正確なデータでOFFを計画しない）。ON/OFFコマンド送信後は必ず状態を再取得し期待状態と照合する（照合できなければ成功扱いにしない）。スケジュール失効または運用終了時刻を過ぎた場合はON復帰を強制する。

## 経済性の考え方（[docs/07-control-logic.md](docs/07-control-logic.md) §7.2）

```
推定削減額 =
    （高額時間帯に回避できる電力購入費）
    − （充電に必要な電力購入費）
    − （推定充放電コスト）

必要充電電力量 = 供給電力量 ÷ 往復効率
```

初期リリースでは蓄電池の残量をハードウェアから取得しない。残量不明を前提に、OFF継続時間を保守的に制限する（`MAX_CONTINUOUS_OFF_MINUTES`、`MAX_DAILY_OFF_MINUTES`、`MIN_SWITCH_INTERVAL_MINUTES` 等の設定値で制御）。

## データモデル（SQLite、[docs/10-database.md](docs/10-database.md)）

6テーブル構成：`forecast_prices`（30分単位の料金予報）、`control_schedules`（予定されたON/OFF計画。理由と推定節約額を含む）、`control_logs`（実際の全操作履歴。`AUTO`/`MANUAL`/`SAFETY`のいずれかに分類し、操作前・要求・確認後の状態を記録）、`system_settings`（key/value。**秘匿情報は保存しない** — 認証情報は環境変数のみで管理する）、`system_events`、`daily_summaries`。

## Dry Runモード

`DRY_RUN=true` が必須の初期値（[docs/13-environment-variables.md](docs/13-environment-variables.md)、[docs/14-safety.md](docs/14-safety.md) §14.3）。Dry Run中はパイプライン全体（予報取得→最適化→スケジュール生成）は動作するが、SwitchBotへの実コマンドは送信されず、「実行予定だった操作」と推定節約額のみをログに記録する。Dry Runの解除は、デプロイ時の環境変数変更だけでなく、Web UI上での明示的な確認操作を必要とする。

## 従うべきディレクトリ構成（[docs/11-directory-structure.md](docs/11-directory-structure.md)）

```
app/
├── main.py, config.py
├── api/            # dashboard.py, forecast.py, control.py, history.py, settings.py
├── providers/       # base.py, looop.py
├── clients/          # switchbot.py
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

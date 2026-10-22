# Looop Power Controller

Looopでんきの市場連動型プラン（30分単位で変動する電力単価）を利用し、電力単価が安い時間帯に蓄電池を充電し、高い時間帯には商用電源の利用を止めるよう、SwitchBotスマートプラグのON/OFFを自動制御するシステム。手動でのタイマー設定作業を不要にする。Web UIから料金・制御状態・運転予定を確認・操作できる。

## プロジェクトの現状

Phase 1（環境構築）完了。Docker Compose で起動し、Web UI（仮のダッシュボード）にアクセスできる。詳細設計は [`docs/`](./docs) 配下を参照。

## 起動方法

```bash
cp .env.example .env        # 初期値は DRY_RUN=true
docker compose up -d --build
```

http://127.0.0.1:8080 にアクセスする。

ローカル開発（[uv](https://docs.astral.sh/uv/) が必要）：

```bash
uv sync
scripts/tailwind.sh         # CSS 生成
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8080
uv run pytest
```

## 基本方針

- Dockerによるコンテナ運用を基本とする。
- 特定のホストOSやハードウェアへの依存を避ける。
- バックエンドはPythonで実装する（FastAPIベースのモノリス構成）。
- Web UIは初期リリースの必須機能とする。
- 自動制御と手動制御の競合を防止する。
- 料金データが不正または取得できない場合は、安全側の制御を優先する。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/01-overview.md](docs/01-overview.md) | プロジェクト概要・目的・基本方針 |
| [docs/02-tech-stack.md](docs/02-tech-stack.md) | 技術スタック |
| [docs/03-architecture.md](docs/03-architecture.md) | システム構成 |
| [docs/04-requirements.md](docs/04-requirements.md) | 機能要件 |
| [docs/05-web-ui.md](docs/05-web-ui.md) | Web UI設計 |
| [docs/06-forecast-provider.md](docs/06-forecast-provider.md) | Looopでんき予報取得設計 |
| [docs/07-control-logic.md](docs/07-control-logic.md) | 制御ロジック |
| [docs/08-switchbot-api.md](docs/08-switchbot-api.md) | SwitchBot API連携 |
| [docs/09-scheduling.md](docs/09-scheduling.md) | スケジューリング設計 |
| [docs/10-database.md](docs/10-database.md) | データベース設計 |
| [docs/11-directory-structure.md](docs/11-directory-structure.md) | ディレクトリ構成 |
| [docs/12-docker.md](docs/12-docker.md) | Docker構成 |
| [docs/13-environment-variables.md](docs/13-environment-variables.md) | 環境変数 |
| [docs/14-safety.md](docs/14-safety.md) | 安全設計・例外処理 |
| [docs/15-roadmap.md](docs/15-roadmap.md) | 実装フェーズ |
| [docs/16-future-scope.md](docs/16-future-scope.md) | 将来的な拡張 |

## 初期リリースの完成条件
1. Docker環境でシステムが稼働する。
2. Web UIから現在の料金・制御状態を確認できる。
3. 当日のLooopでんき予報を自動取得できる。
4. 料金予報を基に制御スケジュールを生成できる。
5. SwitchBot APIを通じてスマートプラグを自動制御できる。
6. Web UIから手動操作ができる。
7. 制御履歴と推定節約額を確認できる。
8. 異常時には可能な限り安全状態へ復帰する。
9. 運用終了時にはスマートプラグをONへ復帰させる。
10. 特定のホストOSや蓄電池製品に依存せず運用できる。

**最終目標：システムを起動するだけで、その日の電力単価に応じたスマートプラグのON/OFF制御が自動実行され、Web UIから料金・制御状態・運転予定を一元管理できること。**

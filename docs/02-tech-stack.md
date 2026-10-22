# 技術スタック

| 分類 | 採用技術 |
|---|---|
| バックエンド | Python 3.12以降 |
| Webフレームワーク | FastAPI |
| テンプレート | Jinja2 |
| UI更新 | HTMX |
| スタイリング | Tailwind CSS |
| グラフ | Chart.js |
| スケジューラー | APScheduler |
| HTTP通信 | httpx |
| データ検証 | Pydantic |
| ORM | SQLAlchemy |
| マイグレーション | Alembic |
| データベース | SQLite |
| テスト | pytest |
| コンテナ管理 | Docker Compose |

初期リリースではSPA構成を採用せず、FastAPIを中心とするモノリシック構成とする。

アプリケーション内ではWeb、外部API連携、料金最適化、制御処理の責務を分離する。

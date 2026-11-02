# 環境変数

```env
# Application
APP_ENV=development
TZ=Asia/Tokyo
DRY_RUN=true

# Web UI
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_USERNAME=admin
WEB_PASSWORD_HASH=
SESSION_SECRET=
ALLOWED_NETWORKS=127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

# Operation
OPERATION_START=09:00
OPERATION_END=18:00

# Looop
LOOOP_AREA=hokkaido
FORECAST_RETRY_COUNT=3

# SwitchBot
SWITCHBOT_TOKEN=
SWITCHBOT_SECRET=
SWITCHBOT_DEVICE_ID=

# Jackery（ポータブル電源の残量取得。取得専用アカウント）
JACKERY_ACCOUNT=
JACKERY_PASSWORD=
JACKERY_DEVICE_ID=
# 残量がこの値（%）以下になったら ON に戻す
BATTERY_MIN_PERCENT=5

# Schedule
# でんき注意報のスケジュールを自動で有効にする（でんき警報は設定によらず有効）
AUTO_ENABLE=false
ROUND_TRIP_EFFICIENCY=0.85

MAX_CONTINUOUS_OFF_MINUTES=120
MAX_DAILY_OFF_MINUTES=180
MIN_SWITCH_INTERVAL_MINUTES=30

ESTIMATED_AVERAGE_POWER_W=150

# Logging
LOG_LEVEL=INFO
```

初期状態は必ずDry Runとする。

## Web UI の認証・アクセス制御

| 変数 | 説明 |
|---|---|
| WEB_USERNAME | Web UI の管理ユーザー名 |
| WEB_PASSWORD_HASH | ログインパスワードの scrypt ハッシュ。`uv run python scripts/hash_password.py` で生成する |
| SESSION_SECRET | セッションCookieの署名鍵（十分に長いランダム文字列） |
| ALLOWED_NETWORKS | アクセスを許可する接続元（カンマ区切りのCIDR）。範囲外からの接続は 403 とする |

`WEB_PASSWORD_HASH` または `SESSION_SECRET` が未設定の場合、Web UI にはログインできない（認証なしでは動作させない）。

Web UI から変更した制御条件（運用時間、最適化パラメータ）は `system_settings` に保存され、環境変数の値より優先される。環境変数の値は未保存の項目の初期値として使う。

## SwitchBot

`SWITCHBOT_TOKEN` / `SWITCHBOT_SECRET` は SwitchBot アプリの「プロフィール → 設定 → アプリバージョンを10回タップ → 開発者向けオプション」で取得する。`SWITCHBOT_DEVICE_ID` は `uv run python scripts/switchbot_check.py devices` で確認できる。未設定の場合、プラグ状態は「未確認」と表示し、SwitchBot API を呼び出さない。

`APP_ENV=test` の場合、スケジューラー（予報取得・プラグ状態確認などの定期ジョブ）は起動しない。

## スケジュール

`AUTO_ENABLE`、運用時間、OFF時間の制限、往復効率、想定平均負荷は設定画面からも変更でき、保存した値が環境変数より優先される。`ROUND_TRIP_EFFICIENCY` と `ESTIMATED_AVERAGE_POWER_W` は推定節約額の算出にのみ使う。

## Jackery

`JACKERY_ACCOUNT` / `JACKERY_PASSWORD` には、本アカウントからポータブル電源を共有した**取得専用のアカウント**を設定する（同時ログインは1台までのため、本アカウントを設定するとスマホアプリがログアウトされる）。`JACKERY_DEVICE_ID` は `uv run python scripts/jackery_check.py devices` で確認でき、共有した機器が1台なら空欄でよい。未設定の場合、残量は「不明」として扱う（docs/07-control-logic.md §7.7）。

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

# Optimization
MIN_SAVING_SPREAD=20
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

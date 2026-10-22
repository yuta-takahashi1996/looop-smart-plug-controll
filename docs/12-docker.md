# Docker構成

初期リリースでは単一アプリケーション構成とする。

Webサーバーとスケジューラーは同じアプリケーションプロセスで管理し、スケジューラーの重複起動を防止する。

```yaml
services:
  looop-controller:
    build:
      context: .
      dockerfile: Dockerfile

    container_name: looop-controller

    restart: unless-stopped

    env_file:
      - .env

    environment:
      TZ: Asia/Tokyo
      PYTHONUNBUFFERED: "1"

    ports:
      - "127.0.0.1:8080:8080"

    volumes:
      - ./data:/app/data
      - ./logs:/app/logs

    stop_grace_period: 30s

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

初期状態ではローカルホストからのアクセスに限定する。

宅内LANへ公開する場合は、認証とアクセス制限を適用する。

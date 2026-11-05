# ディレクトリ構成

```text
looop-power-controller/
│
├── compose.yaml
├── Dockerfile
├── .env
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
│
├── app/
│   ├── main.py
│   ├── config.py
│   │
│   ├── api/
│   │   ├── dashboard.py
│   │   ├── forecast.py
│   │   ├── control.py
│   │   ├── devices.py
│   │   ├── history.py
│   │   ├── schedule.py
│   │   └── settings.py
│   │
│   ├── providers/
│   │   ├── base.py
│   │   └── looop.py
│   │
│   ├── clients/
│   │   ├── switchbot.py
│   │   ├── portable_power.py
│   │   ├── jackery.py
│   │   └── fossibot.py      # 調査の結果、採用した場合（docs/17 §17.9）
│   │
│   ├── services/
│   │   ├── forecast.py
│   │   ├── optimizer.py
│   │   ├── scheduler.py
│   │   ├── controller.py
│   │   ├── safety.py
│   │   └── maintenance.py
│   │
│   ├── models/
│   │   ├── device.py
│   │   ├── forecast.py
│   │   ├── schedule.py
│   │   └── settings.py
│   │
│   ├── repositories/
│   │   └── database.py
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── schedule.html
│   │   ├── history.html
│   │   ├── devices.html
│   │   ├── settings.html
│   │   └── components/
│   │
│   └── static/
│       ├── css/
│       └── js/
│
├── tests/
│   ├── test_forecast.py
│   ├── test_optimizer.py
│   ├── test_controller.py
│   ├── test_safety.py
│   └── test_api.py
│
├── data/
│
└── logs/
```

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
│   │   ├── history.py
│   │   └── settings.py
│   │
│   ├── providers/
│   │   ├── base.py
│   │   └── looop.py
│   │
│   ├── clients/
│   │   └── switchbot.py
│   │
│   ├── services/
│   │   ├── forecast.py
│   │   ├── optimizer.py
│   │   ├── scheduler.py
│   │   ├── controller.py
│   │   └── safety.py
│   │
│   ├── models/
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
│   │   ├── forecast.html
│   │   ├── schedule.html
│   │   ├── history.html
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

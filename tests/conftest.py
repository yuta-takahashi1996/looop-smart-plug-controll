import os

# app モジュールの import 前に設定し、テストが data/app.db に触れないようにする
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DRY_RUN"] = "true"

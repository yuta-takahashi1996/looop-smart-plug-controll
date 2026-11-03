"""Web UI のログインパスワードから WEB_PASSWORD_HASH を生成する。

    uv run python scripts/hash_password.py          # 対話入力（端末から実行する）
    uv run python scripts/hash_password.py < file   # 標準入力の1行目を使う

出力された行（WEB_PASSWORD_HASH='...'）をそのまま .env に貼り付ける。ハッシュは $ を含むため、
シングルクォートで囲んで出力する（囲まないと Docker Compose が $ を変数として展開し、値が壊れる）。
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.auth import hash_password  # noqa: E402


def main() -> None:
    if sys.stdin.isatty():
        password = getpass.getpass("パスワード: ")
        if password and getpass.getpass("パスワード（確認）: ") != password:
            sys.exit("パスワードが一致しません")
    else:
        # 端末がない場合（パイプ入力など）は標準入力の1行目をパスワードとする
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        sys.exit("パスワードが空です")
    print(f"WEB_PASSWORD_HASH='{hash_password(password)}'")


if __name__ == "__main__":
    main()

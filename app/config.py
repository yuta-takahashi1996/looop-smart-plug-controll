"""アプリケーション設定。

すべての設定は環境変数（または .env）から読み込む。
SwitchBot の認証情報などの秘匿情報は環境変数のみで管理し、DBへは保存しない。
"""

from datetime import time
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# ループバックとプライベートアドレス帯（Docker のブリッジ経由のアクセスを含む）
DEFAULT_ALLOWED_NETWORKS = "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def _parse_networks(value: str) -> list[IPv4Network | IPv6Network]:
    return [ip_network(part.strip()) for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: Literal["development", "production", "test"] = "development"
    tz: str = "Asia/Tokyo"
    dry_run: bool = True
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

    # Web UI
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_username: str = "admin"
    # scripts/hash_password.py で生成した scrypt ハッシュ。未設定の場合はログインできない
    web_password_hash: SecretStr = SecretStr("")
    # セッションCookieの署名鍵。未設定の場合はログインできない
    session_secret: SecretStr = SecretStr("")
    # Web UI へのアクセスを許可する接続元（カンマ区切りのCIDR）
    allowed_networks: Annotated[list[IPv4Network | IPv6Network], NoDecode] = Field(
        default_factory=lambda: _parse_networks(DEFAULT_ALLOWED_NETWORKS)
    )

    # Operation
    operation_start: time = time(9, 0)
    operation_end: time = time(18, 0)

    # Looop
    looop_area: str = "hokkaido"
    forecast_retry_count: int = Field(default=3, ge=0)

    # SwitchBot
    switchbot_token: SecretStr = SecretStr("")
    switchbot_secret: SecretStr = SecretStr("")
    switchbot_device_id: str = ""

    # Optimization
    min_saving_spread: float = Field(default=20, ge=0)
    round_trip_efficiency: float = Field(default=0.85, gt=0, le=1)
    max_continuous_off_minutes: int = Field(default=120, ge=0)
    max_daily_off_minutes: int = Field(default=180, ge=0)
    min_switch_interval_minutes: int = Field(default=30, ge=0)
    estimated_average_power_w: float = Field(default=150, ge=0)

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("allowed_networks", mode="before")
    @classmethod
    def _split_networks(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_networks(value)
        return value

    @property
    def auth_configured(self) -> bool:
        return bool(self.web_password_hash.get_secret_value() and self.session_secret.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()

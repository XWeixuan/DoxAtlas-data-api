from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_PORT = 8028
DEFAULT_ALLOWED_CLIENT_IPS = "43.135.22.202"


def _bool_env(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: str = "") -> set[str]:
    raw = os.environ.get(name, default)
    return {item.strip() for item in raw.split(",") if item.strip()}


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    allowed_client_ips: set[str]
    trust_proxy_headers: bool
    disable_ip_allowlist: bool
    init_db_on_startup: bool


def get_settings() -> ApiSettings:
    return ApiSettings(
        host=os.environ.get("DATA_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("DATA_API_PORT", str(DEFAULT_PORT))),
        allowed_client_ips=_csv_env("DATA_API_ALLOWED_CLIENT_IPS", DEFAULT_ALLOWED_CLIENT_IPS),
        trust_proxy_headers=_bool_env("DATA_API_TRUST_PROXY_HEADERS", False),
        disable_ip_allowlist=_bool_env("DATA_API_DISABLE_IP_ALLOWLIST", False),
        init_db_on_startup=_bool_env("DATA_API_INIT_DB_ON_STARTUP", True),
    )

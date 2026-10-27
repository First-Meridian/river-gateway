"""Gateway configuration: a YAML file with ${ENV} expansion, validated by pydantic."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Facts from the River docs (docs.river.ai/guides/inference*, /guides/inference-limits/).
RIVER_API_BASE = "https://api.river.ai/v1"
RIVER_PRICING_URL = "https://console.river.ai/api/public/pricing"
RIVER_MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MiB total JSON, including base64 images
RIVER_NON_STREAM_DEADLINE = 900.0  # up to 15 minutes for a non-streaming response

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

# Used when no config file exists: proxy everything to River Cloud.
DEFAULT_CONFIG_YAML = """
upstreams:
  river:
    base_url: ${RIVER_BASE_URL:-https://api.river.ai/v1}
    api_key: ${RIVER_API_KEY}
admin_token: ${GATEWAY_ADMIN_TOKEN}
"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8080
    max_body_bytes: int = Field(RIVER_MAX_BODY_BYTES, ge=1024)
    cors_origins: list[str] = []
    metrics: bool = True
    ui: bool = True
    log_level: str = "info"


class RetryConfig(_Strict):
    max_retries: int = Field(2, ge=0, le=10)
    backoff_base: float = Field(0.5, gt=0)
    backoff_max: float = Field(8.0, gt=0)
    # A Retry-After longer than this is not waited on: the request moves to a
    # fallback model or the 429/503 is returned to the client.
    max_retry_after: float = Field(30.0, ge=0)


class UpstreamConfig(_Strict):
    base_url: str = RIVER_API_BASE
    api_key: str = ""
    connect_timeout: float = Field(10.0, gt=0)
    # River allows up to 10 minutes of prefill and 15 minutes for a
    # non-streaming response, so the read timeout must exceed both.
    read_timeout: float = Field(RIVER_NON_STREAM_DEADLINE, gt=0)
    max_concurrency: int | None = Field(None, ge=1)
    queue_timeout: float = Field(30.0, ge=0)
    inject_stream_usage: bool = True
    headers: dict[str, str] = {}

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class Price(_Strict):
    """USD per million tokens, the same shape as River's public pricing JSON."""

    input: float = 0.0
    cached_input: float | None = None
    output: float = 0.0


class ModelRoute(_Strict):
    name: str
    upstream: str | None = None
    target: str | None = None
    fallbacks: list[str] = []
    chat_defaults: dict[str, Any] = {}
    responses_defaults: dict[str, Any] = {}
    price: Price | None = None
    hidden: bool = False

    @property
    def target_model(self) -> str:
        return self.target or self.name


class PricingConfig(_Strict):
    url: str | None = RIVER_PRICING_URL
    refresh_seconds: float = Field(3600.0, ge=60)
    overrides: dict[str, Price] = {}


class ValidationConfig(_Strict):
    image_mime_types: list[str] = ["image/jpeg", "image/png"]
    reject_unsupported_responses_params: bool = True
    responses_store_default_false: bool = True


class AccountsConfig(_Strict):
    # The upstream whose River API key the account pool switches; defaults to
    # default_upstream. Other upstreams (e.g. deployments) keep their own keys.
    upstream: str | None = None


class KeyDefaults(_Strict):
    rpm: int | None = Field(60, ge=1)
    max_concurrency: int | None = Field(8, ge=1)


class GatewayConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: str = "data/gateway.db"
    admin_token: str | None = None
    upstreams: dict[str, UpstreamConfig] = Field(default_factory=lambda: {"river": UpstreamConfig()})
    default_upstream: str | None = "river"
    expose_upstream_models: bool = True
    models_cache_seconds: float = Field(300.0, ge=0)
    models: list[ModelRoute] = []
    retry: RetryConfig = Field(default_factory=RetryConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    key_defaults: KeyDefaults = Field(default_factory=KeyDefaults)
    accounts: AccountsConfig = Field(default_factory=AccountsConfig)

    @field_validator("admin_token", "default_upstream", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return value or None

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        if not self.upstreams:
            raise ValueError("at least one upstream is required")
        if self.default_upstream is not None and self.default_upstream not in self.upstreams:
            raise ValueError(f"default_upstream {self.default_upstream!r} is not defined in upstreams")
        if self.accounts.upstream is not None and self.accounts.upstream not in self.upstreams:
            raise ValueError(f"accounts.upstream {self.accounts.upstream!r} is not defined in upstreams")
        names: set[str] = set()
        for route in self.models:
            if route.name in names:
                raise ValueError(f"duplicate model route {route.name!r}")
            names.add(route.name)
            upstream = route.upstream or self.default_upstream
            if upstream is None:
                raise ValueError(f"model {route.name!r} needs an upstream because default_upstream is not set")
            if upstream not in self.upstreams:
                raise ValueError(f"model {route.name!r} uses unknown upstream {upstream!r}")
        for route in self.models:
            for fallback in route.fallbacks:
                if fallback == route.name:
                    raise ValueError(f"model {route.name!r} cannot fall back to itself")
                if fallback not in names and self.default_upstream is None:
                    raise ValueError(f"fallback {fallback!r} of model {route.name!r} is not a configured model")
        return self


PLACEHOLDER_ADMIN_TOKENS = {"change-me", "changeme", "admin", "secret"}
WEAK_ADMIN_TOKEN = (
    "admin_token is a placeholder or shorter than 16 characters; anyone who can reach the gateway "
    "could manage its keys. Set GATEWAY_ADMIN_TOKEN to a long random string."
)


def admin_token_is_weak(token: str) -> bool:
    return token.lower() in PLACEHOLDER_ADMIN_TOKENS or len(token) < 16


def expand_env(value: Any) -> Any:
    """Replace ${VAR} and ${VAR:-default} in every string of a parsed YAML tree."""
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; variables already set in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    """Load the config file (or built-in defaults) and resolve relative paths.

    Lookup order: explicit path, $RIVER_GATEWAY_CONFIG, ./config.yaml, defaults.
    A .env file next to the config file (or in the working directory) is loaded first.
    """
    candidate = path or os.environ.get("RIVER_GATEWAY_CONFIG")
    if not candidate and Path("config.yaml").is_file():
        candidate = "config.yaml"
    if candidate:
        config_path = Path(candidate).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"config file not found: {config_path}")
        base_dir = config_path.parent
        load_dotenv(base_dir / ".env")
        load_dotenv(Path.cwd() / ".env")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        base_dir = Path.cwd()
        load_dotenv(base_dir / ".env")
        raw = yaml.safe_load(DEFAULT_CONFIG_YAML)
    config = GatewayConfig.model_validate(expand_env(raw))
    if config.database != ":memory:" and not Path(config.database).is_absolute():
        config.database = str(base_dir / config.database)
    return config

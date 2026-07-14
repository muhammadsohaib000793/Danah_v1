"""Twelve-factor configuration.

Every variable in `.env.example` is read here, and every setting here appears in
`.env.example` — that bidirectional contract is enforced by
`tests/unit/test_config_contract.py`.

Secrets have no usable defaults: in `production` the app fails fast at startup if a
required secret is missing or still carries a `CHANGE_ME` placeholder.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final, Self

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import (
    AppEnv,
    Classification,
    EmbeddingProvider,
    LLMProvider,
    Role,
    StorageBackend,
)

# ---------------------------------------------------------------------------
# Cost ledger — USD per 1,000,000 tokens.
# Overridable wholesale via the LLM_PRICE_TABLE env var (JSON), so a price change
# never requires a code deploy.
# ---------------------------------------------------------------------------
DEFAULT_PRICE_TABLE: Final[dict[str, dict[str, float]]] = {
    # Anthropic
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-4-5": {"input": 5.00, "output": 25.00},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    # Embeddings (output price is 0 — embeddings bill on input only)
    "voyage-3.5": {"input": 0.06, "output": 0.0},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

# Master prompt §7.6: role → clearance ceiling.
ROLE_CLEARANCE: Final[dict[Role, Classification]] = {
    Role.ADMIN: Classification.OFFICIAL_SENSITIVE,
    Role.EXECUTIVE: Classification.OFFICIAL_SENSITIVE,
    Role.ANALYST: Classification.OFFICIAL,
    Role.VIEWER: Classification.INTERNAL,
}

_PLACEHOLDER_PREFIXES: Final[tuple[str, ...]] = ("CHANGE_ME", "changeme", "your-", "xxx")

# Embedding dimensions each model family can emit. Both providers accept an explicit
# output dimension, so EMBEDDING_DIM is honoured rather than merely asserted.
_SUPPORTED_EMBEDDING_DIMS: Final[dict[str, set[int]]] = {
    "voyage-3.5": {256, 512, 1024, 2048},
    "voyage-3-large": {256, 512, 1024, 2048},
    "text-embedding-3-small": {512, 1536},
    "text-embedding-3-large": {256, 1024, 3072},
}


def _csv(value: str) -> list[str]:
    """Parse a comma-separated env value into a list of trimmed, non-empty strings."""
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """Application settings. Mirrors `.env.example` one-for-one."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------------
    app_name: str = "DANAH"
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    tz: str = "Asia/Dubai"
    # Stored raw because pydantic-settings would otherwise try to JSON-decode a list.
    cors_origins: str = "http://localhost:3000,http://localhost:8000,null"

    # -- Security & auth -----------------------------------------------------
    jwt_secret_key: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 14
    admin_email: str = "admin@ministry.gov"
    # The first executive. Approval notifications are addressed to `role=executive`, and nothing
    # is ever published without an executive deciding to publish it — so a deployment seeded with
    # an admin alone has an approval queue that no one can clear and no one is told about.
    approver_email: str = "executive@ministry.gov"
    admin_initial_password: SecretStr = SecretStr("")
    rate_limit_login_per_minute: int = 5
    rate_limit_chat_per_minute: int = 20

    # -- OIDC (Phase 4 stub, disabled by default) ----------------------------
    oidc_enabled: bool = False
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")

    # -- Database ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://danah:danah@localhost:5432/danah"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    postgres_user: str = "danah"
    postgres_password: SecretStr = SecretStr("danah")
    postgres_db: str = "danah"
    # Consumed by docker-compose, not by the app; declared here to keep the
    # .env.example <-> Settings contract complete in both directions.
    postgres_host_port: int = 5432
    redis_host_port: int = 6379

    # -- Redis ---------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # -- LLM providers -------------------------------------------------------
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_fallback_enabled: bool = False
    llm_model_primary: str = "claude-sonnet-4-5"
    llm_model_fast: str = "claude-haiku-4-5"
    openai_model_primary: str = "gpt-4o"
    openai_model_fast: str = "gpt-4o-mini"
    llm_max_tokens_default: int = 2048
    llm_temperature_default: float = 0.2
    llm_timeout_seconds: int = 90
    llm_max_retries: int = 3
    llm_price_table: str = ""  # optional JSON override of DEFAULT_PRICE_TABLE
    pipeline_token_budget: int = 400_000
    daily_cost_alert_usd: float = 25.0

    # -- Embeddings ----------------------------------------------------------
    embedding_provider: EmbeddingProvider = EmbeddingProvider.VOYAGE
    voyage_api_key: SecretStr = SecretStr("")
    embedding_model: str = "voyage-3.5"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1024
    embedding_batch_size: int = 64

    # -- RAG / retrieval -----------------------------------------------------
    chunk_size_tokens: int = 800
    chunk_overlap_tokens: int = 150
    retrieval_top_k: int = 8
    retrieval_min_score: float = 0.25
    hybrid_search_enabled: bool = True

    # -- Agents & orchestrator ----------------------------------------------
    agent_max_tool_iterations: int = 6
    signal_relevance_threshold: float = 0.55
    pipeline_schedule_cron: str = "0 5 * * *"
    pipeline_max_items_per_run: int = 150
    briefing_languages: str = "en,ar"

    # -- Ingestion -----------------------------------------------------------
    watch_countries: str = "ARE,SAU,PAK,USA,CHN"
    worldbank_indicators: str = "NY.GDP.MKTP.KD.ZG,FP.CPI.TOTL.ZG,SL.UEM.TOTL.ZS"
    watch_query_terms: str = "trade policy,energy prices,supply chain,sanctions"
    rss_feeds: str = "https://feeds.bbci.co.uk/news/business/rss.xml"
    reliefweb_country_filter: str = "are,pak"
    default_poll_interval_minutes: int = 60
    webhook_hmac_default_secret: SecretStr = SecretStr("")

    # -- Storage -------------------------------------------------------------
    storage_backend: StorageBackend = StorageBackend.LOCAL
    storage_local_path: str = "./data/documents"
    s3_endpoint_url: str = ""
    s3_bucket: str = "danah-documents"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_region: str = ""
    max_upload_size_mb: int = 25
    allowed_upload_extensions: str = "pdf,docx,txt,md,html"

    # -- Notifications -------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_email: str = "danah-noreply@ministry.gov"
    smtp_use_tls: bool = True

    # -- Observability -------------------------------------------------------
    metrics_enabled: bool = True
    request_id_header: str = "X-Request-ID"
    sentry_dsn: str = ""

    # ------------------------------------------------------------------
    # Derived / parsed views
    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins)

    @property
    def briefing_language_list(self) -> list[str]:
        return _csv(self.briefing_languages)

    @property
    def watch_country_list(self) -> list[str]:
        return [c.upper() for c in _csv(self.watch_countries)]

    @property
    def worldbank_indicator_list(self) -> list[str]:
        return _csv(self.worldbank_indicators)

    @property
    def watch_query_term_list(self) -> list[str]:
        return _csv(self.watch_query_terms)

    @property
    def rss_feed_list(self) -> list[str]:
        return _csv(self.rss_feeds)

    @property
    def reliefweb_country_list(self) -> list[str]:
        return [c.lower() for c in _csv(self.reliefweb_country_filter)]

    @property
    def allowed_upload_extension_set(self) -> set[str]:
        return {e.lower().lstrip(".") for e in _csv(self.allowed_upload_extensions)}

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def role_clearance(self) -> dict[Role, Classification]:
        return ROLE_CLEARANCE

    @property
    def price_table(self) -> dict[str, dict[str, float]]:
        """Effective price table: defaults merged with the LLM_PRICE_TABLE override."""
        table = {model: dict(prices) for model, prices in DEFAULT_PRICE_TABLE.items()}
        if self.llm_price_table.strip():
            override: dict[str, dict[str, float]] = json.loads(self.llm_price_table)
            for model, prices in override.items():
                table[model] = {**table.get(model, {"input": 0.0, "output": 0.0}), **prices}
        return table

    def price_for(self, model: str) -> tuple[Decimal, Decimal]:
        """(input, output) USD per 1M tokens. Unknown models cost 0 rather than crash."""
        prices = self.price_table.get(model, {"input": 0.0, "output": 0.0})
        return Decimal(str(prices.get("input", 0.0))), Decimal(str(prices.get("output", 0.0)))

    @property
    def sync_database_url(self) -> str:
        """psycopg-style URL for Alembic's synchronous engine paths."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def active_llm_key(self) -> SecretStr:
        return (
            self.anthropic_api_key
            if self.llm_provider is LLMProvider.ANTHROPIC
            else self.openai_api_key
        )

    @property
    def active_embedding_key(self) -> SecretStr:
        return (
            self.voyage_api_key
            if self.embedding_provider is EmbeddingProvider.VOYAGE
            else self.openai_api_key
        )

    @property
    def active_embedding_model(self) -> str:
        return (
            self.embedding_model
            if self.embedding_provider is EmbeddingProvider.VOYAGE
            else self.openai_embedding_model
        )

    @property
    def has_llm_credentials(self) -> bool:
        """False in PENDING-CREDENTIALS mode: the API still boots, LLM routes 503."""
        return bool(self.active_llm_key.get_secret_value().strip())

    @property
    def has_embedding_credentials(self) -> bool:
        return bool(self.active_embedding_key.get_secret_value().strip())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @field_validator("database_url")
    @classmethod
    def _normalise_db_scheme(cls, v: str) -> str:
        """Accept the bare scheme every managed provider hands out, and drive it with asyncpg.

        Railway, Render, Neon, Fly and Supabase all inject `postgresql://` (Heroku still emits
        the legacy `postgres://`). This app's engine is async and needs the `+asyncpg` driver, so
        a raw provider URL would fail at connect time with a driver error that looks nothing like
        its cause. Normalising here means the deployment can paste the provider's URL verbatim —
        or reference it directly — and it just works. A URL that already names a driver is left
        untouched.
        """
        for prefix in ("postgresql+", "postgres+"):
            if v.startswith(prefix):
                return v
        if v.startswith("postgresql://"):
            return "postgresql+asyncpg://" + v[len("postgresql://") :]
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://") :]
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        level = v.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return level

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _validate_overlap(cls, v: int, info: ValidationInfo) -> int:
        size = info.data.get("chunk_size_tokens")
        if isinstance(size, int) and v >= size:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_SIZE_TOKENS")
        return v

    @field_validator("llm_price_table")
    @classmethod
    def _validate_price_table(cls, v: str) -> str:
        if v.strip():
            try:
                parsed: Any = json.loads(v)
            except json.JSONDecodeError as exc:  # pragma: no cover - config error path
                raise ValueError(f"LLM_PRICE_TABLE must be valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("LLM_PRICE_TABLE must be a JSON object of model -> prices")
        return v

    @model_validator(mode="after")
    def _validate_embedding_dim(self) -> Self:
        model = self.active_embedding_model
        supported = _SUPPORTED_EMBEDDING_DIMS.get(model)
        if supported is not None and self.embedding_dim not in supported:
            raise ValueError(
                f"EMBEDDING_DIM={self.embedding_dim} is not emittable by {model!r}; "
                f"supported: {sorted(supported)}. The vector column dimension is fixed at "
                f"migration time, so these must agree."
            )
        return self

    @model_validator(mode="after")
    def _fail_fast_on_missing_secrets(self) -> Self:
        """Master prompt §12: never ship a usable default for a secret.

        Development is allowed to run without provider keys (PENDING-CREDENTIALS mode) —
        LLM-backed routes return 503 rather than silently faking answers. Production
        must have every secret, and no placeholder values.
        """
        problems: list[str] = []

        def _check(name: str, value: SecretStr, *, required_always: bool) -> None:
            raw = value.get_secret_value().strip()
            if not raw:
                if required_always or self.is_production:
                    problems.append(f"{name} is required but empty")
                return
            if raw.startswith(_PLACEHOLDER_PREFIXES):
                problems.append(f"{name} still holds a placeholder value")

        # Required in every environment — the app cannot mint or verify tokens without it.
        _check("JWT_SECRET_KEY", self.jwt_secret_key, required_always=True)
        if len(self.jwt_secret_key.get_secret_value()) < 32 and self.is_production:
            problems.append("JWT_SECRET_KEY must be at least 32 characters in production")

        _check("ADMIN_INITIAL_PASSWORD", self.admin_initial_password, required_always=False)
        _check(
            "WEBHOOK_HMAC_DEFAULT_SECRET", self.webhook_hmac_default_secret, required_always=False
        )
        _check("POSTGRES_PASSWORD", self.postgres_password, required_always=False)

        if self.is_production:
            if self.app_debug:
                problems.append("APP_DEBUG must be false in production")
            if "null" in self.cors_origin_list:
                problems.append(
                    "CORS_ORIGINS must not contain 'null' in production "
                    "(it exists only for local file:// testing of the v11 prototype)"
                )
            if self.llm_provider is LLMProvider.ANTHROPIC:
                _check("ANTHROPIC_API_KEY", self.anthropic_api_key, required_always=True)
            else:
                _check("OPENAI_API_KEY", self.openai_api_key, required_always=True)
            if self.llm_fallback_enabled:
                _check("OPENAI_API_KEY", self.openai_api_key, required_always=True)
                _check("ANTHROPIC_API_KEY", self.anthropic_api_key, required_always=True)
            if self.embedding_provider is EmbeddingProvider.VOYAGE:
                _check("VOYAGE_API_KEY", self.voyage_api_key, required_always=True)
            else:
                _check("OPENAI_API_KEY", self.openai_api_key, required_always=True)
            if self.oidc_enabled and not self.oidc_issuer_url:
                problems.append("OIDC_ISSUER_URL is required when OIDC_ENABLED=true")

        if problems:
            raise ValueError(
                "Invalid configuration (see .env.example):\n  - " + "\n  - ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Cached so `.env` is read exactly once.

    Always go through this function rather than instantiating `Settings()` — tests
    clear the cache (`get_settings.cache_clear()`) to install an overridden config.
    """
    return Settings()

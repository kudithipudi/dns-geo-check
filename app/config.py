from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_path: str = "data/dns-geo-check.db"

    # App log verbosity (see §7 of the lab standards): flip via LOG_LEVEL in
    # .env without touching code.
    log_level: str = "info"

    # Per-IP cap on POST /check (each call fans out to 8 Cloudflare Durable
    # Objects), enforced over a trailing window.
    rate_limit_per_minute: int = 20
    rate_limit_window_seconds: int = 60

    # The deployed Cloudflare Worker that runs the region fan-out, and the
    # shared secret it checks (X-Probe-Secret). Blank by default so a fresh
    # checkout / uxcheck still boots; /check returns 503 until both are set.
    worker_url: str = ""
    probe_secret: str = ""

    # Where GET / sends a browser. This is a pure JSON API with no UI of its
    # own; in the lab it fronts the "DNS Geo Check" card on net-tools, so a
    # stray visitor is bounced there. Leave blank on a standalone deploy and
    # GET / returns a small JSON pointer to the API instead.
    index_redirect_url: str = ""

    # How long to wait on the Worker's aggregated response. The Worker itself
    # caps each region at ~10s and returns via Promise.allSettled (~12s worst
    # case), so this is the outer bound: Worker ~12s < this 20s < nginx 45s.
    request_timeout_seconds: float = 20.0


def get_settings() -> Settings:
    # Not cached: Settings() is cheap to build, so we always read the current
    # environment/.env rather than risk a stale cached instance (e.g. across
    # tests that monkeypatch env vars).
    return Settings()

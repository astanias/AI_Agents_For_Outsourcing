from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    database_url: str = "postgresql+psycopg2://appuser:apppassword@localhost:5433/appdb"

    app_env: str = "development"

    jwt_secret: str = "dev-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    frontend_origin: str | None = None

    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # lax|strict|none
    cookie_domain: str | None = None
    csrf_protection_enabled: bool = False

    google_client_id: str | None = None
    google_client_secret: str | None = None

    resend_api_key: str | None = None
    email_from_address: str | None = None
    email_from_name: str = "AI Scheduler"
    app_base_url: str = "http://127.0.0.1:8000"

    log_level: str = "INFO"

    openrouteservice_api_key: str | None = None
    openrouteservice_base_url: str = "https://api.openrouteservice.org"
    openrouteservice_timeout_seconds: int = 10
    openrouteservice_profile: str = "driving-car"

    travel_warning_buffer_minutes: int = 10
    travel_warning_tight_window_minutes: int = 15

    organization_default_location: str | None = None
    organization_default_location_latitude: float | None = None
    organization_default_location_longitude: float | None = None

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_base_url: str | None = None

    @field_validator("organization_default_location_latitude", "organization_default_location_longitude", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value):
        if value == "":
            return None
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    @property
    def is_deployed_like(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production", "stage", "staging"}

    def validate_runtime(self) -> None:
        if not self.is_production:
            return

        errors: list[str] = []
        if self.jwt_secret == "dev-change-me" or len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET must be changed to a strong value of at least 32 characters")
        if not self.cookie_secure:
            errors.append("COOKIE_SECURE must be true in production")
        if not self.csrf_protection_enabled:
            errors.append("CSRF_PROTECTION_ENABLED must be true in production")
        if self.frontend_origin and "localhost" in self.frontend_origin.lower():
            errors.append("FRONTEND_ORIGIN must not point to localhost in production")
        if self.frontend_origin and "127.0.0.1" in self.frontend_origin:
            errors.append("FRONTEND_ORIGIN must not point to 127.0.0.1 in production")

        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


settings = Settings()

from pydantic_settings import BaseSettings
from sqlalchemy.engine import make_url


def get_async_database_url(database_url: str) -> str:
    """Return a PostgreSQL URL suitable for SQLAlchemy's async engine."""
    url = make_url(database_url)
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    return database_url


class Settings(BaseSettings):
    ENV: str = "development"  # development | testing | production — see main.py's JWT_SECRET guard
    DATABASE_URL: str = "postgresql+asyncpg://around:around@localhost:5432/around"
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 14  # 14 days
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    APPLE_OAUTH_CLIENT_ID: str = ""
    OBJECT_STORAGE_BUCKET: str = "around-media"
    OBJECT_STORAGE_ENDPOINT: str = ""
    OBJECT_STORAGE_KEY: str = ""
    OBJECT_STORAGE_SECRET: str = ""
    CORS_ORIGINS: list[str] = ["http://localhost:5500", "http://127.0.0.1:5500"]
    DEFAULT_CITY: str = "Vilnius"

    # --- Email verification ---
    # Provider-agnostic: any SMTP-speaking provider works (Gmail app
    # password, SendGrid/Postmark/SES SMTP relay, Resend's SMTP endpoint,
    # etc.) — deliberately not tied to one vendor's REST API, so there's
    # no new dependency and no vendor lock-in. If SMTP_HOST is blank,
    # EmailSender (app/email_sender.py) logs the email instead of sending
    # it — safe by default, nothing breaks with no provider configured.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@around.lt"
    SMTP_USE_TLS: bool = True
    # Base URL used to build the verification link sent by email — must
    # be the real production frontend origin (e.g. https://around.lt),
    # not the backend's own URL.
    PUBLIC_APP_URL: str = "http://localhost:5500"
    # Master switch for actually BLOCKING unverified accounts from
    # sensitive actions. Defaults to False deliberately: with no SMTP
    # provider configured, nobody could ever receive a verification
    # email, so turning enforcement on by default would lock every new
    # signup out of the app. The verification flow itself (token
    # creation, the email, the verify/resend endpoints) works regardless
    # of this flag — only the *gating* of other endpoints depends on it.
    # Flip to true once SMTP_* is configured and confirmed working.
    REQUIRE_EMAIL_VERIFICATION: bool = False

    class Config:
        env_file = ".env"


settings = Settings()

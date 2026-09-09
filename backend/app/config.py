from pydantic_settings import BaseSettings


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

    class Config:
        env_file = ".env"


settings = Settings()

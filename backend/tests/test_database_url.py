from sqlalchemy.engine import make_url

from app.config import get_async_database_url


def test_plain_postgresql_url_uses_asyncpg():
    database_url = "postgresql://user:password@host:5432/db"

    assert get_async_database_url(database_url) == "postgresql+asyncpg://user:password@host:5432/db"


def test_explicit_asyncpg_url_is_unchanged():
    database_url = "postgresql+asyncpg://user:password@host:5432/db"

    assert get_async_database_url(database_url) == database_url


def test_normalization_preserves_encoded_password_and_query_parameters():
    normalized = get_async_database_url(
        "postgresql://user:pa%40ss%3Aword@host:5432/db?sslmode=require"
    )
    url = make_url(normalized)

    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "user"
    assert url.password == "pa@ss:word"
    assert url.query == {"sslmode": "require"}

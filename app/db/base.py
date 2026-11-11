from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine(
    url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pgbouncer: bool = False,
    pool_timeout: float = 5.0,
    statement_timeout_ms: int = 0,
    idle_tx_timeout_ms: int = 0,
    command_timeout: float | None = 60.0,
    app_name: str = "ampirevpn",
) -> AsyncEngine:
    """Движок SQLAlchemy.

    * ``pool_timeout`` — сколько ждать свободного соединения. Раньше было 30 с по умолчанию: при исчерпании пула
      бот «подвисал». С 5 секундами запрос быстро падает, а не копит очередь.
    * ``statement_timeout_ms`` / ``idle_tx_timeout_ms`` — серверные таймауты PostgreSQL: зависший запрос и
      «забытая» открытая транзакция не держат соединение и блокировки бесконечно.
    * С PgBouncer (transaction mode) параметры старта соединения не передаём — ограничения задают на роли БД
      (``ALTER ROLE ampire SET statement_timeout = '30s'``, см. docs/SETUP.md); prepared statements отключены,
      а имена подготовленных запросов уникальны (рецепт SQLAlchemy для asyncpg + PgBouncer).
    """
    kwargs: dict = {"pool_pre_ping": True, "pool_recycle": 1800, "pool_timeout": pool_timeout}
    connect_args: dict = {}
    if command_timeout:
        connect_args["command_timeout"] = command_timeout
    if pgbouncer:
        connect_args.update(
            {
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            }
        )
    else:
        server_settings = {"application_name": app_name}
        if statement_timeout_ms > 0:
            server_settings["statement_timeout"] = str(int(statement_timeout_ms))
        if idle_tx_timeout_ms > 0:
            server_settings["idle_in_transaction_session_timeout"] = str(int(idle_tx_timeout_ms))
        connect_args["server_settings"] = server_settings
    kwargs["connect_args"] = connect_args
    return create_async_engine(url, pool_size=pool_size, max_overflow=max_overflow, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт таблицы, если их ещё нет (только для разработки и тестов — в проде схему ведёт Alembic)."""
    from app.db import models  # noqa: F401  (регистрирует модели в metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

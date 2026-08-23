from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

if settings.DB_DIALECT == "mysql":
    # SQLAlchemy's pre_ping support for aiomysql infers whether to call
    # dbapi_connection.ping(reconnect=...) by inspecting pymysql.connections
    # .Connection.ping's default, but that default changed in pymysql 1.2.0
    # and the aiomysql wrapper's ping() has no default at all — the guess
    # comes out wrong and pool_pre_ping crashes every connection checkout.
    # See https://github.com/sqlalchemy/sqlalchemy/issues/10492
    # Postgres (asyncpg) has no equivalent issue, so this is mysql-only.
    from sqlalchemy.dialects.mysql.aiomysql import MySQLDialect_aiomysql

    def _do_ping(self, dbapi_connection):
        dbapi_connection.ping(False)
        return True

    MySQLDialect_aiomysql.do_ping = _do_ping


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.ENVIRONMENT == "development"),
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:15432/langchain_app",
)

# Engine 维护连接池并全局复用；pool_pre_ping 会在借出连接前剔除失效连接。
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)


# SessionLocal 是会话工厂，不是可供所有请求共享的 Session 实例。
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


# 建表前先导入模型模块，否则 Base.metadata 还不知道有哪些表。
def init_db() -> None:
    # 延迟导入避免循环依赖，同时确保 metadata 已登记全部模型。
    import backend.db.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

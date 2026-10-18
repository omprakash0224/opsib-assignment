"""
Pytest fixtures and test environment configuration.
"""
import os
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
import app.db.models  # Ensure all models are loaded


@pytest.fixture(scope="session")
def engine():
    """Creates a temporary test database (SQLite in-memory by default, or TEST_DATABASE_URL)."""
    test_db_url = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    if test_db_url.startswith("postgres://"):
        test_db_url = test_db_url.replace("postgres://", "postgresql://", 1)

    connect_args = {}
    if test_db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    test_engine = create_engine(
        test_db_url,
        connect_args=connect_args,
        future=True,
        pool_pre_ping=True,
    )

    if test_db_url.startswith("sqlite"):
        @event.listens_for(test_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(scope="function")
def db_session(engine):
    """Provides a transactional database session for each test."""
    connection = engine.connect()
    transaction = connection.begin()
    SessionTest = sessionmaker(bind=connection, expire_on_commit=False)
    session = SessionTest()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(scope="function")
def client(db_session):
    """Provides a FastAPI TestClient bound to the isolated db_session."""
    def override_get_db():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as test_client:
        yield test_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(scope="function")
def fresh_db_engine():
    """Creates an isolated in-memory SQLite database using StaticPool for multi-session support."""
    from sqlalchemy.pool import StaticPool
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture(scope="function")
def fresh_client(fresh_db_engine):
    """
    Provides a FastAPI TestClient where EVERY request gets a fresh DB session
    (created by SessionLocal and closed in finally), matching production get_db behavior.
    """
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=fresh_db_engine,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as test_client:
        yield test_client
    fastapi_app.dependency_overrides.clear()


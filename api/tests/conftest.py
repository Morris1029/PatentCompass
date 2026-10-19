import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db
from app.config import get_settings
from app.jobs import RunContext
from app.models import Base, Project, ResearchRun, ScopeVersion, new_id, utcnow


@pytest.fixture(scope="session")
def test_engine():
    url = make_url(os.environ.get("TEST_DATABASE_URL", get_settings().database_url)).set(database="patentcompass_test")
    if url.database != "patentcompass_test":
        raise RuntimeError("Tests must use the isolated patentcompass_test database")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        if not connection.scalar(text("SELECT 1 FROM pg_database WHERE datname='patentcompass_test'")):
            connection.execute(text('CREATE DATABASE "patentcompass_test"'))
    admin.dispose()
    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine, monkeypatch, tmp_path):
    connection = test_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(get_settings(), "storage_root", tmp_path)
    with factory() as session:
        yield session
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(db_session):
    row = Project(owner_id=get_settings().dev_owner_id, name="测试项目")
    db_session.add(row)
    db_session.flush()
    scope = ScopeVersion(project_id=row.id, number=1, confirmed=True, content={"industry": "无人机"})
    db_session.add(scope)
    db_session.flush()
    row.scope_version_id = scope.id
    db_session.flush()
    return row


@pytest.fixture
def context_factory(db_session):
    def make(project=None, kind="analyze", payload=None):
        db_session.flush()
        token = new_id()
        row = ResearchRun(owner_id=get_settings().dev_owner_id, project_id=project.id if project else None,
                          kind=kind, status="running", payload=payload or {}, dispatch_pending=False,
                          scope_version_id=project.scope_version_id if project else None,
                          dataset_version_ids=[project.dataset_version_id] if project and project.dataset_version_id else [],
                          lease_token=token, lease_expires_at=utcnow() + timedelta(hours=1))
        db_session.add(row)
        db_session.flush()
        return RunContext(row.id, token)
    return make

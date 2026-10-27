"""One request's counts, revision and result rows must share a database snapshot."""
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session


@contextmanager
def read_snapshot(db):
    # Authentication has already opened the dependency session's transaction.
    # Use a separate read-only transaction instead of committing that session
    # or changing isolation after its first query.
    with Session(bind=db.get_bind(), expire_on_commit=False) as snapshot:
        snapshot.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
        snapshot.execute(text('SET TRANSACTION READ ONLY'))
        yield snapshot

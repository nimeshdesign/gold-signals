import pytest


@pytest.fixture
def sign_in():
    """Sign a TestClient in as the owner (creates a real server-side session in the current database)."""
    def _sign_in(client):
        from app import db
        from app.web import auth, main

        with db.session() as conn:
            sid = auth.start_session(conn, 3600)
        client.cookies.set(auth.COOKIE, auth.make_session("admin", sid, main._secret(), 3600))
    return _sign_in

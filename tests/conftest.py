import pytest

from app import create_app


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        from app.extensions import db

        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()

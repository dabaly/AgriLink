import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(
        "testing",
        test_config={"MARKETPLACE_UPLOAD_ROOT": str(tmp_path / "marketplace-uploads")},
    )
    with app.app_context():
        from app.extensions import db

        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()

def test_home_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Fresh produce, closer together." in response.data


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_not_found_page(client):
    response = client.get("/missing-page")
    assert response.status_code == 404
    assert b"We could not complete that request." in response.data

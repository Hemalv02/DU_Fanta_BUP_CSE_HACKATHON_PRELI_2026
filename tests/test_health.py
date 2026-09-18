"""GET /health contract (Section 6.2)."""


def test_health_returns_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_fast_and_stable(client) -> None:
    for _ in range(5):
        assert client.get("/health").status_code == 200

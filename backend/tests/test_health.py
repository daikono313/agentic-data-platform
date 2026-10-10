from fastapi.testclient import TestClient

from adp.api.app import create_app


def test_health() -> None:
    client = TestClient(create_app())
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

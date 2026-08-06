"""Tests for the HTTP acquisition server."""


import pytest
from fastapi.testclient import TestClient

from documentcrawler.server import create_app


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n'
        'filename_template = "{first_author_last}_{year}.{ext}"\n'
        "workers = 1\n",
        encoding="utf-8",
    )
    app = create_app(config_path)
    with TestClient(app) as tc:
        yield tc


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "db" in data


def test_acquire_empty_body(client):
    resp = client.post("/acquire", json={})
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


def test_acquire_valid_doi(client):
    resp = client.post("/acquire", json={"doi": "10.1234/test.1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("pending", "failed")
    assert "id" in data


def test_get_job_not_found(client):
    resp = client.get("/jobs/99999")
    assert resp.status_code == 404


def test_get_job_exists(client):
    resp = client.post("/acquire", json={"doi": "10.1234/test.2"})
    doc_id = resp.json()["id"]
    resp = client.get(f"/jobs/{doc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == doc_id


def test_get_queue(client):
    client.post("/acquire", json={"doi": "10.1234/test.3"})
    resp = client.get("/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "pending" in data
    assert "items" in data


def test_acquire_duplicate_doi(client):
    a = client.post("/acquire", json={"doi": "10.1234/dup.1"})
    b = client.post("/acquire", json={"doi": "10.1234/dup.1"})
    assert a.json()["id"] == b.json()["id"]


def test_404_unknown_path(client):
    resp = client.get("/nonexistent")
    assert resp.status_code == 404


def test_acquire_invalid_body_returns_error_structure(client):
    resp = client.post("/acquire", content="not json")
    assert resp.status_code == 400 or resp.status_code == 422
    data = resp.json()
    assert "error" in data or "detail" in data


def test_events_stream_connects(client):
    with client.stream("GET", "/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        line = next(resp.iter_lines())
        assert "SSE pipeline stream connected" in line

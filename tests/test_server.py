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


def test_acquire_sync_returns_final_result(client):
    resp = client.post("/acquire/sync", json={"doi": "10.1234/test.1"})
    assert resp.status_code == 200
    data = resp.json()
    # No sources are enabled in the test config, so the pipeline resolves
    # immediately — the point of this test is that /acquire/sync blocks
    # until a *final* status, unlike /acquire which returns "pending".
    assert data["status"] in ("done", "failed")
    assert "id" in data


def test_acquire_sync_rejects_empty_body(client):
    resp = client.post("/acquire/sync", json={})
    assert resp.status_code == 400


def test_acquire_sync_rejects_out_of_range_timeout(client):
    resp = client.post("/acquire/sync?timeout=999", json={"doi": "10.1234/test.1"})
    assert resp.status_code == 400


def test_public_demo_disables_queue_endpoints(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\npublic_demo = true\n',
        encoding="utf-8",
    )
    app = create_app(config_path)
    with TestClient(app) as client:
        resp = client.post("/acquire", json={"doi": "10.1234/test.1"})
        assert resp.status_code == 400
        assert "acquire/sync" in resp.json()["error"]["message"]

        resp = client.post("/acquire/batch", json=[{"doi": "10.1234/test.1"}])
        assert resp.status_code == 400

        # /acquire/sync itself still works under public_demo.
        resp = client.post("/acquire/sync", json={"doi": "10.1234/test.1"})
        assert resp.status_code == 200
        assert resp.json()["status"] in ("done", "failed")


def test_root_landing(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "DocumentCrawler" in data["service"]
    assert data["documentation"] == "/docs"
    assert "acquire_sync" in data["endpoints"]
    # Landing doubles as the discovery page for the public demo.
    assert "mode" not in data  # non-demo instance


def test_root_landing_public_demo_mode(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\npublic_demo = true\n',
        encoding="utf-8",
    )
    app = create_app(config_path)
    with TestClient(app) as client:
        data = client.get("/").json()
        assert data["mode"] == "public_demo"
        assert data["notes"]


def test_unknown_path_uses_error_envelope(client):
    resp = client.get("/nonexistent/path")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "http_error"


def test_wrong_method_uses_error_envelope(client):
    resp = client.get("/acquire/sync")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "http_error"


def test_validation_error_uses_error_envelope(client):
    resp = client.post("/acquire", json={"doi": "10.1/x", "authors": "not-a-list"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


def test_reading_list_rejects_unknown_format(client):
    resp = client.get("/export/reading-list?format=bogus")
    assert resp.status_code == 400
    assert "Unsupported format" in resp.json()["error"]["message"]


def test_reading_list_accepts_known_formats(client):
    for fmt in ("html", "htm", "md", "markdown"):
        resp = client.get(f"/export/reading-list?format={fmt}")
        assert resp.status_code == 200


def test_public_demo_blocks_webhooks(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\npublic_demo = true\n',
        encoding="utf-8",
    )
    app = create_app(config_path)
    with TestClient(app) as client:
        resp = client.post("/webhooks", json={"url": "https://example.com/hook"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "disabled_in_demo"
        resp = client.get("/webhooks")
        assert resp.status_code == 403
        resp = client.delete("/webhooks/1")
        assert resp.status_code == 403


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


def test_export_endpoint(client):
    client.post("/acquire", json={"doi": "10.1000/export.test", "title": "Export Test Paper"})
    resp = client.get("/export?format=bibtex")
    assert resp.status_code == 200
    assert "Export Test Paper" in resp.text
    assert "application/x-bibtex" in resp.headers["content-type"]


def test_events_stream_connects(client):
    with client.stream("GET", "/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        line = next(resp.iter_lines())
        assert "SSE pipeline stream connected" in line


def test_openapi_metadata(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["title"] == "DocumentCrawler Acquisition Server"
    assert "openapi_tags" in data or "tags" in data


def test_acquire_sync_reports_enrich_providers(client):
    resp = client.post("/acquire/sync", json={"doi": "10.1234/test.9"})
    assert resp.status_code == 200
    body = resp.json()
    assert "enriched" in body
    assert isinstance(body["enriched"].get("enrich_providers"), list)


# -----------------------------------------------------------------------------
# Web UI + file download
# -----------------------------------------------------------------------------


def test_root_serves_html_for_browsers(client):
    resp = client.get("/", headers={"accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Document Finder" in resp.text
    assert "/acquire/sync" in resp.text  # the UI talks to the API


def test_root_serves_json_for_api_clients(client):
    resp = client.get("/", headers={"accept": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["service"]


def _file_test_app(tmp_path, make_file: bool):
    dl = tmp_path / "downloads"
    dl.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\ndownload_dir = "{dl.as_posix()}"\ndb_path = "{(tmp_path / "crawler.db").as_posix()}"\n',
        encoding="utf-8",
    )
    app = create_app(config_path)
    pdf = dl / "Smith_2020_paper.pdf"
    if make_file:
        pdf.write_bytes(b"%PDF-1.7 fake-bytes-for-tests")
    return app, pdf


def test_document_file_download(tmp_path):
    from documentcrawler.db import Database
    from documentcrawler.models import DocStatus, DocumentQuery

    app, pdf = _file_test_app(tmp_path, make_file=True)
    # Seed rows with our own connection (the app's handle is thread-bound).
    db = Database(tmp_path / "crawler.db")
    doc_id = db.add_query(DocumentQuery(doi="10.1/x"))
    db.set_status(doc_id, DocStatus.DONE, file_path=str(pdf), sha256="x" * 64)
    db.close()

    with TestClient(app) as tc:
        resp = tc.get(f"/documents/{doc_id}/file")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "Smith_2020_paper.pdf" in resp.headers.get("content-disposition", "")


def test_document_file_errors(tmp_path):
    from documentcrawler.db import Database
    from documentcrawler.models import DocStatus, DocumentQuery

    app, pdf = _file_test_app(tmp_path, make_file=False)  # done doc, but file vanished
    db = Database(tmp_path / "crawler.db")
    gone_id = db.add_query(DocumentQuery(doi="10.1/gone"))
    db.set_status(gone_id, DocStatus.DONE, file_path=str(pdf), sha256="x" * 64)
    nofile_id = db.add_query(DocumentQuery(doi="10.1/nofile"))
    db.close()

    with TestClient(app) as tc:

        assert tc.get("/documents/999999/file").status_code == 404
        assert tc.get(f"/documents/{nofile_id}/file").status_code == 404
        resp = tc.get(f"/documents/{gone_id}/file")
        assert resp.status_code == 410
        assert "no longer available" in resp.json()["error"]["message"]

from fastapi.testclient import TestClient

from documentcrawler.server import create_app


def test_server_api_key_authentication(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\napi_key = "secret123"\n',
        encoding="utf-8",
    )

    app = create_app(config_path)

    # Missing API Key -> 401
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

        # Invalid API Key -> 401
        resp = client.get("/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

        # Valid X-API-Key -> 200
        resp = client.get("/health", headers={"X-API-Key": "secret123"})
        assert resp.status_code == 200

        # Valid Authorization Bearer Token -> 200
        resp = client.get("/health", headers={"Authorization": "Bearer secret123"})
        assert resp.status_code == 200


def test_public_demo_disables_db_mutation_endpoints(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\napi_key = "secret123"\npublic_demo = true\n',
        encoding="utf-8",
    )

    app = create_app(config_path)

    with TestClient(app) as client:
        headers = {"X-API-Key": "secret123"}

        # Even with a valid key, mutation endpoints are refused.
        for path in ["/db/vacuum", "/db/dedupe", "/db/clean", "/db/rerank", "/db/auto-tag", "/db/checkpoint"]:
            resp = client.post(path, headers=headers)
            assert resp.status_code == 403, path
            assert resp.json()["error"]["code"] == "disabled_in_demo"

        # Non-mutation endpoints still work.
        resp = client.get("/health", headers=headers)
        assert resp.status_code == 200


def test_public_demo_false_leaves_mutation_endpoints_reachable(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[general]\ndownload_dir = "downloads"\ndb_path = "crawler.db"\n\n'
        '[server]\napi_key = "secret123"\n',
        encoding="utf-8",
    )

    app = create_app(config_path)

    with TestClient(app) as client:
        resp = client.post("/db/vacuum", headers={"X-API-Key": "secret123"})
        assert resp.status_code == 200

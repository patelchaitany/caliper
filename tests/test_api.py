import pytest
from conftest import VULNERABLE
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIPER_BACKEND", "replay")
    monkeypatch.setenv("CALIPER_LEDGER", str(tmp_path / "api.db"))
    import importlib

    import caliper.api as api_module

    importlib.reload(api_module)
    return TestClient(api_module.api)


def post_review(client, author="dev", **kwargs):
    body = {"author": author, "files": VULNERABLE, "passes": 5}
    body.update(kwargs)
    return client.post("/v1/reviews", json=body)


def test_healthz_reports_the_rubric_in_force(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert len(body["rubric"]) == 32


def test_review_returns_a_score_and_grounded_findings(client):
    body = post_review(client).json()
    assert 0.0 <= body["review"]["score"]["value"] <= 100.0
    assert body["review"]["findings"]
    assert body["detector_precision"] <= 1.0


def test_blast_radius_is_exposed_for_every_file(client):
    body = post_review(client).json()
    assert set(body["blast_radius"]) == set(VULNERABLE)
    assert body["blast_radius"]["svc/auth.py"] > body["blast_radius"]["scripts/oneoff.py"]


def test_identical_request_is_served_from_the_ledger(client):
    first = post_review(client).json()
    second = post_review(client).json()
    assert second["cached"] is True
    assert second["review"]["score"]["value"] == first["review"]["score"]["value"]


def test_empty_submission_is_rejected(client):
    assert client.post("/v1/reviews", json={"author": "d", "files": {}}).status_code == 400


def test_review_can_be_fetched_by_id(client):
    review_id = post_review(client).json()["review"]["review_id"]
    assert client.get(f"/v1/reviews/{review_id}").status_code == 200
    assert client.get("/v1/reviews/missing").status_code == 404


def test_history_reports_comparability(client):
    post_review(client, author="alice")
    body = client.get("/v1/authors/alice/history").json()
    assert body["reviews"] == 1
    assert body["comparable"] is True
    assert "by_category" in body


def test_rubric_endpoint_publishes_the_full_scoring_function(client):
    body = client.get("/v1/rubric").json()
    assert body["version"] and body["hash"]
    assert set(body["severity_weight"]) == {"critical", "high", "medium", "low", "info"}
    assert body["impact_gain"] > 0


def test_passes_are_bounded(client):
    assert post_review(client, passes=99).status_code == 422

"""The Showcase screen: open to everyone, docs from a fixed list only, media from SHOWCASE_DIR."""
from fastapi.testclient import TestClient

from app.main import app


def test_showcase_page_docs_and_media(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")  # public deploy: still open, no sign-in
    c = TestClient(app)
    page = c.get("/showcase")
    assert page.status_code == 200 and "Showcase" in page.text
    names = [d["name"] for d in c.get("/showcase/docs").json()]
    assert {"readme", "story", "testing", "privacy"} <= set(names)
    assert c.get("/showcase/doc/testing").text.startswith("# Testing")
    assert c.get("/showcase/doc/nope").status_code == 404
    assert c.get("/showcase/doc/..%2F.env").status_code == 404
    assert c.get("/showcase-media/../.env").status_code == 404

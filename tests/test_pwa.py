"""Add to Home Screen: the manifest, icons and service worker are served, and pass the password gate."""
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_manifest_icons_and_service_worker_are_served():
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(res.text)
    assert manifest["display"] == "standalone" and manifest["start_url"].startswith("/")
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert "/api/" in sw.text  # API calls are never cached
    page = client.get("/").text
    assert 'rel="manifest"' in page and "serviceWorker" in page


def test_install_files_and_front_door_are_open_admin_is_not(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")
    for path in ("/manifest.webmanifest", "/sw.js", "/pwa/icon-192.png"):
        assert client.get(path).status_code == 200, path
    assert client.get("/").status_code == 200  # the sign-in page; the AI endpoints check for a family
    assert client.get("/admin").status_code == 401

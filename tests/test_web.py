"""Web app: local mode (stateful) and hosted mode (stateless, e.g. Vercel)."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def _upload(client, name: str, **form):
    data = {"files": (io.BytesIO((FIX / name).read_bytes()), name), "flatten": "on", **form}
    return client.post("/scan", data=data, content_type="multipart/form-data")


@pytest.fixture
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))  # so we can check nothing is left behind
    monkeypatch.setenv("UNVEIL_HOME", str(tmp_path / "home"))
    from unveil.web import create_app
    app = create_app(hosted=True)
    app.testing = True
    return app.test_client(), tmp_path


def test_hosted_home_is_honest_about_hosting(hosted_client):
    client, _ = hosted_client
    html = client.get("/").get_data(as_text=True)
    assert "Hosted demo" in html and "Local only" not in html
    assert "never uploaded" not in html
    assert 'name="sender"' not in html  # no sender tracking on a shared server


def test_hosted_scan_returns_everything_in_one_response(hosted_client):
    client, tmp = hosted_client
    resp = _upload(client, "resume_attack.pdf")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Hidden AI instructions found" in html
    assert 'download="resume_attack.unveiled.pdf"' in html
    assert "data:application/pdf;base64," in html
    assert len(resp.data) < 4.5 * 1024 * 1024  # Vercel response limit
    # upload and safe copy are deleted before the response is sent; no database is created
    assert [p.name for p in tmp.iterdir()] == []


def test_hosted_word_file(hosted_client):
    client, _ = hosted_client
    html = _upload(client, "essay_attack.docx").get_data(as_text=True)
    assert "Hidden AI instructions found" in html and 'download="essay_attack.unveiled.pdf"' in html


def test_hosted_rejects_large_upload_with_a_message(hosted_client):
    client, _ = hosted_client
    big = io.BytesIO(b"%PDF-1.4\n" + b"0" * (5 * 1024 * 1024))
    resp = client.post("/scan", data={"files": (big, "big.pdf")}, content_type="multipart/form-data")
    assert resp.status_code == 413
    assert "larger than 4 MB" in resp.get_data(as_text=True)


def test_hosted_senders_page_explains_local_only(hosted_client):
    client, _ = hosted_client
    assert "runs on your own computer" in client.get("/senders").get_data(as_text=True)


def test_vercel_entrypoint_exposes_flask_app(monkeypatch):
    monkeypatch.delenv("UNVEIL_HOSTED", raising=False)
    spec = importlib.util.spec_from_file_location("vercel_app", ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from flask import Flask
    assert isinstance(mod.app, Flask)
    assert "Hosted demo" in mod.app.test_client().get("/").get_data(as_text=True)
    for key in ("UNVEIL_HOSTED", "UNVEIL_WORKERS", "UNVEIL_HOME"):
        os.environ.pop(key, None)


def test_local_mode_still_keeps_results_and_senders(tmp_path):
    from unveil.web import create_app
    app = create_app(db_path=str(tmp_path / "t.db"), workdir=str(tmp_path / "w"), hosted=False)
    client = app.test_client()
    resp = _upload(client, "resume_attack.pdf", sender="x@example.com")
    assert resp.status_code == 302 and "/r/" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Local only" in page and "x@example.com" in page


def test_worker_pool_failure_falls_back_to_one_process(monkeypatch):
    import unveil.parallel as par

    class NoPool:
        def __init__(self, *a, **k):
            raise OSError("no /dev/shm")

    monkeypatch.setattr(par, "ProcessPoolExecutor", NoPool)
    monkeypatch.setattr(par, "_can_spawn", lambda: True)
    chunks = [[0, 1], [2, 3], [4]]
    out = list(par.run_chunks(lambda path, pages: (path, pages), "doc.pdf", chunks))
    assert out == [("doc.pdf", [0, 1, 2, 3, 4])]

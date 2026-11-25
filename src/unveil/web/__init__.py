"""Drag-and-drop web app.

Local mode (`unveil web`): runs on 127.0.0.1, keeps results between pages, tracks senders.

Hosted mode (e.g. on Vercel, switched on by the VERCEL or UNVEIL_HOSTED environment
variable): every request may land on a different short-lived server, so each scan is
handled start to finish in one request inside a temporary folder that is deleted before
the response is sent. The safe copy is returned inside the page. Sender tracking is off,
because a shared server must not mix one visitor's history with another's.
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from .. import report as rpt
from ..models import ScanResult
from ..sanitize import SanitizeOptions, SanitizeResult, sanitize
from ..scanner import SUPPORTED, scan_file
from ..senders import STATUSES, SenderDB

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

HOSTED_MAX_UPLOAD = 4 * 1024 * 1024        # Vercel rejects request bodies over 4.5 MB
HOSTED_MAX_DOWNLOAD = int(2.6 * 1024 * 1024)  # base64 inflates by 4/3; responses are capped at 4.5 MB


def is_hosted() -> bool:
    return bool(os.environ.get("UNVEIL_HOSTED") or os.environ.get("VERCEL"))


@dataclass
class Job:
    id: str
    original: Path
    result: ScanResult
    sender: dict | None
    sanitized: SanitizeResult | None
    error: str | None


def create_app(db_path: str | None = None, workdir: str | None = None, hosted: bool | None = None) -> Flask:
    hosted = is_hosted() if hosted is None else hosted
    if hosted:
        return _create_hosted_app()

    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    work = Path(workdir or tempfile.mkdtemp(prefix="unveil-"))
    db = SenderDB(db_path)
    jobs: dict[str, Job] = {}
    batches: dict[str, list[str]] = {}

    @app.context_processor
    def _globals():
        return {"allow_actions": True, "statuses": STATUSES, "hosted": False}

    @app.get("/")
    def index():
        return render_template("index.html", recent=db.history(limit=12),
                               accept=",".join(sorted(SUPPORTED)), senders=[s["sender"] for s in db.list()])

    @app.post("/scan")
    def scan():
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return redirect(url_for("index"))
        sender = (request.form.get("sender") or "").strip() or None
        flatten = request.form.get("flatten", "on") == "on"
        ids = []
        for up in files:
            jid = uuid.uuid4().hex[:12]
            d = work / jid
            d.mkdir(parents=True)
            original = d / (secure_filename(up.filename) or "upload")
            up.save(original)
            result = scan_file(original)
            before = db.get(sender) if sender else None
            blocked = bool(before and before["status"] == "blocked")
            rec = db.record_scan(sender, original.name, result)
            sanitized, err = None, None
            if flatten:
                if blocked:
                    err = "Safe copy not made: this sender is blocked. Unblock them on the Senders page to process their files."
                else:
                    try:
                        sanitized = sanitize(str(original), result, str(d / "out"), SanitizeOptions())
                    except Exception as exc:
                        err = f"Could not make a safe copy: {exc}"
            jobs[jid] = Job(jid, original, result, rec, sanitized, err)
            ids.append(jid)
        if len(ids) == 1:
            return redirect(url_for("result", jid=ids[0]))
        bid = uuid.uuid4().hex[:12]
        batches[bid] = ids
        return redirect(url_for("batch", bid=bid))

    def _job(jid: str) -> Job:
        job = jobs.get(jid)
        if not job:
            abort(404)
        return job

    @app.get("/r/<jid>")
    def result(jid):
        job = _job(jid)
        ctx = rpt.context(job.result, job.sender, job.sanitized)
        ctx["name"] = job.original.name
        previewable = bool(job.sanitized and job.sanitized.output.endswith((".pdf", ".png")))
        return render_template("result.html", job=job, sanitize_error=job.error,
                               download_url=url_for("download", jid=jid) if job.sanitized else None,
                               preview_url=url_for("preview", jid=jid) if previewable else None, **ctx)

    @app.get("/r/<jid>/preview.png")
    def preview(jid):
        """First page of the safe copy, as the AI will now see it."""
        job = _job(jid)
        if not job.sanitized:
            abort(404)
        out = Path(job.sanitized.output)
        thumb = out.with_suffix(".preview.png")
        if not thumb.exists():
            import pymupdf
            with pymupdf.open(out) as doc:
                page = doc[0]
                zoom = min(2.0, 900 / max(page.rect.width, page.rect.height))
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(thumb)
        return send_file(thumb, mimetype="image/png")

    @app.get("/b/<bid>")
    def batch(bid):
        ids = batches.get(bid) or abort(404)
        return render_template("batch.html", jobs=[jobs[i] for i in ids])

    @app.get("/r/<jid>/download")
    def download(jid):
        job = _job(jid)
        if not job.sanitized:
            abort(404)
        out = Path(job.sanitized.output)
        return send_file(out, as_attachment=True, download_name=out.name)

    @app.get("/r/<jid>/report")
    def report_download(jid):
        job = _job(jid)
        html = rpt.html(job.result, job.sender, job.sanitized)
        tmp = job.original.parent / f"{job.original.stem}.unveil-report.html"
        tmp.write_text(html, encoding="utf-8")
        return send_file(tmp, as_attachment=True, download_name=tmp.name)

    @app.get("/senders")
    def senders():
        return render_template("senders.html", senders=db.list(), history=db.history(limit=100))

    @app.post("/senders/status")
    def sender_status():
        sender = (request.form.get("sender") or "").strip()
        status = request.form.get("status", "")
        if sender and status in STATUSES:
            db.set_status(sender, status, request.form.get("note") or None)
            for job in jobs.values():
                if job.sender and job.sender["sender"] == sender.lower():
                    job.sender = db.get(sender)
        nxt = request.form.get("next") or url_for("senders")
        return redirect(nxt if nxt.startswith("/") else url_for("senders"))

    import atexit
    atexit.register(lambda: shutil.rmtree(work, ignore_errors=True) if not workdir else None)
    return app


# ---------------------------------------------------------------------------------------
# Hosted (stateless) mode
# ---------------------------------------------------------------------------------------

def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _create_hosted_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["MAX_CONTENT_LENGTH"] = HOSTED_MAX_UPLOAD
    accept = ",".join(sorted(SUPPORTED))
    limit_mb = HOSTED_MAX_UPLOAD // (1024 * 1024)

    @app.context_processor
    def _globals():
        return {"allow_actions": False, "statuses": STATUSES, "hosted": True, "limit_mb": limit_mb}

    def _home(error: str | None = None, status: int = 200):
        return render_template("index.html", recent=[], accept=accept, senders=[], error=error), status

    @app.errorhandler(RequestEntityTooLarge)
    def _too_big(_exc):
        return _home(f"That file is larger than {limit_mb} MB, the most this hosted version accepts. "
                     "Run unveil on your own computer for bigger files.", 413)

    @app.get("/")
    def index():
        return _home()

    @app.post("/scan")
    def scan():
        up = next((f for f in request.files.getlist("files") if f and f.filename), None)
        if up is None:
            return redirect(url_for("index"))
        flatten = request.form.get("flatten", "on") == "on"
        with tempfile.TemporaryDirectory(prefix="unveil-") as tmp:
            original = Path(tmp) / (secure_filename(up.filename) or "upload")
            up.save(original)
            result = scan_file(original)
            sanitized, err, download_url, download_name, preview_url = None, None, None, None, None
            if flatten:
                try:
                    sanitized = sanitize(str(original), result, str(Path(tmp) / "out"), SanitizeOptions())
                except Exception as exc:
                    err = f"Could not make a safe copy: {exc}"
            if sanitized:
                out = Path(sanitized.output)
                data = out.read_bytes()
                if len(data) > HOSTED_MAX_DOWNLOAD:
                    err = (f"The safe copy is {len(data) / 1e6:.1f} MB, too large to send back from the hosted "
                           "version. Run unveil on your own computer for long documents.")
                    sanitized = None
                else:
                    mime = {".pdf": "application/pdf", ".png": "image/png"}.get(out.suffix, "text/plain")
                    download_url, download_name = _data_uri(data, mime), out.name
                    if out.suffix in (".pdf", ".png"):
                        import pymupdf
                        with pymupdf.open(out) as doc:
                            page = doc[0]
                            zoom = min(1.5, 700 / max(page.rect.width, page.rect.height))
                            preview_url = _data_uri(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png"),
                                                    "image/png")
            ctx = rpt.context(result, None, sanitized, evidence=True, max_evidence_pages=2)
            ctx["name"] = original.name
            html = render_template("result.html", job=None, sanitize_error=err, download_url=download_url,
                                   download_name=download_name, preview_url=preview_url, **ctx)
        # The temporary folder (upload and safe copy) is gone by the time this is sent.
        return html

    @app.get("/senders")
    def senders():
        return render_template("senders_hosted.html")

    @app.post("/senders/status")
    def sender_status():
        return redirect(url_for("senders"))

    return app

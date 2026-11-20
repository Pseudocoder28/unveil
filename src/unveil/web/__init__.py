"""Local drag-and-drop web app. Runs on 127.0.0.1 only; files never leave the machine."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from .. import report as rpt
from ..models import ScanResult
from ..sanitize import SanitizeOptions, SanitizeResult, sanitize
from ..scanner import SUPPORTED, scan_file
from ..senders import STATUSES, SenderDB

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class Job:
    id: str
    original: Path
    result: ScanResult
    sender: dict | None
    sanitized: SanitizeResult | None
    error: str | None


def create_app(db_path: str | None = None, workdir: str | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    work = Path(workdir or tempfile.mkdtemp(prefix="unveil-"))
    db = SenderDB(db_path)
    jobs: dict[str, Job] = {}
    batches: dict[str, list[str]] = {}

    @app.context_processor
    def _globals():
        return {"allow_actions": True, "statuses": STATUSES}

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
        return render_template("result.html", job=job, sanitize_error=job.error,
                               download_url=url_for("download", jid=jid) if job.sanitized else None, **ctx)

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

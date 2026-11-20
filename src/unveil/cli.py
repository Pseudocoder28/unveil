"""Command line interface.

  unveil scan  FILE_OR_FOLDER...   report hidden AI instructions
  unveil clean FILE_OR_FOLDER...   report + write a flattened safe copy
  unveil senders ...               watch list / blocklist of people who send files
  unveil web                       local drag-and-drop web app
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import report as rpt
from .scanner import SUPPORTED, scan_file

EXIT = {"clean": 0, "suspicious": 1, "malicious": 2}


def _expand(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED
                              and ".unveiled." not in f.name))
        elif p.exists():
            out.append(p)
        else:
            print(f"unveil: no such file: {p}", file=sys.stderr)
    return out


def _db(args):
    if getattr(args, "no_log", False):
        return None
    from .senders import SenderDB
    return SenderDB(getattr(args, "db", None))


def _write_report(args, result, sender, sanitized, src: Path) -> str | None:
    if not args.report:
        return None
    target = Path(args.report)
    if target.suffix.lower() != ".html":
        target.mkdir(parents=True, exist_ok=True)
        target = target / f"{src.stem}.unveil-report.html"
    target.write_text(rpt.html(result, sender, sanitized), encoding="utf-8")
    return str(target)


def cmd_scan(args, do_clean: bool = False) -> int:
    files = _expand(args.files)
    if not files:
        return 64
    db = _db(args)
    worst = 0
    json_out = []
    for f in files:
        result = scan_file(f)
        sender_rec = db.get(args.sender) if (db and args.sender) else None
        blocked = bool(sender_rec and sender_rec["status"] == "blocked")
        prior = [s for s in db.seen_hash(result.sha256)] if db else []
        sender_rec = db.record_scan(args.sender, f.name, result) if db else None

        sanitized, err = None, None
        if do_clean:
            if blocked and not args.force:
                err = f"Not flattening: sender {args.sender} is blocked (use --force to override)."
            else:
                from .sanitize import SanitizeOptions, sanitize
                opts = SanitizeOptions(dpi=args.dpi, snap=0 if args.no_snap else 12, ocr=args.ocr,
                                       image_format="jpeg" if args.jpeg else "png", docx_engine=args.docx_engine)
                try:
                    sanitized = sanitize(str(f), result, args.output, opts)
                except Exception as exc:
                    err = f"Could not flatten: {exc}"

        if args.json:
            json_out.append(rpt.to_json(result, sender_rec, sanitized))
        else:
            print(rpt.console(result, sender_rec, sanitized, show_info=args.all))
            if prior:
                bad = [p for p in prior if p["verdict"] == "malicious"]
                print(rpt._c("2", f"  Seen this exact file {len(prior)} time(s) before"
                                  + (f" ({len(bad)} flagged malicious)" if bad else "") + ".\n"))
            if err:
                print(rpt._c("1;31", "  " + err) + "\n")
        path = _write_report(args, result, sender_rec, sanitized, f)
        if path and not args.json:
            print(rpt._c("2", f"  Report: {path}\n"))
        worst = max(worst, EXIT[result.verdict])
    if args.json:
        print(json_out[0] if len(json_out) == 1 else "[\n" + ",\n".join(json_out) + "\n]")
    return worst


def cmd_senders(args) -> int:
    from .senders import SenderDB
    db = SenderDB(args.db)
    action = args.action
    if action == "list":
        rows = db.list()
        if not rows:
            print("No senders recorded yet. Use --sender when scanning to track who sent each file.")
            return 0
        print(f"{'SENDER':36} {'STATUS':9} {'FILES':>5} {'BAD':>4}  NOTE")
        for r in rows:
            print(f"{r['sender'][:36]:36} {r['status']:9} {r['scans']:>5} {r['malicious']:>4}  {r['note']}")
        return 0
    if action == "history":
        for h in db.history(args.sender):
            print(f"{h['scanned_at']}  {h['verdict']:10} {h['score']:>3}  {h['sender'] or '-':28} {h['filename']}")
        return 0
    if not args.sender:
        print("unveil senders: this action needs a sender (email or name)", file=sys.stderr)
        return 64
    if action == "show":
        rec = db.get(args.sender)
        if not rec:
            print(f"{args.sender}: not seen before")
            return 0
        for k in ("sender", "status", "note", "scans", "malicious", "suspicious", "last_seen"):
            print(f"{k:11} {rec[k]}")
        return 0
    if action == "remove":
        db.remove(args.sender)
        print(f"Removed {args.sender}")
        return 0
    status = {"block": "blocked", "unblock": "unknown", "trust": "trusted", "watch": "watch"}[action]
    db.set_status(args.sender, status, args.note)
    print(f"{args.sender} → {status}")
    return 0


def cmd_web(args) -> int:
    from .web import create_app
    app = create_app(db_path=args.db)
    url = f"http://{args.host}:{args.port}"
    print(f"unveil web UI on {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unveil", description=(
        "Find hidden AI prompt injections in PDFs, Word files and more, then rebuild the file from "
        "screenshots so only what a human can see survives."))
    p.add_argument("--version", action="version", version=f"unveil {__version__}")
    p.add_argument("--db", help="sender database path (default ~/.unveil/unveil.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("files", nargs="+", help="files or folders")
        sp.add_argument("-s", "--sender", help="who sent the file (email or name); tracked in the sender list")
        sp.add_argument("--json", action="store_true", help="print JSON instead of the console report")
        sp.add_argument("--report", metavar="PATH", help="also write an HTML report (file.html or a folder)")
        sp.add_argument("--all", action="store_true", help="show informational items too")
        sp.add_argument("--no-log", action="store_true", help="do not record this scan in the sender database")

    s = sub.add_parser("scan", help="report hidden AI instructions")
    common(s)

    c = sub.add_parser("clean", help="report and write a flattened safe copy")
    common(c)
    c.add_argument("-o", "--output", help="folder for safe copies (default: next to the original)")
    c.add_argument("--dpi", type=int, default=150, help="render resolution (default 150)")
    c.add_argument("--ocr", action="store_true", help="add an OCR text layer from the visible pixels (needs Tesseract)")
    c.add_argument("--jpeg", action="store_true", help="smaller files using JPEG pages")
    c.add_argument("--no-snap", action="store_true", help="do not snap near-white/near-black pixels")
    c.add_argument("--docx-engine", choices=["auto", "libreoffice", "builtin"], default="auto")
    c.add_argument("--force", action="store_true", help="flatten even if the sender is blocked")

    sd = sub.add_parser("senders", help="manage the watch list / blocklist")
    sd.add_argument("action", choices=["list", "show", "block", "unblock", "trust", "watch", "remove", "history"])
    sd.add_argument("sender", nargs="?")
    sd.add_argument("--note", help="reason, e.g. 'hidden prompt in CV, Nov 2026'")

    w = sub.add_parser("web", help="start the local web app")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "clean":
        return cmd_scan(args, do_clean=True)
    if args.cmd == "senders":
        return cmd_senders(args)
    if args.cmd == "web":
        return cmd_web(args)
    return 64


if __name__ == "__main__":
    sys.exit(main())

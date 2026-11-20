"""Time scan and clean on the benchmark files:  python bench/bench.py"""
import sys, tempfile, time
from pathlib import Path
from unveil.scanner import scan_file
from unveil.sanitize import sanitize

DATA = Path(__file__).parent / "data"


def main():
    for name in sys.argv[1:] or ["big.pdf", "big.docx", "../../tests/fixtures/resume_attack.pdf"]:
        f = DATA / name
        t0 = time.perf_counter(); r = scan_file(f); t1 = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            s = sanitize(str(f), r, tmp); t2 = time.perf_counter()
            size = Path(s.output).stat().st_size
        print(f"{f.name:22} pages={r.pages or s.pages:<4} scan={t1-t0:6.2f}s  clean={t2-t1:6.2f}s  "
              f"out={size/1e6:6.2f}MB  verdict={r.verdict} findings={len([x for x in r.findings if x.severity!='info'])}")


if __name__ == "__main__":
    main()

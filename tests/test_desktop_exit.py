"""Quitting the desktop app: an orderly stop, then the process ends without MuPDF calling back into Python."""
import subprocess
import sys
import textwrap


def _run(body: str, tmp_path):
    script = tmp_path / "quit.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)


def test_exit_runs_cleanup_then_ends_with_pending_pdf_warnings(tmp_path):
    marker = tmp_path / "cleaned"
    r = _run(f"""
        import threading, pymupdf
        from papermap import desktop

        def work():  # a worker thread leaves MuPDF warnings behind, as translation threads do
            pymupdf.mupdf.fz_warn("pending warning in a worker")
        t = threading.Thread(target=work); t.start(); t.join()
        pymupdf.mupdf.fz_warn("pending warning on the main thread")
        desktop.exit_now(lambda: open({str(marker)!r}, "w").write("ok"))
        raise SystemExit("exit_now returned")
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "ok"


def test_exit_still_ends_when_cleanup_fails(tmp_path):
    r = _run("""
        from papermap import desktop

        def broken():
            raise RuntimeError("server did not stop")
        desktop.exit_now(broken, 3)
    """, tmp_path)
    assert r.returncode == 3 and "cleanup before exit failed" in r.stderr

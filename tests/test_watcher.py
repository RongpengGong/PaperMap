import time

from papermap.watcher import Debouncer, iter_inbox_pdfs


def test_debouncer_waits_for_stable_size(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"a")
    out = []
    d = Debouncer(out.append, quiet_s=0.2)
    d.touch(str(f))
    d.check_once()
    assert out == []
    f.write_bytes(b"ab")  # still growing
    d.check_once()
    time.sleep(0.25)
    d.check_once()
    assert out == [f]


def test_startup_scan_respects_exclusions(config, pdfs):
    priv = config.inbox_dirs[0] / "_private"
    priv.mkdir()
    (priv / "secret.pdf").write_bytes(b"%PDF")
    names = {p.name for p in iter_inbox_pdfs(config)}
    assert "secret.pdf" not in names and "paper.pdf" in names

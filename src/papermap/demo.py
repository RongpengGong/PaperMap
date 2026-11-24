"""Sample library for new users: a few open-access (CC BY) papers downloaded from the official PMC
open-access collection and imported the normal way, into their own group so they are easy to delete."""
from __future__ import annotations

import threading

from . import tracking

GROUP = "示例论文 · Sample papers"
PAPERS = [  # (DOI, PMCID) - all CC BY, PDFs from the PMC open-access collection
    ("10.1186/s13059-017-1382-0", "PMC5802054"),   # Scanpy (Genome Biology 2018)
    ("10.1186/s13059-019-1874-1", "PMC6927181"),   # sctransform (Genome Biology 2019)
    ("10.1038/s41592-021-01358-2", "PMC8828470"),  # Squidpy (Nature Methods 2022)
    ("10.1038/s41467-023-37168-7", "PMC10027878"), # deconvolution benchmark (Nat Commun 2023)
    ("10.1186/s13059-022-02601-5", "PMC8783472"),  # zero-inflation in scRNA-seq (Genome Biology 2022)
    ("10.1038/s41467-022-32283-3", "PMC9357016"),  # single-cell atlas of HCC (Nat Commun 2022)
]
_state = {"running": False, "done": 0, "total": len(PAPERS), "added": 0, "failed": 0}
_lock = threading.Lock()


def status() -> dict:
    return dict(_state)


def start(pipeline) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, added=0, failed=0)
    threading.Thread(target=_run, args=(pipeline,), daemon=True, name="demo-import").start()
    return True


def _run(pipeline) -> None:
    registry, config = pipeline.registry, pipeline.config
    try:
        cid = next((r["id"] for r in registry.query("SELECT id FROM collections WHERE name=?", (GROUP,))), None) \
            or registry.create_collection(GROUP)
        for doi, pmcid in PAPERS:
            try:
                row = registry.paper_by_doi(doi)
                if row is None:
                    path = None
                    for url in tracking.pmc_pdf_urls(pmcid):
                        path = tracking._download(url, config.imported_dir, f"{pmcid}.pdf")  # noqa: SLF001
                        if path:
                            break
                    if path is None:
                        raise RuntimeError("download failed")
                    pipeline.handle_path(path)
                    row = registry.paper_by_doi(doi)
                    _state["added"] += 1
                if row is not None:
                    registry.add_to_collection(cid, [row["id"]])
            except Exception as exc:  # noqa: BLE001 - one paper failing does not stop the others
                _state["failed"] += 1
                registry.log("warning", f"Sample paper {doi} could not be imported: {exc}")
            _state["done"] += 1
        registry.log("info", f"Sample library: {_state['added']} paper(s) imported into '{GROUP}'")
    finally:
        _state["running"] = False

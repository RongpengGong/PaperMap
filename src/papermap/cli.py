"""Command line entry point: papermap init / scan / run / serve / reprocess / status."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from . import config as cfgmod
from .registry import Registry

PRIVACY_NOTICE = (
    "Privacy: the text of each new paper (title, abstract, key sections) is sent to the "
    "LLM provider you configure (llm.base_url). PDFs in excluded folders/patterns are never sent.\n"
    "隐私提示：每篇新论文的文本（标题、摘要、关键章节）会发送到你配置的 LLM 服务商；"
    "被排除规则命中的 PDF 永远不会发送。"
)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    ans = _ask(f"{prompt} ({' / '.join(options)})", default)
    while ans not in options:
        ans = _ask(f"Please type one of: {' / '.join(options)}", default)
    return ans


def cmd_init(args: argparse.Namespace) -> int:
    print(f"PaperMap {__version__} setup\n")
    data_dir = _ask("Data folder (config, database, knowledge base)", cfgmod.DEFAULT_DATA_DIR)
    cfg_path = Path(args.config).expanduser() if args.config else Path(data_dir).expanduser() / "config.yaml"
    if cfg_path.exists() and _ask(f"{cfg_path} exists. Overwrite? (y/N)", "n").lower() != "y":
        print("Aborted; existing config kept.")
        return 1
    inbox = _ask("Inbox folder to watch (PDFs from Zotero or downloads)", str(Path.cwd() / "inbox"))
    lang = _ask("Card language: zh / en / bilingual", "zh")
    while lang not in ("zh", "en", "bilingual"):
        lang = _ask("Please type zh, en or bilingual", "zh")
    print("\nLLM provider:\n  1) DeepSeek official (just paste your API key)\n  2) Custom (OpenAI Chat/Responses, Anthropic or Gemini compatible endpoint)")
    choice = _ask("Choose 1 or 2", "1")
    llm: dict = {}
    print("You can leave the key empty and set it later on the web Settings page.")
    if choice == "2":
        llm["provider"] = "custom"
        llm["custom"] = {
            "base_url": _ask("Base URL", ""),
            "protocol": _ask_choice("API protocol", list(cfgmod.PROTOCOLS), "openai"),
            "model": _ask("Model name", ""),
            "api_key": getpass.getpass("API key (input hidden, Enter to skip): ").strip(),
        }
        api_key = llm["custom"]["api_key"]
    else:
        pre = cfgmod.PROVIDERS["deepseek"]
        llm["provider"] = "deepseek"
        llm["model"] = _ask(f"Model ({' / '.join(pre['models'])})", pre["default_model"])
        api_key = getpass.getpass(f"API key from {pre['key_url']} (input hidden, Enter to skip): ").strip()
        llm["api_key"] = api_key
    from .taxonomy import BUILTIN

    print("\nClassification scheme (editable later on the web Taxonomy page):")
    for i, (key, label) in enumerate(BUILTIN.items(), 1):
        print(f"  {i}) {key}: {label}")
    pick = _ask("Choose", "1")
    keys = list(BUILTIN)
    taxonomy_file = keys[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(keys) else keys[0]
    email = _ask("Contact e-mail for Crossref/OpenAlex polite pool (optional)", "")
    budget = _ask("Monthly LLM budget in USD (0 = unlimited)", "5")

    raw = {
        "inbox_dirs": [inbox],
        "data_dir": data_dir,
        "llm": {**llm, "monthly_budget_usd": float(budget or 0)},
        "output_language": lang,
        "taxonomy_file": taxonomy_file,
        "metadata": {"contact_email": email},
    }
    cfg = cfgmod.from_dict(raw)
    cfg.save(cfg_path)
    if api_key:
        try:
            cfg_path.chmod(0o600)
        except OSError:
            pass
    cfg.ensure_dirs()
    Path(inbox).expanduser().mkdir(parents=True, exist_ok=True)
    Registry(cfg.db_path).close()
    print(f"\nConfig written to {cfg_path}")
    if cfg_path != cfgmod.default_config_path():
        print(f"Use it with: papermap --config {cfg_path} <command>  (or set {cfgmod.CONFIG_ENV})")
    print("\n" + PRIVACY_NOTICE)
    print("\nNext: `papermap scan` for a one-off scan, or `papermap run` to watch the folder.")
    return 0


def _load(args: argparse.Namespace):
    from .pipeline import Pipeline

    cfg = cfgmod.load(args.config)
    cfg.ensure_dirs()
    _file_logging(cfg.logs_dir)
    reg = Registry(cfg.db_path)
    pipe = Pipeline(cfg, reg)
    try:
        pipe.migrate_library()
    except Exception:  # noqa: BLE001 - never block startup on the migration
        logging.getLogger(__name__).exception("library migration failed")
    if getattr(args, "func", None) in (cmd_run, cmd_serve, cmd_app):
        threading.Thread(target=pipe.reindex_all, name="reindex", daemon=True).start()
    return cfg, reg, pipe


def _file_logging(log_dir: Path) -> None:
    """Also log to <data>/logs/papermap.log (the packaged app has no console)."""
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    h = RotatingFileHandler(log_dir / "papermap.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)


def _key_hint(cfg) -> None:
    if not cfg.api_key():
        print("⚠ No LLM API key set: new papers are detected and registered, but cards wait "
              "until you add a key on the web Settings page (papermap serve).")


def cmd_scan(args: argparse.Namespace) -> int:
    from .watcher import iter_inbox_pdfs

    cfg, reg, pipe = _load(args)
    print(PRIVACY_NOTICE.splitlines()[0])
    _key_hint(cfg)
    # Phase 1: register files (DOI + metadata, no LLM)
    pipe.hold = True
    counts: dict[str, int] = {}
    for path in iter_inbox_pdfs(cfg):
        outcome = pipe.handle_path(path)
        print(f"{outcome:>20}  {path}")
        counts[outcome] = counts.get(outcome, 0) + 1
    print("\nFiles:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no PDFs found")

    # Phase 2: LLM step, with a cost check for large batches
    pending = reg.query("SELECT id, title FROM papers WHERE status IN ('new','extracting') ORDER BY id")
    if pending and cfg.api_key():
        est = pipe.estimate(len(pending))
        if len(pending) >= int(cfg.raw.get("bulk_confirm_threshold", 5)) and not args.yes:
            print(f"\n{len(pending)} papers need the LLM: ~{est['tokens_in'] / 1e6:.2f}M input + "
                  f"{est['tokens_out'] / 1e6:.2f}M output tokens.")
            if est["priced"]:
                print(f"Estimated cost: ${est['cost_offpeak']:.3f} (off-peak) - ${est['cost_peak']:.3f} (peak); "
                      f"spent this month ${est['spent']:.3f}" + (f" of ${est['budget']:.2f}" if est["budget"] else ""))
            else:
                print("No prices configured, so no cost estimate (Settings -> prices).")
            if not sys.stdin.isatty():
                print("Not an interactive terminal: re-run with --yes to process them.")
                return 0
            if _ask("Process them now? (y/N)", "n").lower() != "y":
                print("Not processed. They stay queued; run `papermap scan` again (or confirm on the web page).")
                return 0
        pipe.hold = False
        for i, row in enumerate(pending, 1):
            try:
                outcome = pipe.process_paper(row["id"])
            except KeyboardInterrupt:
                print("\nInterrupted - progress is saved; run `papermap scan` again to continue.")
                return 130
            print(f"[{i}/{len(pending)}] {outcome:<12} {(row['title'] or '')[:80]}")
            if outcome in ("waiting_llm", "retry_later"):
                print("Stopping: the LLM is not available right now (see `papermap status` / activity log).")
                break
    pipe.hold = False
    extra = pipe.classify_papers(mode="unclassified")
    if extra:
        print("Classification:", ", ".join(f"{k}={v}" for k, v in extra.items()))
    _print_status(reg)
    return 0


def _web_url(cfg, path: str = "/") -> str:
    return f"http://{cfg.web['host']}:{int(cfg.web['port'])}{path}"


def _already_running(cfg) -> bool:
    import httpx

    try:
        return httpx.get(_web_url(cfg, "/static/style.css"), timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    from .tray import open_url

    threading.Timer(delay, lambda: open_url(url)).start()


def cmd_app(args: argparse.Namespace) -> int:
    """Desktop-app mode (double-click / login item): create a default config on
    first use, run the watcher + web UI, show a menu-bar/tray icon, and open
    the browser unless --background."""
    path = Path(args.config).expanduser() if args.config else cfgmod.default_config_path()
    first_run = not path.exists()
    if first_run:
        home = path.parent
        cfg = cfgmod.from_dict({"inbox_dirs": [str(home / "inbox")], "data_dir": str(home),
                                "taxonomy_file": "default"}, path=path)
        cfg.save()
        (home / "inbox").mkdir(parents=True, exist_ok=True)
        print(f"Created {path}")
    cfg = cfgmod.load(path)
    if _already_running(cfg):
        if not getattr(args, "background", False):
            import httpx

            try:
                shown = httpx.post(_web_url(cfg, "/api/app/show"), timeout=3).json().get("ok")
            except (httpx.HTTPError, ValueError):
                shown = False
            if not shown:
                from .tray import open_url

                open_url(_web_url(cfg))
        print("PaperMap is already running.")
        return 0
    args.config = str(path)
    start_page = "/settings?welcome=1" if first_run else "/"

    from . import desktop, tray

    if not getattr(args, "no_window", False) and desktop.available():
        cfg, reg, pipe = _load(args)
        pipe.migrate_library()
        return desktop.run(cfg, reg, pipe, background=getattr(args, "background", False), start_page=start_page)

    if getattr(args, "no_tray", False) or not tray.available():
        args.no_web = False
        args.open = None if getattr(args, "background", False) else start_page
        return cmd_run(args)

    import uvicorn

    from .watcher import Watcher
    from .web.app import create_app

    cfg, reg, pipe = _load(args)
    watcher = Watcher(cfg, pipe)
    threading.Thread(target=watcher.start, name="watcher-start", daemon=True).start()  # may wait on OS permissions
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, reg, pipe), host=cfg.web["host"],
                                           port=int(cfg.web["port"]), log_level="warning"))
    threading.Thread(target=server.run, name="web", daemon=True).start()
    if not getattr(args, "background", False):
        _open_browser_later(_web_url(cfg, start_page))

    def quit_all() -> None:
        server.should_exit = True
        watcher.stop()
        threading.Timer(2.0, lambda: os._exit(0)).start()

    tray.run(_web_url(cfg), cfg.knowledge_dir, cfg.inbox_dirs, cfg.path, cfg.logs_dir, quit_all)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .watcher import Watcher

    cfg, reg, pipe = _load(args)
    if not getattr(args, "no_web", False) and _already_running(cfg):
        # exit 0 so launchd/systemd do not restart-loop when another instance runs
        print(f"PaperMap is already running at {_web_url(cfg)} - not starting a second copy.")
        return 0
    print(PRIVACY_NOTICE)
    _key_hint(cfg)
    watcher = Watcher(cfg, pipe)
    watcher.start()
    print("Watching:", ", ".join(str(p) for p in cfg.inbox_dirs))
    if getattr(args, "open", None):
        _open_browser_later(_web_url(cfg, args.open))
    if args.no_web:
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        stop.wait()
    else:
        _serve(cfg, reg, pipe)
    watcher.stop()
    return 0


def _serve(cfg, reg, pipe) -> None:
    import uvicorn

    from .web.app import create_app

    host, port = cfg.web["host"], int(cfg.web["port"])
    print(f"Web UI: http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg, reg, pipe), host=host, port=port, log_level="warning")


def cmd_serve(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    if getattr(args, "port", None):
        cfg.raw["web"]["port"] = args.port
    _serve(cfg, reg, pipe)
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    if args.paper_id:
        rows = [r for r in (reg.get_paper(i) for i in args.paper_id) if r]
    elif args.prompt_version:
        rows = reg.query("SELECT * FROM papers WHERE prompt_version=?", (args.prompt_version,))
    elif args.status:
        rows = reg.list_papers(status=args.status, limit=100000)
    else:
        print("Specify --paper-id, --prompt-version or --status")
        return 2
    print(f"Reprocessing {len(rows)} paper(s)")
    for r in rows:
        if r["status"] in ("doi_unverified", "skipped_supplement") and not args.status:
            print(f"  #{r['id']}: status {r['status']} - skipped (no card for these)")
            continue
        reg.update_paper(r["id"], status="new")
        print(f"  #{r['id']}: {pipe.process_paper(r['id'], force=True)}")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    tax = pipe.taxonomy()
    mode = "all" if args.all else "stale" if args.stale else "unclassified"
    print(f"Taxonomy: {tax.name} ({len(tax.axis('method'))} methods, {len(tax.axis('topic'))} topics)")
    _key_hint(cfg)
    counts = pipe.classify_papers(mode=mode, paper_ids=args.paper_id)
    pipe.refresh_fields()
    print("Result:", ", ".join(f"{k}={v}" for k, v in counts.items()) or "nothing to classify")
    n = pipe.stale_classification_count()
    if n:
        print(f"{n} paper(s) were classified with an older taxonomy/prompt; run `papermap classify --stale`.")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    print(f"Indexed {pipe.reindex_all(force=args.force)} paper(s) for full-text search")
    return 0


def cmd_autostart(args: argparse.Namespace) -> int:
    from . import autostart

    cfg = cfgmod.load(args.config)
    if args.action == "status":
        print("Autostart:", autostart.status())
        print(autostart.describe(cfg.path, cfg.logs_dir))
    elif args.action == "install":
        print(autostart.install(cfg.path, cfg.logs_dir, start_now=not args.no_start))
        print(f"PaperMap now starts at login; web UI at {_web_url(cfg)}")
    else:
        print(autostart.uninstall())
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    pipe.taxonomy()
    if args.slug:
        for slug in args.slug:
            pipe.consolidate_field(slug)
            print(f"{slug}: consolidated")
    else:
        done = pipe.consolidate_due()
        print("Consolidated:", ", ".join(done) or "nothing due")
    return 0


def cmd_handbook(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    pipe.taxonomy()
    slugs = args.slug or [f["slug"] for f in reg.active_fields() if reg.field_paper_rows(f["id"])]
    for slug in slugs:
        try:
            print(f"{slug}: knowledge/{pipe.generate_handbook(slug)}")
        except Exception as exc:  # noqa: BLE001
            print(f"{slug}: failed - {exc}")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    cfg, reg, pipe = _load(args)
    pipe.taxonomy()
    t = pipe.new_field_threshold
    pipe.migrate_legacy_proposals()
    rows = reg.pending_fields("open")
    if not rows:
        print("No pending fields.")
    for r in rows:
        flag = "READY" if r["n_papers"] >= t else "     "
        print(f"{flag} #{r['id']:<3} {r['axis']:<6} {r['n_papers']}/{t}  {r['slug']:<30} {r['name_zh']} / {r['name_en']}")
    if args.establish:
        print("established:", pipe.establish_pending(args.establish))
    if args.reject:
        pipe.reject_pending(args.reject)
        print("rejected", args.reject)
    if args.merge:
        pid, target = args.merge
        pipe.merge_pending(int(pid), target)
        print(f"merged {pid} into {target}")
    return 0


def _print_status(reg: Registry) -> None:
    counts = reg.status_counts()
    print("Library:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty",
          f"| LLM cost this month: ${reg.month_cost():.4f}")


def cmd_status(args: argparse.Namespace) -> int:
    cfg, reg, _ = _load(args)
    _print_status(reg)
    for r in reg.list_papers(limit=args.limit):
        print(f"#{r['id']:<4} {r['status']:<18} {r['doi'] or r['doi_candidate'] or '-':<32} {(r['title'] or '')[:70]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="papermap", description="Local literature knowledge map")
    ap.add_argument("--config", help=f"config.yaml path (default: ${cfgmod.CONFIG_ENV} or ~/PaperMap/config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init", help="interactively create a config file").set_defaults(func=cmd_init)
    p = sub.add_parser("scan", help="scan inbox folders once and process new PDFs")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask before processing many papers")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("run", help="watch inbox folders continuously (+ web UI)")
    p.add_argument("--no-web", action="store_true", help="do not start the web UI")
    p.add_argument("--open", nargs="?", const="/", default=None, help="open the web UI in the browser")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("serve", help="start the local web UI only")
    p.add_argument("--port", type=int, help="override web.port (e.g. to run next to the app)")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("reprocess", help="re-run LLM extraction for selected papers")
    p.add_argument("--paper-id", type=int, nargs="+")
    p.add_argument("--prompt-version")
    p.add_argument("--status", choices=["error", "new", "done"])
    p.set_defaults(func=cmd_reprocess)
    p = sub.add_parser("classify", help="classify papers into the taxonomy (default: unclassified only)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stale", action="store_true", help="also re-classify papers made with an older taxonomy")
    g.add_argument("--all", action="store_true", help="re-classify every paper")
    p.add_argument("--paper-id", type=int, nargs="+")
    p.set_defaults(func=cmd_classify)
    p = sub.add_parser("handbook", help="generate study handbooks (default: every field with papers)")
    p.add_argument("slug", nargs="*")
    p.set_defaults(func=cmd_handbook)
    p = sub.add_parser("pending", help="list proposed new fields; establish / merge / reject them")
    p.add_argument("--establish", type=int, metavar="ID")
    p.add_argument("--reject", type=int, metavar="ID")
    p.add_argument("--merge", nargs=2, metavar=("ID", "TARGET_SLUG"))
    p.set_defaults(func=cmd_pending)
    p = sub.add_parser("reindex", help="rebuild the full-text search index")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_reindex)
    p = sub.add_parser("consolidate", help="consolidate field hub pages (default: those that are due)")
    p.add_argument("slug", nargs="*")
    p.set_defaults(func=cmd_consolidate)
    p = sub.add_parser("autostart", help="start PaperMap automatically at login")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--no-start", action="store_true", help="only register; start at next login")
    p.set_defaults(func=cmd_autostart)
    p = sub.add_parser("app", help="desktop mode: first-run setup, run with a menu-bar icon, open the browser")
    p.add_argument("--background", action="store_true", help="do not open the browser (used at login)")
    p.add_argument("--no-tray", action="store_true", help="no menu-bar/tray icon")
    p.add_argument("--no-window", action="store_true", help="browser UI instead of the native window")
    p.set_defaults(func=cmd_app)
    p = sub.add_parser("status", help="show library summary")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    # Windowed builds (no console) have no stdout/stderr on Windows
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.cmd is None:  # plain `papermap` / double-clicked executable
        args.func = cmd_app
        args.background = False
        args.no_tray = False
        args.no_window = False
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return int(args.func(args) or 0)
    except cfgmod.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

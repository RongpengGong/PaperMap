"""Command line entry point: papermap init / scan / run / serve / reprocess / status."""

from __future__ import annotations

import argparse
import getpass
import logging
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
    email = _ask("Contact e-mail for Crossref/OpenAlex polite pool (optional)", "")
    budget = _ask("Monthly LLM budget in USD (0 = unlimited)", "5")

    raw = {
        "inbox_dirs": [inbox],
        "data_dir": data_dir,
        "llm": {**llm, "monthly_budget_usd": float(budget or 0)},
        "output_language": lang,
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
    reg = Registry(cfg.db_path)
    return cfg, reg, Pipeline(cfg, reg)


def _key_hint(cfg) -> None:
    if not cfg.api_key():
        print("⚠ No LLM API key set: new papers are detected and registered, but cards wait "
              "until you add a key on the web Settings page (papermap serve).")


def cmd_scan(args: argparse.Namespace) -> int:
    from .watcher import iter_inbox_pdfs

    cfg, reg, pipe = _load(args)
    print(PRIVACY_NOTICE.splitlines()[0])
    _key_hint(cfg)
    counts: dict[str, int] = {}
    for path in iter_inbox_pdfs(cfg):
        print(f"… {path.name}", end="", flush=True)
        outcome = pipe.handle_path(path)
        print(f"\r{outcome:>20}  {path}")
        counts[outcome] = counts.get(outcome, 0) + 1
    pending = pipe.process_pending()
    for k, v in pending.items():
        counts[f"pending:{k}"] = v
    print("\nSummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no PDFs found")
    _print_status(reg)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .watcher import Watcher

    cfg, reg, pipe = _load(args)
    print(PRIVACY_NOTICE)
    _key_hint(cfg)
    watcher = Watcher(cfg, pipe)
    watcher.start()
    print("Watching:", ", ".join(str(p) for p in cfg.inbox_dirs))
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="interactively create a config file").set_defaults(func=cmd_init)
    sub.add_parser("scan", help="scan inbox folders once and process new PDFs").set_defaults(func=cmd_scan)
    p = sub.add_parser("run", help="watch inbox folders continuously (+ web UI)")
    p.add_argument("--no-web", action="store_true", help="do not start the web UI")
    p.set_defaults(func=cmd_run)
    sub.add_parser("serve", help="start the local web UI only").set_defaults(func=cmd_serve)
    p = sub.add_parser("reprocess", help="re-run LLM extraction for selected papers")
    p.add_argument("--paper-id", type=int, nargs="+")
    p.add_argument("--prompt-version")
    p.add_argument("--status", choices=["error", "new", "done"])
    p.set_defaults(func=cmd_reprocess)
    p = sub.add_parser("status", help="show library summary")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

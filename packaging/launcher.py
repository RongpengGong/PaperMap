"""Entry point for the PyInstaller build: double-click starts PaperMap."""
import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["--plugin-worker"]:
        from papermap.plugin_worker import main as worker_main

        sys.exit(worker_main(sys.argv[2:]))
    from papermap.cli import main

    sys.exit(main())

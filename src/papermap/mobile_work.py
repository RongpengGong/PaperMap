"""Opt-in desktop processing for one paired session. Only accepted jobs outlive pairing."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

import psutil

from . import evidence, mobile_sync, translate
from .kb_writer import card_filename
from .pdf_import import publish_pdf

MAX_PDF = 256 * 1024 * 1024
CHUNK = 1024 * 1024
_LIMIT = 256
_SHA = re.compile(r"[a-f0-9]{64}")
_BLOB = re.compile(r"\.papermap-phone-([1-9][0-9]{0,9})-[a-zA-Z0-9_-]+\.part")
_REVIEW = "这份 PDF 需要在电脑的待审核页中核对。"
_SELECTION = "这篇论文已不在电脑文献库，或电脑上的 PDF 已换版。"
_MODEL = "请在电脑设置中配置可用的主模型，并检查模型额度。"
_TRANSLATOR = "请在电脑设置中配置默认翻译服务，并完成所需的授权。"
_CHANGED = "电脑端 PDF 版本已变化，请重新核对共享范围。"
_GENERIC = "处理失败，请在电脑的任务记录中查看详情，再重试。"
_HOLD = "电脑端批量导入正在等待确认，请在电脑首页处理后重试。"
_CHECKSUM = "接收的文件与手机上的 PDF 不一致，请重新上传。"
_UNREADABLE = "无法打开这份 PDF，请检查文件是否损坏或受密码保护。"


def reap_uploads(pipe):
    """Remove interrupted transfers only after their owning process and desktop job have ended."""
    folder = pipe.config.imported_dir
    files = list(folder.glob(".papermap-phone-*.part"))
    if not files:
        return
    jobs = pipe.registry.query("SELECT options_json FROM jobs WHERE kind='mobile_import' AND status IN ('queued','running')")
    active = set()
    for row in jobs:
        try:
            active.add(json.loads(row["options_json"])["upload"])
        except (ValueError, TypeError, KeyError):
            continue
    for path in files:
        name = _BLOB.fullmatch(path.name)
        if name and int(name[1]) < 2**31 and path.name not in active and not psutil.pid_exists(int(name[1])):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class WorkError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def import_pdf(pipe, job):
    """Use the desktop's identification/dedup rules without altering an existing private paper."""
    name = json.loads(job["options_json"])["upload"]
    revision = job["arg"]
    if not _BLOB.fullmatch(name) or not _SHA.fullmatch(revision):
        raise ValueError("Invalid upload")
    tmp = pipe.config.imported_dir / name
    try:
        if tmp.is_symlink() or not tmp.is_file() or not 0 < tmp.stat().st_size <= MAX_PDF:
            raise ValueError("Invalid upload")
        if evidence.document_revision(tmp) != revision:
            raise ValueError(_CHECKSUM)
        try:
            with mobile_sync._document(tmp):
                pass
        except ValueError as exc:
            raise ValueError(_UNREADABLE) from exc
        with pipe._lock:
            known = pipe.registry.file_by_sha(revision)
            if known and known["paper_id"]:
                return json.dumps({"existing": True, "paperId": known["paper_id"]})
            path = publish_pdf(tmp, "Mobile-" + revision[:16] + ".pdf")
            try:
                result = pipe._handle_path(path, register_only=True)
            finally:
                # Files without a new row include duplicate titles, DOI aliases and metadata failures.
                entry = pipe.registry.file_by_sha(revision)
                if not entry:
                    path.unlink(missing_ok=True)
            if result == "existing":
                raise ValueError(_SELECTION)
            if result != "registered" or not entry or not entry["paper_id"]:
                raise ValueError(_REVIEW if result in ("doi_unverified", "skipped_supplement") else _GENERIC)
            pid = entry["paper_id"]
            row = pipe.registry.get_paper(pid)
            meta = json.loads(row["meta_json"] or "{}")
            stem = Path(card_filename(dict(meta, doi=row["doi"]), revision[:10])).stem
            if (pipe.config.knowledge_dir / "papers" / stem).exists():
                stem += "-" + uuid.uuid4().hex
            # Reserve the normal paper folder so translation can run before a main model is configured.
            pipe.registry.update_paper(pid, kb_path=f"papers/{stem}/{stem}.md")
            pipe.place_pdfs(pid)
            mobile_sync.pdf_path(pipe.config, pipe.registry, pid, revision)
            return json.dumps({"existing": False, "paperId": pid})
    finally:
        tmp.unlink(missing_ok=True)


def make_card(pipe, job):
    pid = job["paper_id"]
    with pipe._lock:
        path = mobile_sync.pdf_path(pipe.config, pipe.registry, pid, job["arg"])
        row = pipe.registry.get_paper(pid)
        if row["card_json"]:
            pipe._refresh_card(pid)
            return ""
        if row["status"] not in ("new", "error", "extracting"):
            raise ValueError(_REVIEW)
        if not pipe.config.model_ready():
            raise ValueError(_MODEL)
        result = pipe.process_paper(pid, source_pdf=path)
        if result not in ("processed", "known"):
            raise ValueError({"waiting_llm": _MODEL, "held": _HOLD}.get(result, _GENERIC))
        mobile_sync.pdf_path(pipe.config, pipe.registry, pid, job["arg"])
    return ""


class MobileWork:
    def __init__(self, pipe, revisions, stopped):
        self.pipe = pipe
        self.revisions = revisions
        self.stopped = stopped
        self.lock = threading.RLock()
        self.upload = None
        self.tasks = {}

    def _live(self):
        if self.stopped.is_set():
            raise WorkError(403, "pairing_ended")

    def _discard(self):
        if self.upload:
            self.upload[2].unlink(missing_ok=True)
            self.upload = None

    def close(self):
        with self.lock:
            self._discard()

    def begin(self, revision, size):
        if type(size) is not int or not 0 < size <= MAX_PDF:
            raise WorkError(413, "pdf_size_invalid")
        with self.lock:
            self._live()
            if self.upload and self.upload[:2] == (revision, size):
                return {"offset": self.upload[2].stat().st_size}
            self._discard()
            folder = self.pipe.config.imported_dir
            folder.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".papermap-phone-{os.getpid()}-", suffix=".part", dir=folder)
            os.close(fd)
            self.upload = revision, size, Path(name)
            return {"offset": 0}

    def chunk(self, revision, offset, data):
        with self.lock:
            self._live()
            if not self.upload or self.upload[0] != revision:
                raise WorkError(409, "begin_upload")
            _, size, path = self.upload
            current = path.stat().st_size
            if not 0 < len(data) <= CHUNK or offset > current or offset + len(data) > size:
                raise WorkError(409, "offset_mismatch")
            with path.open("r+b") as f:
                f.seek(offset)
                if offset < current:
                    if f.read(len(data)) != data:
                        raise WorkError(409, "offset_mismatch")
                else:
                    f.write(data)
            return {"offset": path.stat().st_size}

    def submit(self, body):
        key, kind, rev, pid = (body.get(k) for k in ("id", "kind", "revision", "paperId"))
        if (not isinstance(key, str) or len(key) != 36 or str(uuid.UUID(key)) != key
                or not isinstance(rev, str) or not _SHA.fullmatch(rev)
                or kind not in ("import", "card", "translation")):
            raise WorkError(400, "invalid_task")
        if kind == "import":
            if pid is not None:
                raise WorkError(400, "invalid_task")
            pid = 0
        elif type(pid) is not int or pid <= 0:
            raise WorkError(400, "invalid_task")
        signature = kind, pid, rev
        with self.lock:
            self._live()
            previous = self.tasks.get(key)
            if previous:
                if previous[0] != signature:
                    raise WorkError(409, "request_id_reused")
                return self.status(key)
            if len(self.tasks) >= _LIMIT:
                raise WorkError(429, "session_task_limit")
            for other, (sig, _, _) in list(self.tasks.items()):
                if sig == signature and self.status(other)["state"] in ("queued", "running"):
                    self.tasks[key] = self.tasks[other]
                    if kind == "import" and self.upload and self.upload[0] == rev:
                        self._discard()
                    return self.status(key)
            if kind == "import":
                if not self.upload or self.upload[0] != rev or self.upload[2].stat().st_size != self.upload[1]:
                    raise WorkError(409, "upload_incomplete")
                # Keep at most one accepted upload waiting for identification, plus one in-flight blob.
                for other, (sig, _, _) in self.tasks.items():
                    if sig[0] == "import" and self.status(other)["state"] in ("queued", "running"):
                        raise WorkError(409, "import_busy")
                jid = self.pipe.jobs.submit("mobile_import", 0, rev, options={"upload": self.upload[2].name})
                self.upload = None  # The accepted desktop job now owns this file.
                error = ""
            else:
                if self.revisions.get(pid) != rev:
                    raise WorkError(403, "not_shared")
                result = mobile_sync.read_companion(self.pipe.config, self.pipe.registry, pid, rev)
                jid, error = self._generate(kind, pid, rev, result)
            self.tasks[key] = (signature, jid, error)
            return self.status(key)

    def _generate(self, kind, pid, rev, material):
        if kind == "card":
            if material["card"]:
                return 0, ""
            if not self.pipe.config.model_ready():
                return 0, _MODEL
            return self.pipe.jobs.submit("mobile_card", pid, rev), ""
        if material["translation"]["state"] == "ready":
            return 0, ""
        try:
            svc = translate.service_by_id(self.pipe.config, None)
            if ((svc["kind"] == "bilibili" and not self.pipe.config.raw["translation"].get("privacy_ack"))
                    or (svc["kind"] == "main" and not self.pipe.config.model_ready())
                    or (svc["kind"] == "custom" and not (svc.get("base_url") and svc.get("model")))):
                return 0, _TRANSLATOR
            return self.pipe.submit_translation(pid, revision=rev), ""
        except (ValueError, translate.TranslationError):
            return 0, _TRANSLATOR

    def status(self, key):
        with self.lock:
            self._live()
            if key not in self.tasks:
                raise WorkError(404, "unknown_task")
            (kind, pid, rev), jid, message = self.tasks[key]
            state = "failed" if message else "done"
            if jid:
                job = self.pipe.registry.one("SELECT status,message FROM jobs WHERE id=?", (jid,))
                if not job:
                    state, message = "failed", _GENERIC
                elif job["status"] == "error":
                    safe = next((text for text in (_REVIEW, _MODEL, _CHANGED, _SELECTION, _HOLD, _CHECKSUM, _UNREADABLE)
                                 if job["message"] == "ValueError: " + text), _GENERIC)
                    state, message = "failed", safe
                else:
                    state = job["status"]
                    if state == "done" and kind == "import":
                        value = json.loads(job["message"])
                        candidate = value["paperId"]
                        if value["existing"] and self.revisions.get(candidate) != rev:
                            state, message = "failed", _SELECTION
                        else:
                            try:
                                mobile_sync.pdf_path(self.pipe.config, self.pipe.registry, candidate, rev)
                            except (ValueError, LookupError, OSError):
                                state, message = "failed", _CHANGED
                            else:
                                pid = candidate
                                self.revisions[pid] = rev
            return {"id": key, "kind": kind, "revision": rev, "paperId": pid, "state": state, "message": message}

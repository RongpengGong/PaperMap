"""An opt-in TLS listener serving only explicitly shared papers on a local subnet."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import mobile_sync, mobile_work

_PRIVATE = tuple(ipaddress.ip_network(s) for s in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
_PAPER_ROUTE = re.compile(r'/v1/papers/([1-9][0-9]{0,17})/([a-f0-9]{64})(?:/(pdf|changes|companion|pages/(0|[1-9][0-9]{0,8})))?')
_MAX_BODY = 256 * 1024
_UPLOAD_ROUTE = re.compile(r'/v1/uploads/([a-f0-9]{64})(?:/chunks/(0|[1-9][0-9]{0,8}))?')
_TASK_ROUTE = re.compile(r'/v1/tasks/([a-f0-9-]{36})')
_MAX_CLIENTS = 8
_LOG = logging.getLogger(__name__)


def _networks() -> dict[str, ipaddress.IPv4Network]:
    networks = {}
    active = psutil.net_if_stats()
    for name, records in psutil.net_if_addrs().items():
        if not active.get(name) or not active[name].isup:
            continue
        for item in records:
            if item.family != socket.AF_INET or not item.netmask:
                continue
            interface = ipaddress.IPv4Interface(f'{item.address}/{item.netmask}')
            # Restrict direct peers to the interface netmask AND the RFC1918 range.
            for private in _PRIVATE:
                if interface.ip in private:
                    networks[item.address] = max((private, interface.network), key=lambda n: n.prefixlen)
    return networks


def addresses() -> list[str]:
    return sorted(_networks(), key=ipaddress.IPv4Address)


def _identity(address: str) -> tuple[ssl.SSLContext, str, str]:
    """Keep the CA key in memory; remove the temporary server key after loading SSL."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PaperMap local pairing')])
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])

    def certificate(subject, key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))

    ca = (certificate(ca_name, ca_key).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
          .sign(ca_key, hashes.SHA256()))
    leaf = (certificate(server_name, leaf_key).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix='papermap-tls-') as folder:
        path = Path(folder) / 'identity.pem'
        path.write_bytes(leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                         + leaf.public_bytes(pem))
        context.load_cert_chain(str(path))
    public_key = leaf_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode('ascii')
    return context, ca.public_bytes(pem).decode('ascii'), pin


class _Listener(ThreadingHTTPServer):
    # Join accepted writes before stop() returns or the registry is closed.
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, address, network, context, info, config, registry, revisions, processor=None):
        self.network = network
        self.context = context
        self.info = info
        self.config = config
        self.registry = registry
        self.revisions = revisions
        self.slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        self.stopped = threading.Event()
        self.work = mobile_work.MobileWork(processor, revisions, self.stopped) if processor else None
        self.guard = threading.Lock()
        self.clients = set()
        self.failed = deque()
        self.requests = deque()
        self.verified = {}
        super().__init__((address, 0), _Handler)

    def verify_request(self, request, client_address):
        return not self.stopped.is_set() and ipaddress.ip_address(client_address[0]) in self.network

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(15)
            # The accept loop stays available while a client negotiates TLS.
            with self.context.wrap_socket(request, server_side=True, do_handshake_on_connect=False) as secured:
                with self.guard:
                    if self.stopped.is_set():
                        return
                    self.clients.add(secured)
                try:
                    secured.do_handshake()
                    self.finish_request(secured, client_address)
                    secured.shutdown(socket.SHUT_WR)
                finally:
                    with self.guard:
                        self.clients.discard(secured)
        except (OSError, ConnectionError):
            pass
        finally:
            request.close()
            self.slots.release()

    def admit(self, authorized: bool) -> bool:
        now = time.monotonic()
        with self.guard:
            budget, limit = (self.requests, 1200) if authorized else (self.failed, 30)
            while budget and budget[0] < now - 60:
                budget.popleft()
            if len(budget) >= limit:
                return False
            budget.append(now)
            return True

    def stop(self):
        self.stopped.set()
        with self.guard:
            for stream in self.clients:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.shutdown()
        self.server_close()
        if self.work:
            self.work.close()


class _Handler(BaseHTTPRequestHandler):
    server: _Listener
    server_version = 'PaperMap'
    sys_version = ''
    wbufsize = 64 * 1024

    def log_message(self, *_):
        pass  # Pairing and document requests never enter the desktop HTTP access log.

    def _headers(self, status, mime, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self._headers(status, 'application/json; charset=utf-8', len(encoded))
        self.wfile.write(encoded)

    def _error(self, status, message):
        self._json(status, {'error': message})

    def _allowed(self):
        tokens = self.headers.get_all('Authorization', [])
        expected = ('Bearer ' + self.server.info['token']).encode('ascii')
        valid = len(tokens) == 1 and hmac.compare_digest(tokens[0].encode('latin1'), expected)
        if not self.server.admit(valid):
            self._error(429, 'too_many_requests')
        elif self.server.stopped.is_set() or not valid:
            self._error(401, 'pairing_required')
        elif self.headers.get('Origin') or self.headers.get('Host') != self.server.info['url'][8:]:
            self._error(403, 'native_client_required')
        else:
            return True
        return False

    def _body(self, *, chunk=False):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,9}', lengths[0]):
            self._error(400, 'invalid_length')
            return None
        size = int(lengths[0])
        if size > (mobile_work.CHUNK if chunk else _MAX_BODY):
            self._error(413, 'chunk_too_large' if chunk else 'changes_too_large')
            return None
        raw = self.rfile.read(size)
        if self.headers.get_content_type() != ('application/octet-stream' if chunk else 'application/json'):
            self._error(415, 'invalid_content_type')
            return None
        if len(raw) != size:
            raise ValueError('incomplete changes')
        if chunk:
            return raw
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid changes')
        return value

    def _processing(self):
        work = self.server.work
        if work is None:
            self._error(403, 'processing_disabled')
            return
        upload = _UPLOAD_ROUTE.fullmatch(self.path)
        task = _TASK_ROUTE.fullmatch(self.path)
        if upload and self.command == 'POST':
            body = self._body(chunk=upload[2] is not None)
            if body is not None:
                result = (work.chunk(upload[1], int(upload[2]), body) if upload[2] is not None
                          else work.begin(upload[1], body.get('byteSize')))
                self._json(200, result)
        elif self.command == 'POST' and self.path == '/v1/tasks':
            body = self._body()
            if body is not None:
                self._json(200, work.submit(body))
        elif task and self.command == 'GET':
            self._json(200, work.status(task[1]))
        else:
            self._error(404, 'not_available')

    def _pdf(self, source, pid, revision):
        # Hash the open handle once per file identity, including after an in-place edit.
        # Mobile checks the digest of the assembled chunks before publishing its copy.
        with source.open('rb') as stream:
            st = os.fstat(stream.fileno())
            identity = (revision, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            with self.server.guard:
                verified = self.server.verified.get(pid) == identity
            if not verified:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    if self.server.stopped.is_set():
                        return
                    digest.update(chunk)
                if digest.hexdigest() != revision:
                    self._error(409, 'revision_changed')
                    return
                with self.server.guard:
                    self.server.verified[pid] = identity
            size = st.st_size
            start, end = 0, size - 1
            ranges = self.headers.get_all('Range', [])
            extra = {'Accept-Ranges': 'bytes'}
            if ranges:
                bounds = _byte_range(ranges[0], size) if len(ranges) == 1 else None
                if bounds is None:
                    self._headers(416, 'application/pdf', 0, {'Content-Range': f'bytes */{size}'})
                    return
                start, end = bounds
                extra['Content-Range'] = f'bytes {start}-{end}/{size}'
            remaining = end - start + 1
            stream.seek(start)
            self._headers(206 if ranges else 200, 'application/pdf', remaining, extra)
            while remaining > 0 and not self.server.stopped.is_set():
                chunk = stream.read(min(remaining, 128 * 1024))
                if not chunk:
                    return
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _route(self):
        if not self._allowed():
            return
        server = self.server
        if mobile_sync.library_id(server.registry) != server.info['libraryId']:
            self._error(409, 'library_changed')
            return
        if self.path == '/v1/papers' and self.command == 'GET':
            result = []
            for pid, revision in sorted(server.revisions.copy().items()):
                try:
                    paper = mobile_sync.capture(server.config, server.registry, pid)
                except (LookupError, ValueError, FileNotFoundError):
                    continue  # Papers removed or replaced during this session may no longer be readable.
                if paper['revision'] == revision:
                    result.append({key: paper[key] for key in
                                   ('id', 'title', 'revision', 'pageCount', 'byteSize', 'companionVersion')})
            from . import appupdate

            upd = appupdate.state(server.config)  # cached answer only: the phone never triggers a GitHub request
            self._json(200, {'v': 1, 'libraryId': server.info['libraryId'], 'papers': result,
                             'processing': server.work is not None,
                             'desktop': {'version': upd['current'], 'latestVersion': upd.get('latest') or '',
                                         'updateAvailable': upd['newer'], 'releaseUrl': upd.get('url') or ''}})
            return
        if self.path == '/v1/tasks' or self.path.startswith(('/v1/uploads/', '/v1/tasks/')):
            self._processing()
            return
        route = _PAPER_ROUTE.fullmatch(self.path)
        if not route or int(route[1]) not in server.revisions:
            self._error(404, 'not_shared')
            return
        pid, revision, resource, page = int(route[1]), route[2], route[3], route[4]
        if revision != server.revisions[pid]:
            self._error(409, 'revision_changed')
            return
        args = (server.config, server.registry, pid)
        if self.command == 'POST' and resource == 'changes':
            body = self._body()
            if body is not None:
                self._json(200, mobile_sync.apply_changes(*args, revision, body))
        elif self.command == 'GET' and resource is None:
            paper = mobile_sync.capture(*args)
            if paper['revision'] != revision:
                self._error(409, 'revision_changed')
            else:
                self._json(200, paper)
        elif self.command == 'GET' and resource == 'companion':
            self._json(200, mobile_sync.read_companion(*args, revision))
        elif self.command == 'GET' and resource == 'pdf':
            self._pdf(mobile_sync.pdf_path(*args, revision), pid, revision)
        elif self.command == 'GET' and page is not None:
            self._json(200, mobile_sync.read_page(*args, revision, int(page)))
        else:
            self._error(405, 'method_not_allowed')

    def _dispatch(self):
        try:
            self._route()
        except mobile_work.WorkError as exc:
            self._error(exc.status, exc.code)
        except mobile_sync.RevisionConflict:
            self._error(409, 'revision_changed')
        except (LookupError, FileNotFoundError):
            self._error(404, 'not_available')
        except (ValueError, RecursionError):
            self._error(400, 'invalid_request')
        except (BrokenPipeError, ConnectionError, TimeoutError, ssl.SSLError):
            pass
        except Exception:
            # The detailed exception stays local; it may reference a private file path.
            _LOG.exception('Mobile sync request failed')
            self._error(500, 'sync_failed')

    do_GET = do_POST = _dispatch


def _byte_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r'bytes=([0-9]{0,19})-([0-9]{0,19})', header)
    if not match or not size or not any(match.groups()):
        return None
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(size - 1, int(right)) if right else size - 1
    else:
        start, end = max(0, size - int(right)), size - 1
    return (start, end) if start <= end else None


class MobileLan:
    def __init__(self, config, registry, pipeline=None):
        self.config = config
        self.registry = registry
        self.pipeline = pipeline
        self._mutex = threading.Lock()
        self._listener = None
        self._thread = None

    addresses = staticmethod(addresses)

    def start(self, address: str, selected_ids, *, processing=False) -> dict:
        if not isinstance(selected_ids, (list, tuple, set)) or not (selected_ids or processing):
            raise ValueError('请选择要共享的论文。')
        if processing and not self.pipeline:
            raise ValueError('电脑处理服务尚未就绪。')
        if not all(type(pid) is int and 0 < pid < 10**18 for pid in selected_ids):
            raise ValueError('论文编号无效。')
        with self._mutex:
            if self._listener:
                raise ValueError('请先关闭当前共享，再选择新的论文。')
            network = _networks().get(address)
            if network is None:
                raise ValueError('请选择此电脑的局域网 IPv4 地址。')
            revisions = {}
            for pid in set(selected_ids):
                try:
                    revisions[pid] = mobile_sync.capture(self.config, self.registry, pid)['revision']
                except LookupError as exc:
                    raise ValueError(str(exc)) from exc
            context, ca, pin = _identity(address)
            info = {'v': 1, 'token': secrets.token_urlsafe(32), 'ca': ca, 'pin': pin,
                    'libraryId': mobile_sync.library_id(self.registry)}
            server = _Listener(address, network, context, info, self.config, self.registry, revisions,
                               self.pipeline if processing else None)
            info['url'] = f'https://{address}:{server.server_port}'
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1},
                                      daemon=True, name='papermap-mobile-lan')
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._listener, self._thread = server, thread
            return dict(info)

    def stop(self):
        with self._mutex:
            if self._listener:
                self._listener.stop()
                self._thread.join()
                self._listener, self._thread = None, None

    def status(self) -> dict:
        with self._mutex:
            if self._listener:
                return {'active': True, 'address': self._listener.server_address[0],
                        'paperIds': sorted(self._listener.revisions.copy()),
                        **({'processing': True} if self._listener.work else {})}
            return {'active': False}

    def pairing(self) -> dict:
        with self._mutex:
            if not self._listener:
                raise ValueError('请先开启局域网共享。')
            return dict(self._listener.info)

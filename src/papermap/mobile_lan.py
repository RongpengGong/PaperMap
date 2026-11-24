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
import subprocess
import sys
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

from . import llm as llm_mod, mobile_backup, mobile_inbox, mobile_match, mobile_sync, mobile_work

_PRIVATE = tuple(ipaddress.ip_network(s) for s in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
_PAPER_ROUTE = re.compile(r'/v1/papers/([1-9][0-9]{0,17})/([a-f0-9]{64})(?:/(pdf|changes|companion|match|pages/(0|[1-9][0-9]{0,8})))?')
_MAX_BODY = 256 * 1024
_UPLOAD_ROUTE = re.compile(r'/v1/uploads/([a-f0-9]{64})(?:/chunks/(0|[1-9][0-9]{0,8}))?')
_TASK_ROUTE = re.compile(r'/v1/tasks/([a-f0-9-]{36})')
_INBOX_ROUTE = re.compile(r'/v1/inbox/new/([1-9][0-9]{0,17})')
_BACKUP_ROUTE = re.compile(r'/v1/backups/([a-f0-9]{64})(?:/(chunks/(0|[1-9][0-9]{0,9})|finish|meta|pdf))?')
_MAX_CLIENTS = 8
_PAIRING = 'mobile_pairing.json'  # private: the token and keys that let a paired phone reconnect after a restart
_CA_DAYS = 5 * 365
_LEAF_DAYS = 397
_RESUME_EVERY = 30  # seconds between attempts to reopen the share after a restart
_LOG = logging.getLogger(__name__)


# VPN and other point-to-point tunnels (utun, ppp, ipsec, WireGuard...) are never reachable from a phone on
# the same Wi-Fi; a school or company VPN often uses a private 172.x address that would otherwise be picked.
_TUNNEL = re.compile(r'^(utun|ppp|ipsec|tun|tap|wg|gif|stf|awdl|llw|zt|tailscale|nordlynx|anpi|ap\d|bridge\d{3})', re.I)


def _is_tunnel(name: str, item) -> bool:
    return bool(_TUNNEL.match(name)) or bool(getattr(item, 'ptp', None)) or item.netmask == '255.255.255.255'


def _networks() -> dict[str, ipaddress.IPv4Network]:
    networks = {}
    active = psutil.net_if_stats()
    for name, records in psutil.net_if_addrs().items():
        if not active.get(name) or not active[name].isup:
            continue
        for item in records:
            if item.family != socket.AF_INET or not item.netmask or _is_tunnel(name, item):
                continue
            interface = ipaddress.IPv4Interface(f'{item.address}/{item.netmask}')
            # Restrict direct peers to the interface netmask AND the RFC1918 range.
            for private in _PRIVATE:
                if interface.ip in private:
                    networks[item.address] = max((private, interface.network), key=lambda n: n.prefixlen)
    return networks


def addresses() -> list[str]:
    """Home and office networks first (192.168.x, then 10.x, then 172.16-31.x): the first one is the default."""
    rank = {16: 0, 8: 1, 12: 2}  # prefix length of the RFC1918 block the address belongs to
    nets = _networks()

    def key(addr: str):
        block = next((p for p in _PRIVATE if ipaddress.IPv4Address(addr) in p), None)
        return (rank.get(block.prefixlen if block else 0, 3), ipaddress.IPv4Address(addr))
    return sorted(nets, key=key)


def computer_name() -> str:
    # Shown in the phone's device list, so several paired computers can be told apart.
    name = ''
    if sys.platform == 'darwin':
        try:
            name = subprocess.run(['scutil', '--get', 'ComputerName'], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ''
    if not name:
        name = os.environ.get('COMPUTERNAME') or socket.gethostname()
    name = re.sub(r'\.local$', '', name)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', name).strip()[:60] or 'PaperMap'


_SHARE_STATE = 'mobile_share.json'


def remembered(config) -> dict:
    """What was shared last time, offered again so re-pairing after an address change is one click."""
    try:
        data = json.loads((Path(config.data_dir) / _SHARE_STATE).read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    ids = [i for i in data.get('paperIds') or [] if type(i) is int and i > 0]
    return {'paperIds': ids, 'address': str(data.get('address') or ''), 'inbox': data.get('inbox') is not False,
            'processing': data.get('processing') is True, 'at': str(data.get('at') or ''),
            'active': data.get('active') is True, 'review': data.get('review') is True}


def _remember(config, address, ids, processing, inbox, active=True, review=False) -> None:
    state = {'paperIds': sorted(set(ids)), 'address': address, 'processing': bool(processing), 'inbox': bool(inbox),
             'review': bool(review),
             'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'), 'active': bool(active)}
    try:
        (Path(config.data_dir) / _SHARE_STATE).write_text(json.dumps(state), 'utf-8')
    except OSError:
        _LOG.warning('Could not remember the shared papers')


def _mark_stopped(config) -> None:
    """The user closed the share: do not reopen it when PaperMap starts again."""
    last = remembered(config)
    if last.get('active'):
        _remember(config, last['address'], last['paperIds'], last['processing'], last['inbox'], active=False,
                  review=last['review'])


def _load_pairing(config, library_id: str) -> dict:
    """The saved token, port and keys of this library's pairing, or {} when there is none to reuse."""
    try:
        data = json.loads((Path(config.data_dir) / _PAIRING).read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    if (not isinstance(data, dict) or data.get('v') != 1 or data.get('libraryId') != library_id
            or not isinstance(data.get('token'), str) or len(data['token']) < 32 or not isinstance(data.get('keys'), dict)):
        return {}
    try:
        ca = x509.load_pem_x509_certificate(str(data['keys'].get('ca', '')).encode('ascii'))
    except ValueError:
        return {}
    if ca.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=30):
        return {}  # Start a fresh pairing well before the phone's copy of the CA expires.
    port = data.get('port')
    return {'token': data['token'], 'keys': {k: str(v) for k, v in data['keys'].items()},
            'port': port if type(port) is int and 1024 <= port <= 65535 else 0}


def _save_pairing(config, library_id: str, token: str, keys: dict, port: int) -> None:
    path = Path(config.data_dir) / _PAIRING
    temp = path.with_name(path.name + '.tmp')
    payload = json.dumps({'v': 1, 'libraryId': library_id, 'token': token, 'keys': keys, 'port': port})
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(payload)
        os.replace(temp, path)
    except OSError:
        _LOG.warning('Could not save the phone pairing; the phone will need to scan again after a restart')


def _forget_port(config) -> None:
    """Keep the token and keys but let the next start pick any free port."""
    path = Path(config.data_dir) / _PAIRING
    try:
        data = json.loads(path.read_text('utf-8'))
        data['port'] = 0
        _save_pairing(config, data['libraryId'], data['token'], data['keys'], 0)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _forget_pairing(config) -> None:
    for name in (_PAIRING, _PAIRING + '.tmp'):
        try:
            (Path(config.data_dir) / name).unlink()
        except FileNotFoundError:
            pass


def _identity(address: str, keys: dict | None = None) -> tuple[ssl.SSLContext, str, str]:
    """Issue a server certificate for this address; remove the temporary key file after loading SSL.

    With ``keys`` (PEM strings 'caKey', 'ca', 'leafKey'), an earlier pairing's CA and server key are reused,
    so a phone that trusts that CA and pins that key keeps working; missing entries are created and filled in.
    """
    keys = {} if keys is None else keys
    pem = serialization.Encoding.PEM
    try:
        ca_key = serialization.load_pem_private_key(keys['caKey'].encode('ascii'), None)
        leaf_key = serialization.load_pem_private_key(keys['leafKey'].encode('ascii'), None)
        ca = x509.load_pem_x509_certificate(keys['ca'].encode('ascii'))
        if not (isinstance(ca_key, ec.EllipticCurvePrivateKey) and isinstance(leaf_key, ec.EllipticCurvePrivateKey)):
            raise ValueError('unexpected key type')
    except (KeyError, ValueError, TypeError, AttributeError):
        keys.clear()
        ca_key = ec.generate_private_key(ec.SECP256R1())
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        ca = None
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PaperMap local pairing')])
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])

    def certificate(subject, key, days):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))

    if ca is None:
        ca = (certificate(ca_name, ca_key, _CA_DAYS).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
              .sign(ca_key, hashes.SHA256()))
    # The server certificate is issued again on every start, so it always names the current address.
    leaf = (certificate(server_name, leaf_key, _LEAF_DAYS).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), False)
            .sign(ca_key, hashes.SHA256()))
    secret = serialization.PrivateFormat.PKCS8
    keys.update(caKey=ca_key.private_bytes(pem, secret, serialization.NoEncryption()).decode('ascii'),
                leafKey=leaf_key.private_bytes(pem, secret, serialization.NoEncryption()).decode('ascii'),
                ca=ca.public_bytes(pem).decode('ascii'))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix='papermap-tls-') as folder:
        path = Path(folder) / 'identity.pem'
        path.write_bytes(leaf_key.private_bytes(pem, secret, serialization.NoEncryption())
                         + leaf.public_bytes(pem))
        context.load_cert_chain(str(path))
    public_key = leaf_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode('ascii')
    return context, ca.public_bytes(pem).decode('ascii'), pin


class _Listener(ThreadingHTTPServer):
    # Join accepted writes before stop() returns or the registry is closed.
    daemon_threads = False
    # Reopening the saved port right after a restart must not wait for old connections in TIME_WAIT.
    # On Windows SO_REUSEADDR would let another program take over a port in use, so it stays off there.
    allow_reuse_address = sys.platform != 'win32'

    def __init__(self, address, network, context, info, config, registry, revisions, processor=None, inbox=False, port=0,
                 actions=None):
        self.network = network
        self.context = context
        self.info = info
        self.config = config
        self.registry = registry
        self.revisions = revisions
        self.inbox = inbox
        self.actions = actions  # (pipeline, field_tasks) when the phone may settle review items and take papers in
        self.pipeline = processor
        self.slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        self.stopped = threading.Event()
        self.work = mobile_work.MobileWork(processor, revisions, self.stopped) if processor else None
        self.guard = threading.Lock()
        self.clients = set()
        self.failed = deque()
        self.requests = deque()
        self.verified = {}
        self.last_seen = 0.0
        super().__init__((address, port), _Handler)

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
            self.server.last_seen = time.time()  # shown in the desktop sidebar
            return True
        return False

    def _body(self, *, chunk=False, limit=None):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,9}', lengths[0]):
            self._error(400, 'invalid_length')
            return None
        size = int(lengths[0])
        if size > (mobile_work.CHUNK if chunk else limit or _MAX_BODY):
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

    def _inbox(self):
        if not self.server.inbox:
            self._error(403, 'inbox_disabled')
            return
        decision = _INBOX_ROUTE.fullmatch(self.path)
        actions = self.server.actions
        if self.path == '/v1/inbox' and self.command == 'GET':
            self._json(200, mobile_inbox.overview(self.server.registry, actions is not None))
        elif decision and self.command == 'POST':
            body = self._body()
            if body is not None:
                self._json(200, mobile_inbox.decide(self.server.registry, int(decision[1]), body.get('action'),
                                                    actions[0] if actions else None))
        elif self.path == '/v1/review' and self.command == 'POST':
            if actions is None:
                self._error(403, 'actions_disabled')
                return
            body = self._body()
            if body is not None:
                self._json(200, mobile_inbox.act(actions[0], actions[1], body))
        else:
            self._error(404, 'not_available')

    def _backups(self):
        config = self.server.config
        if self.path == '/v1/backups' and self.command == 'GET':
            self._json(200, {'v': 1, 'backups': mobile_backup.listing(config), **mobile_backup.summary(config)})
            return
        route = _BACKUP_ROUTE.fullmatch(self.path)
        if not route:
            self._error(404, 'not_available')
            return
        revision, action, offset = route[1], route[2], route[3]
        if self.command == 'POST' and action is None:
            body = self._body()
            if body is not None:
                self._json(200, mobile_backup.begin(config, revision, body.get('byteSize')))
        elif self.command == 'POST' and offset is not None:
            body = self._body(chunk=True)
            if body is not None:
                self._json(200, mobile_backup.chunk(config, revision, int(offset), body, 0))
        elif self.command == 'POST' and action == 'finish':
            body = self._body(limit=mobile_backup.MAX_META)
            if body is not None:
                self._json(200, mobile_backup.finish(config, revision, body))
        elif self.command == 'GET' and action == 'meta':
            self._json(200, mobile_backup.meta(config, revision))
        elif self.command == 'GET' and action == 'pdf':
            self._pdf(mobile_backup.pdf_file(config, revision), 'backup:' + revision, revision)
        else:
            self._error(405, 'method_not_allowed')

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
                             'processing': server.work is not None, 'inbox': server.inbox,
                             'reviewActions': server.actions is not None, 'backups': True,
                             'desktop': {'version': upd['current'], 'latestVersion': upd.get('latest') or '',
                                         'updateAvailable': upd['newer'], 'releaseUrl': upd.get('url') or ''}})
            return
        if self.path in ('/v1/inbox', '/v1/review') or self.path.startswith('/v1/inbox/'):
            self._inbox()
            return
        if self.path == '/v1/backups' or self.path.startswith('/v1/backups/'):
            self._backups()
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
            paper = mobile_sync.capture(*args, marks=True)
            if paper['revision'] != revision:
                self._error(409, 'revision_changed')
            else:
                self._json(200, paper)
        elif self.command == 'POST' and resource == 'match':
            body = self._body()
            if body is not None:
                # The model is only used when this pairing allowed the phone to use the computer's services.
                llm = server.pipeline.llm if body.get('model') is True and server.pipeline is not None else None
                if body.get('model') is True and llm is None:
                    self._error(403, 'processing_disabled')
                    return
                self._json(200, mobile_match.suggest(*args, revision, body.get('quote'), body.get('page'), llm))
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
        except (mobile_work.WorkError, mobile_backup.BackupError) as exc:
            self._error(exc.status, exc.code)
        except mobile_sync.RevisionConflict:
            self._error(409, 'revision_changed')
        except (llm_mod.BudgetExceeded, llm_mod.LLMError, llm_mod.LLMHTTPError):
            self._error(502, 'model_failed')
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
        self._resuming = threading.Event()  # set to cancel a pending resume (user action or shutdown)
        self._resumer = None
        self.moved_port = False  # the saved port was taken, so the phone has to scan again
        self.field_tasks = None  # set by the web app: establishing a field runs through its task queue

    addresses = staticmethod(addresses)

    def remembered(self) -> dict:
        return remembered(self.config)

    def start(self, address: str, selected_ids, *, processing=False, inbox=False, review=False, _resume=False) -> dict:
        if not isinstance(selected_ids, (list, tuple, set)) or not (selected_ids or processing or inbox):
            raise ValueError('请选择要共享的论文。')
        if (processing or review) and not self.pipeline:
            raise ValueError('电脑处理服务尚未就绪。')
        review = bool(review and inbox)  # acting on the lists needs the lists
        if not all(type(pid) is int and 0 < pid < 10**18 for pid in selected_ids):
            raise ValueError('论文编号无效。')
        if not _resume:
            self._cancel_resume()
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
                    if _resume:
                        continue  # A paper deleted since the last share no longer reopens with it.
                    raise ValueError(str(exc)) from exc
            library = mobile_sync.library_id(self.registry)
            saved = _load_pairing(self.config, library)
            keys = dict(saved.get('keys', {}))
            context, ca, pin = _identity(address, keys)
            # The token only carries over when the phone's trusted CA and pinned key did too.
            token = saved.get('token') if saved and saved['keys'].get('ca') == keys['ca'] else None
            info = {'v': 1, 'token': token or secrets.token_urlsafe(32), 'ca': ca, 'pin': pin,
                    'libraryId': library, 'name': computer_name()}
            args = (address, network, context, info, self.config, self.registry, revisions,
                    self.pipeline if processing else None, bool(inbox))
            extra = {'actions': (self.pipeline, self.field_tasks)} if review else {}
            port = saved.get('port', 0) if token else 0
            try:
                server = _Listener(*args, port=port, **extra)
            except OSError:
                if not port or _resume:
                    raise  # A resume tries the saved port again later before giving it up.
                server = _Listener(*args, **extra)
            self.moved_port = bool(port) and server.server_port != port
            info['url'] = f'https://{address}:{server.server_port}'
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1},
                                      daemon=True, name='papermap-mobile-lan')
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._listener, self._thread = server, thread
            _save_pairing(self.config, library, info['token'], keys, server.server_port)
            _remember(self.config, address, selected_ids if not _resume else sorted(revisions), processing, inbox,
                      review=review)
            return dict(info)

    def resume(self) -> None:
        """Reopen the share that was on when PaperMap last quit, with the same pairing, once its address is back."""
        last = self.remembered()
        if not last.get('active') or self._resumer:
            return
        self._resuming.clear()

        def attempt():
            tries = 0
            while not self._resuming.is_set():
                if last['address'] in _networks():
                    try:
                        ready = bool(self.pipeline)
                        self.start(last['address'], last['paperIds'], processing=last['processing'] and ready,
                                   inbox=last['inbox'], review=last['review'] and ready, _resume=True)
                        if tries >= 4:
                            self.moved_port = True
                        return
                    except ValueError:
                        return  # Shared already, or nothing left to share.
                    except OSError:
                        tries += 1
                        if tries >= 4:  # The saved port stays taken: open a new one, the phone scans again.
                            _forget_port(self.config)
                self._resuming.wait(_RESUME_EVERY)

        self._resumer = threading.Thread(target=attempt, daemon=True, name='papermap-mobile-resume')
        self._resumer.start()

    def _cancel_resume(self):
        self._resuming.set()
        if self._resumer and self._resumer is not threading.current_thread():
            self._resumer.join(5)
        self._resumer = None

    def _close_listener(self):
        with self._mutex:
            if self._listener:
                self._listener.stop()
                self._thread.join()
                self._listener, self._thread = None, None

    def stop(self):
        """The user closes the share. The pairing is kept, so sharing again needs no new scan."""
        self._cancel_resume()
        self._close_listener()
        _mark_stopped(self.config)

    def close(self):
        """PaperMap quits: the share reopens on the next start."""
        self._cancel_resume()
        self._close_listener()

    def revoke(self):
        """Discard the pairing: phones paired before must scan a new code."""
        self.stop()
        _forget_pairing(self.config)
        self.moved_port = False

    def paired(self) -> bool:
        return bool(_load_pairing(self.config, mobile_sync.library_id(self.registry)))

    def status(self) -> dict:
        with self._mutex:
            if self._listener:
                return {'active': True, 'address': self._listener.server_address[0],
                        'paperIds': sorted(self._listener.revisions.copy()),
                        **({'processing': True} if self._listener.work else {}),
                        **({'inbox': True} if self._listener.inbox else {}),
                        **({'review': True} if self._listener.actions else {}),
                        **({'movedPort': True} if self.moved_port else {})}
            return {'active': False}

    def sidebar(self) -> dict:
        """Connection state for the sidebar entry; cheap enough to compute on every page."""
        with self._mutex:
            listener = self._listener
            if listener:
                seen = listener.last_seen
                return {'active': True, 'papers': len(listener.revisions),
                        'minutesAgo': int((time.time() - seen) // 60) if seen else None}
        return {'active': False, 'resuming': bool(self._resumer and not self._resuming.is_set())}

    def pairing(self) -> dict:
        with self._mutex:
            if not self._listener:
                raise ValueError('请先开启局域网共享。')
            return dict(self._listener.info)

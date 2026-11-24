"""Real TLS and scoped mobile API, confined to a mocked loopback interface."""
import base64
import hashlib
import ipaddress
import socket
import ssl
import uuid
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from papermap import mobile_lan


@pytest.fixture
def lan(config, registry, pdfs, monkeypatch):
    for key in ('paper', 'refs_only'):
        pdf = pdfs[key]
        doc_id = registry.create_paper(title=key, status='done')
        registry.upsert_file(hashlib.sha256(pdf.read_bytes()).hexdigest(), str(pdf),
                             pdf.stat().st_size, pdf.stat().st_mtime, doc_id)
    monkeypatch.setattr(mobile_lan, '_networks', lambda: {'127.0.0.1': ipaddress.IPv4Network('127.0.0.0/8')})
    sharing = mobile_lan.MobileLan(config, registry)
    try:
        yield sharing
    finally:
        sharing.stop()


def trusted(pair):
    return ssl.create_default_context(cadata=pair['ca'])


def client(pair):
    return httpx.Client(base_url=pair['url'], verify=trusted(pair), trust_env=False, timeout=3,
                        headers={'Authorization': 'Bearer ' + pair['token']})


def test_lan_addresses_bind_only_private_active_interfaces(monkeypatch):
    def record(ip, netmask):
        return SimpleNamespace(address=ip, netmask=netmask, family=socket.AF_INET)
    monkeypatch.setattr(mobile_lan.psutil, 'net_if_addrs', lambda: {
        'up': [record('192.168.88.2', '255.255.255.0'), record('0.0.0.0', '0.0.0.0'),
               record('203.0.113.7', '255.255.255.0'), record('127.0.0.1', '255.0.0.0')],
        'down': [record('10.1.1.1', '255.0.0.0')]})
    monkeypatch.setattr(mobile_lan.psutil, 'net_if_stats', lambda: {
        'up': SimpleNamespace(isup=True), 'down': SimpleNamespace(isup=False)})
    assert mobile_lan.addresses() == ['192.168.88.2']
    assert mobile_lan._networks()['192.168.88.2'] == ipaddress.ip_network('192.168.88.0/24')


def test_only_shared_resources_cross_the_tls_session(lan, pdfs):
    assert lan.status() == {'active': False}
    with pytest.raises(ValueError):
        lan.pairing()
    with pytest.raises(ValueError):
        lan.start('0.0.0.0', [1])
    with pytest.raises(ValueError):
        lan.start('127.0.0.1', [])
    with pytest.raises(ValueError):
        lan.start('127.0.0.1', [123456])
    pair = lan.start('127.0.0.1', [1])
    assert pair == lan.pairing() and lan.status() == {'active': True, 'address': '127.0.0.1', 'paperIds': [1]}
    port = int(pair['url'].rsplit(':', 1)[1])
    with socket.create_connection(('127.0.0.1', port), timeout=3) as tcp:
        with trusted(pair).wrap_socket(tcp, server_hostname='127.0.0.1') as tls:
            leaf = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
            spki = leaf.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
            assert base64.b64encode(hashlib.sha256(spki).digest()).decode() == pair['pin']
    with httpx.Client(trust_env=False, timeout=3) as unpaired, pytest.raises(httpx.ConnectError):
        unpaired.get(pair['url']+'/v1/papers')
    # An idle handshake must not block other phones or shutdown.
    with socket.create_connection(('127.0.0.1', port), timeout=3), client(pair) as web:
        assert web.get('/v1/papers', headers={'Authorization': 'Bearer incorrect'}).status_code == 401
        assert web.get('/v1/papers', headers={'Origin': 'https://elsewhere.test'}).status_code == 403
        assert web.get('/v1/papers', headers={'Host': 'elsewhere.test'}).status_code == 403
        listing = web.get('/v1/papers').json()
        assert listing['libraryId'] == pair['libraryId'] and listing['v'] == 1
        assert [p['id'] for p in listing['papers']] == [1]
        assert listing['desktop']['version'] and 'updateAvailable' in listing['desktop']  # shown on the phone
        revision = listing['papers'][0]['revision']
        base = '/v1/papers/1/' + revision
        other = '/v1/papers/2/' + hashlib.sha256(pdfs['refs_only'].read_bytes()).hexdigest()
        for path in ('/settings', '/mcp', '/api/state', '/docs', '/v1/papers/', other, other + '/pdf',
                     other + '/companion'):
            assert web.get(path).status_code == 404
        assert web.get('/v1/papers/1/' + '0' * 64).status_code == 409
        paper = web.get(base).json()
        assert paper['revision'] == revision
        companion = web.get(base + '/companion').json()
        assert companion == {'id': 1, 'revision': revision, 'card': '',
                             'version': listing['papers'][0]['companionVersion'],
                             'translation': {'state': 'missing', 'paragraphs': []}}
        assert paper['companionVersion'] == companion['version']
        pdf = pdfs['paper'].read_bytes()
        assert web.get(base+'/pdf').content == pdf
        for header, start, end in [('bytes=0-31', 0, 31), ('bytes=32-', 32, len(pdf)-1),
                                   ('bytes=-8', len(pdf)-8, len(pdf)-1)]:
            chunk = web.get(base+'/pdf', headers={'Range': header})
            assert chunk.status_code == 206 and chunk.content == pdf[start:end+1]
            assert chunk.headers['Content-Range'] == f'bytes {start}-{end}/{len(pdf)}'
        for header in ('bytes=-0', 'bytes=99-22', 'bytes=0-3,5-6', f'bytes={len(pdf)}-'):
            resp = web.get(base+'/pdf', headers={'Range': header})
            assert resp.status_code == 416 and resp.headers['Content-Range'] == f'bytes */{len(pdf)}'
        segments = web.get(base+'/pages/1').json()['segments']
        note = {'id': str(uuid.uuid4()), 'page': 1, 'segmentId': segments[0]['id'], 'note': 'Phone'}
        changes = {'progress': {'baseVersion': paper['progress']['version'], 'page': 1, 'offset': 0}, 'notes': [note]}
        result = web.post(base+'/changes', json=changes)
        assert result.status_code == 200 and result.json()['acknowledgedNoteIds'] == [note['id']]
        assert web.get(base).json()['notes'][0]['quote'] == segments[0]['text']
        assert web.post(base+'/changes', json=changes).json() == result.json()
        assert web.post(base+'/changes', content=b'{}', headers={'Content-Type':'text/plain'}).status_code == 415
        assert web.post(base+'/changes', json=['invalid']).status_code == 400
        assert web.post(base+'/changes', json={'x':'a'*mobile_lan._MAX_BODY}).status_code == 413
        with pdfs['paper'].open('ab') as doc:
            doc.write(b'\n% revised\n')
        next_revision = hashlib.sha256(pdfs['paper'].read_bytes()).hexdigest()
        assert web.get('/v1/papers').json()['papers'] == []
        assert web.get('/v1/papers/1/'+next_revision+'/pdf').status_code == 409
        assert web.get(base+'/pdf').status_code == 409
        assert web.get(base+'/companion').status_code == 409
        lan.stop()
        assert lan.status() == {'active': False}
        with pytest.raises(httpx.ConnectError):
            web.get('/v1/papers')
    replacement = lan.start('127.0.0.1', [2])
    assert replacement['libraryId'] == pair['libraryId']
    assert replacement['token'] != pair['token'] and replacement['pin'] != pair['pin']
    with client(replacement) as web:
        assert web.get('/v1/papers', headers={'Authorization': 'Bearer ' + pair['token']}).status_code == 401
        assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [2]
    lan.stop()
    lan.stop()

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


def test_vpn_and_tunnels_are_not_offered_and_wifi_comes_first(monkeypatch):
    import socket
    from types import SimpleNamespace as NS

    from papermap import mobile_lan

    addr = lambda a, m, ptp=None: NS(family=socket.AF_INET, address=a, netmask=m, ptp=ptp)  # noqa: E731
    monkeypatch.setattr(mobile_lan.psutil, "net_if_addrs", lambda: {
        "utun4": [addr("172.21.41.75", "255.255.0.0", "172.21.41.75")],   # school VPN, point-to-point
        "ppp0": [addr("10.8.0.2", "255.255.255.255")],
        "en5": [addr("172.20.10.3", "255.255.255.240")],                  # phone hotspot
        "en0": [addr("192.168.178.30", "255.255.255.0")],                 # home Wi-Fi
        "lo0": [addr("127.0.0.1", "255.0.0.0")],
    })
    monkeypatch.setattr(mobile_lan.psutil, "net_if_stats", lambda: {n: NS(isup=True) for n in ("utun4", "ppp0", "en5", "en0", "lo0")})
    assert mobile_lan.addresses() == ["192.168.178.30", "172.20.10.3"]


def test_inbox_is_opt_in_and_files_suggestions_away(lan, registry):
    from papermap import tracking

    tracking.ensure_schema(registry)
    registry.execute(
        "INSERT INTO track_candidates (doi, title, title_key, journal, authors_json, score, status, created_at, updated_at) "
        "VALUES ('10.1038/s41586-026-00001-1', 'A spatial atlas of the liver', 'a spatial atlas of the liver', "
        "'Nature', '[\"Ada Lovelace\"]', 7.5, 'new', '2026-11-24', '2026-11-24')")
    registry.execute("UPDATE papers SET status='doi_unverified' WHERE id=2")

    pair = lan.start('127.0.0.1', [1])
    with client(pair) as web:
        assert web.get('/v1/papers').json()['inbox'] is False
        assert web.get('/v1/inbox').status_code == 403
    lan.stop()

    pair = lan.start('127.0.0.1', [], inbox=True)
    assert lan.status()['inbox'] is True
    with client(pair) as web:
        assert web.get('/v1/papers').json()['inbox'] is True
        box = web.get('/v1/inbox').json()
        [new] = box['newPapers']
        assert new['title'] == 'A spatial atlas of the liver' and new['url'] == 'https://doi.org/10.1038/s41586-026-00001-1'
        assert new['authors'] == ['Ada Lovelace']
        assert [(r['kind'], r['title']) for r in box['review']] == [('doi', 'refs_only')]
        # Only the titles of unshared papers are visible, never their files.
        assert web.get('/v1/papers').json()['papers'] == []
        bad = web.post(f"/v1/inbox/new/{new['id']}", json={'action': 'take_in'})
        assert bad.status_code == 400
        assert web.post(f"/v1/inbox/new/{new['id']}", json={'action': 'dismissed'}).json() == {'ok': True}
        assert web.get('/v1/inbox').json()['newPapers'] == []


def test_pairing_names_the_computer_and_the_share_is_remembered(lan, config, monkeypatch):
    monkeypatch.setattr(mobile_lan, 'computer_name', lambda: 'Lab Mac')
    assert lan.remembered() == {}
    pair = lan.start('127.0.0.1', [2, 1], inbox=False)
    assert pair['name'] == 'Lab Mac'
    lan.stop()
    last = lan.remembered()
    assert last['paperIds'] == [1, 2] and last['address'] == '127.0.0.1'
    assert last['inbox'] is False and last['processing'] is False and last['at']
    (config.data_dir / 'mobile_share.json').write_text('[1]', 'utf-8')
    assert lan.remembered() == {}


def test_computer_name_has_no_control_characters(monkeypatch):
    monkeypatch.setattr(mobile_lan.sys, 'platform', 'linux')
    monkeypatch.setattr(mobile_lan.socket, 'gethostname', lambda: 'ws-12.local\n')
    monkeypatch.delenv('COMPUTERNAME', raising=False)
    assert mobile_lan.computer_name() == 'ws-12'


def test_phone_excerpts_are_relocated_by_text_and_the_model_only_on_request(lan, registry):
    pair = lan.start('127.0.0.1', [1])
    with client(pair) as web:
        [paper] = web.get('/v1/papers').json()['papers']
        base = f"/v1/papers/{paper['id']}/{paper['revision']}"
        seg = web.get(base + '/pages/1').json()['segments'][0]
        # A changed line break and hyphenation still find the same paragraph without any model.
        quote = seg['text'][:120].replace(' ', '\n', 2)
        hit = web.post(base + '/match', json={'quote': quote, 'page': 0}).json()
        assert hit['found'] and hit['by'] == 'text' and hit['segmentId'] == seg['id'] and hit['page'] == 1
        miss = web.post(base + '/match', json={'quote': 'Zebrafish fins regenerate within days.', 'page': 1}).json()
        assert miss['found'] is False
        # The model needs the "use the computer's services" permission of this pairing.
        assert web.post(base + '/match', json={'quote': 'x y z', 'page': 0, 'model': True}).status_code == 403
        assert web.post(base + '/match', json={'quote': '', 'page': 0}).status_code == 400


def test_the_model_chooses_among_the_closest_paragraphs(config, registry, pdfs):
    from papermap import mobile_match

    pdf = pdfs['paper']
    pid = registry.create_paper(title='paper', status='done')
    registry.upsert_file(hashlib.sha256(pdf.read_bytes()).hexdigest(), str(pdf), pdf.stat().st_size, pdf.stat().st_mtime, pid)
    revision = hashlib.sha256(pdf.read_bytes()).hexdigest()

    class Model:
        asked = []

        def chat_json(self, system, user, validate, purpose, paper_id=None, max_tokens=0):
            self.asked.append(user)
            assert validate({'choice': 0}) == [] and validate({'choice': 99})
            return SimpleNamespace(data={'choice': 0, 'reason': '意思相同'})

    model = Model()
    out = mobile_match.suggest(config, registry, pid, revision, 'The cohort was followed for longer than planned.', 0, model)
    assert out['found'] and out['by'] == 'model' and out['reason'] == '意思相同' and len(model.asked) == 1

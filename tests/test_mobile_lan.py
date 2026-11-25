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


def test_the_library_crosses_the_tls_session_except_deleted_papers(lan, pdfs, registry):
    assert lan.status() == {'active': False}
    with pytest.raises(ValueError):
        lan.pairing()
    with pytest.raises(ValueError):
        lan.start('0.0.0.0')
    pair = lan.start('127.0.0.1')
    assert pair == lan.pairing() and lan.status() == {'active': True, 'address': '127.0.0.1', 'papers': 2}
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
        assert [p['id'] for p in listing['papers']] == [1, 2]  # the whole library; the phone picks
        assert listing['desktop']['version'] and 'updateAvailable' in listing['desktop']  # shown on the phone
        revision = listing['papers'][0]['revision']
        base = '/v1/papers/1/' + revision
        other = '/v1/papers/2/' + [p for p in listing['papers'] if p['id'] == 2][0]['revision']
        assert web.get(other).status_code == 200
        registry.execute("UPDATE papers SET status='ignored' WHERE id=2")  # deleted on the computer
        assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [1]
        for path in ('/settings', '/mcp', '/api/state', '/docs', '/v1/papers/', other, other + '/pdf',
                     other + '/companion'):
            assert web.get(path).status_code == 404
        assert web.get('/v1/papers/1/' + '0' * 64).status_code == 409
        paper = web.get(base).json()
        assert paper['revision'] == revision
        companion = web.get(base + '/companion').json()
        assert companion == {'id': 1, 'revision': revision, 'card': '', 'notes': '',
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
        fresh = web.get('/v1/papers').json()['papers']  # the new version is offered by itself
        assert [p['id'] for p in fresh] == [1] and fresh[0]['revision'] == next_revision != revision
        assert web.get('/v1/papers/1/'+next_revision+'/pdf').status_code == 200
        assert web.get(base+'/pdf').status_code == 409
        assert web.get(base+'/companion').status_code == 409
        lan.stop()
        assert lan.status() == {'active': False}
        with pytest.raises(httpx.ConnectError):
            web.get('/v1/papers')
    # Sharing again keeps the pairing: the phone's saved address, CA, pin and token still work.
    again = lan.start('127.0.0.1')
    assert (again['url'], again['token'], again['pin'], again['libraryId']) == (
        pair['url'], pair['token'], pair['pin'], pair['libraryId'])
    with client(pair) as web:
        assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [1]
    lan.revoke()
    replacement = lan.start('127.0.0.1')
    assert replacement['libraryId'] == pair['libraryId']
    assert replacement['token'] != pair['token'] and replacement['pin'] != pair['pin']
    with client(replacement) as web:
        assert web.get('/v1/papers', headers={'Authorization': 'Bearer ' + pair['token']}).status_code == 401
        assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [1]
    lan.stop()
    lan.stop()


def test_a_share_on_at_quit_reopens_with_the_same_pairing(lan, config, registry, monkeypatch):
    monkeypatch.setattr(mobile_lan, '_RESUME_EVERY', 0.05)
    pair = lan.start('127.0.0.1', inbox=True)
    saved = config.data_dir / 'mobile_pairing.json'
    assert saved.stat().st_mode & 0o077 == 0  # the token and keys are readable by this user only
    lan.close()  # PaperMap quits
    assert lan.remembered()['active'] is True
    registry.execute("UPDATE papers SET status='ignored' WHERE id=2")  # gone since; resumes without it
    restarted = mobile_lan.MobileLan(config, registry)
    try:
        restarted.resume()
        restarted._resumer.join(5)
        assert restarted.status() == {'active': True, 'address': '127.0.0.1', 'papers': 1, 'inbox': True}
        with client(pair) as web:  # the phone's stored pairing, untouched
            assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [1]
        restarted.stop()  # the user closes it: no reopening on the next start
        assert restarted.remembered()['active'] is False
    finally:
        restarted.close()
    later = mobile_lan.MobileLan(config, registry)
    later.resume()
    assert later._resumer is None and later.status() == {'active': False}


def test_resume_waits_for_the_address_and_a_taken_port_is_given_up(lan, config, registry, monkeypatch):
    monkeypatch.setattr(mobile_lan, '_RESUME_EVERY', 0.05)
    pair = lan.start('127.0.0.1')
    port = int(pair['url'].rsplit(':', 1)[1])
    lan.close()
    nets = {}
    monkeypatch.setattr(mobile_lan, '_networks', lambda: dict(nets))
    blocker = socket.socket()
    blocker.bind(('127.0.0.1', port))
    blocker.listen()
    restarted = mobile_lan.MobileLan(config, registry)
    try:
        restarted.resume()
        restarted._resumer.join(0.2)
        assert restarted.status() == {'active': False}  # still on another network
        nets['127.0.0.1'] = ipaddress.IPv4Network('127.0.0.0/8')
        restarted._resumer.join(5)
        status = restarted.status()
        assert status['active'] and status['movedPort']
        moved = restarted.pairing()
        assert moved['url'] != pair['url'] and (moved['token'], moved['pin']) == (pair['token'], pair['pin'])
    finally:
        blocker.close()
        restarted.close()


def test_a_damaged_or_foreign_pairing_file_starts_a_new_pairing(lan, config):
    first = lan.start('127.0.0.1')
    lan.stop()
    saved = config.data_dir / 'mobile_pairing.json'
    data = __import__('json').loads(saved.read_text('utf-8'))
    saved.write_text(__import__('json').dumps({**data, 'libraryId': str(uuid.uuid4())}), 'utf-8')
    second = lan.start('127.0.0.1')
    assert second['token'] != first['token'] and second['pin'] != first['pin']
    lan.stop()
    saved.write_text('{not json', 'utf-8')
    third = lan.start('127.0.0.1')
    assert third['token'] not in (first['token'], second['token'])


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

    pair = lan.start('127.0.0.1')
    with client(pair) as web:
        assert web.get('/v1/papers').json()['inbox'] is False
        assert web.get('/v1/inbox').status_code == 403
    lan.stop()

    pair = lan.start('127.0.0.1', inbox=True)
    assert lan.status()['inbox'] is True
    with client(pair) as web:
        assert web.get('/v1/papers').json()['inbox'] is True
        box = web.get('/v1/inbox').json()
        [new] = box['newPapers']
        assert new['title'] == 'A spatial atlas of the liver' and new['url'] == 'https://doi.org/10.1038/s41586-026-00001-1'
        assert new['authors'] == ['Ada Lovelace']
        assert [(r['kind'], r['title']) for r in box['review']] == [('doi', 'refs_only')]
        # A paper waiting for DOI review is listed for review only, never offered for download.
        assert [p['id'] for p in web.get('/v1/papers').json()['papers']] == [1]
        bad = web.post(f"/v1/inbox/new/{new['id']}", json={'action': 'take_in'})
        assert bad.status_code == 400
        assert web.post(f"/v1/inbox/new/{new['id']}", json={'action': 'dismissed'}).json() == {'ok': True}
        assert web.get('/v1/inbox').json()['newPapers'] == []


def test_pairing_names_the_computer_and_the_share_is_remembered(lan, config, monkeypatch):
    monkeypatch.setattr(mobile_lan, 'computer_name', lambda: 'Lab Mac')
    assert lan.remembered() == {}
    pair = lan.start('127.0.0.1', inbox=False)
    assert pair['name'] == 'Lab Mac'
    lan.stop()
    last = lan.remembered()
    assert 'paperIds' not in last and last['address'] == '127.0.0.1'
    assert last['inbox'] is False and last['processing'] is False and last['at']
    (config.data_dir / 'mobile_share.json').write_text('[1]', 'utf-8')
    assert lan.remembered() == {}


def test_computer_name_has_no_control_characters(monkeypatch):
    monkeypatch.setattr(mobile_lan.sys, 'platform', 'linux')
    monkeypatch.setattr(mobile_lan.socket, 'gethostname', lambda: 'ws-12.local\n')
    monkeypatch.delenv('COMPUTERNAME', raising=False)
    assert mobile_lan.computer_name() == 'ws-12'


def test_phone_excerpts_are_relocated_by_text_and_the_model_only_on_request(lan, registry):
    pair = lan.start('127.0.0.1')
    with client(pair) as web:
        paper = web.get('/v1/papers').json()['papers'][0]
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


def test_sidebar_shows_whether_the_phone_has_been_in_touch(lan):
    assert lan.sidebar() == {'active': False, 'resuming': False}
    pair = lan.start('127.0.0.1')
    assert lan.sidebar() == {'active': True, 'papers': 2, 'minutesAgo': None}
    with client(pair) as web:
        web.get('/v1/papers')
    assert lan.sidebar()['minutesAgo'] == 0


def test_phone_settles_review_items_only_when_the_share_allows_it(config, registry, pdfs, monkeypatch):
    from papermap import tracking
    from papermap.pipeline import Pipeline

    from .conftest import FakeMetadata

    monkeypatch.setattr(mobile_lan, '_networks', lambda: {'127.0.0.1': ipaddress.IPv4Network('127.0.0.0/8')})
    pipe = Pipeline(config, registry, metadata=FakeMetadata())
    pipe.taxonomy()
    slugs = [f['slug'] for f in registry.active_fields()]
    labelled = registry.create_paper(title='Needs a label', status='done')
    registry.set_paper_field(labelled, registry.field_by_slug(slugs[0])['id'], status='suggested', confidence=0.4)
    registry.update_paper(labelled, needs_review=1)
    unsure = registry.create_paper(title='Which DOI', status='doi_unverified')
    rejected = registry.create_pending('odd-field', 'method', '怪学科', 'Odd field', 'x', [])
    merged = registry.create_pending('near-field', 'method', '近学科', 'Near field', 'y', [])
    tracking.ensure_schema(registry)
    cid = registry.execute(
        "INSERT INTO track_candidates (doi, title, title_key, journal, authors_json, score, status, created_at, updated_at) "
        "VALUES ('10.1/x', 'Open paper', 'open paper', 'J', '[]', 9, 'new', '2026-11-24', '2026-11-24')").lastrowid
    taken = []
    monkeypatch.setattr(tracking, 'take_in', lambda p, c: taken.append(c) or {'ok': True, 'downloaded': True})

    lan = mobile_lan.MobileLan(config, registry, pipe)
    try:
        pair = lan.start('127.0.0.1', inbox=True)
        with client(pair) as web:
            box = web.get('/v1/inbox').json()
            assert box['actions'] is False and 'fields' not in box
            assert box['totals'] == {'newPapers': 1, 'review': 4}
            assert web.post('/v1/review', json={'kind': 'field', 'id': rejected, 'action': 'reject'}).status_code == 403
            assert web.post(f'/v1/inbox/new/{cid}', json={'action': 'take'}).status_code == 400
        lan.stop()

        pair = lan.start('127.0.0.1', inbox=True, review=True)
        assert lan.status()['review'] is True and lan.remembered()['review'] is True
        with client(pair) as web:
            assert web.get('/v1/papers').json()['reviewActions'] is True
            box = web.get('/v1/inbox').json()
            assert box['actions'] is True and slugs[0] in {f['slug'] for f in box['fields']}
            items = {(i['kind'], i['id']): i for i in box['review']}
            assert items[('doi', unsure)]['actions'] == []
            assert items[('label', labelled)]['actions'] == ['accept', 'keep']
            assert items[('label', labelled)]['labels'][0]['suggested'] is True

            def act(**body):
                return web.post('/v1/review', json=body)

            assert act(kind='doi', id=unsure, action='use').status_code == 404  # still decided on the computer
            assert act(kind='label', id=labelled, action='accept').json() == {'ok': True, 'state': 'done'}
            assert registry.get_paper(labelled)['needs_review'] == 0
            assert [r['status'] for r in registry.paper_field_rows(labelled)] == ['accepted']
            assert act(kind='label', id=labelled, action='accept').status_code == 404  # settled already
            assert act(kind='field', id=rejected, action='reject').json()['ok'] is True
            assert registry.pending_by_id(rejected)['status'] == 'rejected'
            assert act(kind='field', id=merged, action='merge', target='no-such-field').status_code == 400
            assert act(kind='field', id=merged, action='merge', target=slugs[0]).json()['ok'] is True
            assert registry.pending_by_id(merged)['status'] == 'merged'
            assert web.post(f'/v1/inbox/new/{cid}', json={'action': 'take'}).json() == {'ok': True, 'state': 'importing'}
            assert taken == [cid]
    finally:
        lan.close()


def test_phone_backups_are_kept_beside_the_library_and_restored(lan, config, registry, monkeypatch):
    import json as _json
    from papermap import mobile_backup

    pdf = b'%PDF-1.7\n' + b'x' * (mobile_backup.CHUNK + 300) + b'\n%%EOF'
    rev = hashlib.sha256(pdf).hexdigest()
    pair = lan.start('127.0.0.1', inbox=True)
    papers_before = registry.one("SELECT count(*) AS n FROM papers")["n"]
    with client(pair) as web:
        assert web.get('/v1/papers').json()['backups'] is True
        assert web.get('/v1/backups').json()['backups'] == []
        assert web.post(f'/v1/backups/{rev}', json={'byteSize': len(pdf)}).json() == {'offset': 0}
        first = pdf[:mobile_backup.CHUNK]
        octet = {'Content-Type': 'application/octet-stream'}
        assert web.post(f'/v1/backups/{rev}/chunks/0', content=first, headers=octet).json()['offset'] == len(first)
        # Reconnecting resumes where the copy stopped; a wrong offset is refused.
        assert web.post(f'/v1/backups/{rev}', json={'byteSize': len(pdf)}).json() == {'offset': len(first)}
        assert web.post(f'/v1/backups/{rev}/chunks/0', content=first, headers=octet).status_code == 409
        web.post(f'/v1/backups/{rev}/chunks/{len(first)}', content=pdf[len(first):], headers=octet)
        meta = {'title': 'Phone paper', 'pageCount': 3, 'notes': [{'quote': 'q'}], 'ink': {'v': 1, 'strokes': []}}
        assert web.post(f'/v1/backups/{rev}/finish', json=meta).json() == {'ok': True}
        [item] = web.get('/v1/backups').json()['backups']
        assert item['title'] == 'Phone paper' and item['byteSize'] == len(pdf) and item['pageCount'] == 3
        assert web.get(f'/v1/backups/{rev}/pdf').content == pdf
        assert web.get(f'/v1/backups/{rev}/meta').json()['notes'] == [{'quote': 'q'}]
        # A second backup of the same PDF only refreshes the notes.
        assert web.post(f'/v1/backups/{rev}', json={'byteSize': len(pdf)}).json()['done'] is True
        # Bytes that do not match the revision are thrown away.
        bad = hashlib.sha256(b'other').hexdigest()
        web.post(f'/v1/backups/{bad}', json={'byteSize': 5})
        web.post(f'/v1/backups/{bad}/chunks/0', content=b'%PDF!', headers=octet)
        assert web.post(f'/v1/backups/{bad}/finish', json=meta).status_code == 409
        assert web.get(f'/v1/backups/{bad}/pdf').status_code == 404
        monkeypatch.setattr(mobile_backup, 'MAX_TOTAL', len(pdf) + 10)
        other = hashlib.sha256(b'third').hexdigest()
        assert web.post(f'/v1/backups/{other}', json={'byteSize': 100}).status_code == 507
    assert registry.one("SELECT count(*) AS n FROM papers")["n"] == papers_before  # never imported
    assert (config.data_dir / 'phone_backup' / rev / 'paper.pdf').read_bytes() == pdf


def test_new_papers_and_my_notes_reach_the_phone_without_sharing_again(lan, registry, config, tmp_path):
    import pymupdf as fitz

    pair = lan.start('127.0.0.1')
    stem = 'paper_x'
    folder = config.knowledge_dir / 'papers' / stem
    folder.mkdir(parents=True)
    (folder / f'{stem}.md').write_text('## 一句话结论\n\nA.\n', 'utf-8')
    notes = folder / f'{stem}.notes.md'
    notes.write_text('## 与我相关的点\n\n- first thought\n', 'utf-8')
    registry.execute("UPDATE papers SET kb_path=? WHERE id=1", (f'papers/{stem}/{stem}.md',))
    with client(pair) as web:
        listed = {p['id']: p for p in web.get('/v1/papers').json()['papers']}
        assert set(listed) == {1, 2}
        base = f"/v1/papers/1/{listed[1]['revision']}"
        companion = web.get(base + '/companion').json()
        assert companion['notes'] == '## 与我相关的点\n\n- first thought\n'  # read-only on the phone
        # Editing the notes on the computer changes the version the phone checks on each sync.
        notes.write_text('## 与我相关的点\n\n- first thought\n- second thought\n', 'utf-8')
        again = {p['id']: p for p in web.get('/v1/papers').json()['papers']}
        assert again[1]['companionVersion'] != listed[1]['companionVersion']
        assert 'second thought' in web.get(base + '/companion').json()['notes']
        # A paper imported after the share started is offered by itself.
        pdf = tmp_path / 'later.pdf'
        with fitz.open() as doc:
            doc.new_page().insert_text((72, 72), 'Imported later')
            doc.save(pdf)
        pid = registry.create_paper(title='later', status='done')
        registry.upsert_file(hashlib.sha256(pdf.read_bytes()).hexdigest(), str(pdf), pdf.stat().st_size,
                             pdf.stat().st_mtime, pid)
        assert pid in {p['id'] for p in web.get('/v1/papers').json()['papers']}

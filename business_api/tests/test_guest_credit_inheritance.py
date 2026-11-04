import pytest
from fastapi.testclient import TestClient
from app.rent_room_main import create_app
from app.rent_room_store import RentRoomStore
from test_rent_room_capacity import FakeWeChat, login, settings
from test_rent_room_credits import V3, migration, operation

GUEST = 'hmguest00000000001'

def claim(client, headers, spent, guest=GUEST):
    return client.post('/api/rent-room/credits/guest-claims', headers={**headers, **V3}, json={'guestId': guest, 'freeSpent': spent})

@pytest.mark.parametrize('spent', [0, 1, 2])
def test_guest_spending_is_inherited_once_and_never_reset_by_login(settings, spent):
    settings = settings.model_copy(update={'free_capacity': 2})
    with TestClient(create_app(settings, store=RentRoomStore(':memory:'), provider=FakeWeChat(settings))) as client:
        headers = login(client)
        migration(client, headers, 0)
        result = claim(client, headers, spent)
        assert result.status_code == 200
        assert result.json()['credits']['remaining'] == 2 - spent
        assert result.json()['credits']['freeRemaining'] == 2 - spent
        assert claim(client, login(client), spent).json()['credits']['remaining'] == 2 - spent
        assert migration(client, headers, 0).json()['credits']['remaining'] == 2 - spent


def test_guest_claim_updates_monotonically_and_cannot_charge_paid_credits(settings):
    settings = settings.model_copy(update={'free_capacity': 2})
    ledger = RentRoomStore(':memory:')
    with TestClient(create_app(settings, store=ledger, provider=FakeWeChat(settings))) as client:
        headers = login(client)
        migration(client, headers, 0)
        session = ledger.db.execute('SELECT account FROM rr_credit_wallets').fetchone()
        order = ledger.create_order({'account': session['account'], 'openid': 'wx'}, 'paid-order', kind='credits')
        ledger.apply(order['id'], 'paid', 'wx-order')
        assert migration(client, headers, 0).json()['credits']['freeRemaining'] == 2
        reserved = operation(client, headers, 'reserve').json()['credits']
        assert reserved['remaining'] == 11 and reserved['freeRemaining'] == 1
        assert operation(client, headers, 'commit').json()['credits']['freeRemaining'] == 1
        inherited = claim(client, headers, 1).json()['credits']
        assert inherited['remaining'] == 10 and inherited['freeRemaining'] == 0
        assert claim(client, headers, 2).json()['credits']['remaining'] == 10
        assert claim(client, headers, 0).json()['credits']['remaining'] == 10
        assert claim(client, headers, 2, 'hmguest00000000002').json()['credits']['remaining'] == 10


def test_guest_claim_does_not_attach_to_another_account_and_waits_for_reservations(settings):
    settings = settings.model_copy(update={'free_capacity': 2})
    with TestClient(create_app(settings, store=RentRoomStore(':memory:'), provider=FakeWeChat(settings))) as client:
        a, b = login(client), login(client, 'B')
        migration(client, a, 0); migration(client, b, 0)
        operation(client, a, 'reserve')
        assert claim(client, a, 2).status_code == 409
        assert operation(client, a, 'cancel').json()['credits']['freeRemaining'] == 2
        assert claim(client, a, 2).json()['credits']['remaining'] == 0
        assert claim(client, b, 2).status_code == 409
        assert migration(client, b, 0).json()['credits']['remaining'] == 2


def test_guest_claim_requires_authenticated_wechat_v3_and_valid_input(settings):
    ledger = RentRoomStore(':memory:')
    with TestClient(create_app(settings, store=ledger, provider=FakeWeChat(settings))) as client:
        a = login(client)
        assert claim(client, {}, 1).status_code == 401
        assert claim(client, a, 1).status_code == 409
        migration(client, a, 0)
        for spent in [-1, True, '1', 1.5, 1000001]:
            assert claim(client, a, spent).status_code == 422
        assert claim(client, a, 1, '../outside').status_code == 422
        assert client.post('/api/rent-room/credits/guest-claims', headers=a, json={'guestId': GUEST, 'freeSpent': 1}).status_code == 409
        token = ledger.create_session('apple-account', 'apple', '', provider='apple')
        assert claim(client, {'Authorization': 'Bearer ' + token}, 1).status_code == 409


def test_claim_receipt_survives_database_reopen_and_concurrent_retry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.rent_room_credits import CreditWallets
    path = str(tmp_path / 'guest.sqlite3')
    a, b = RentRoomStore(path), RentRoomStore(path)
    wa, wb = CreditWallets(a), CreditWallets(b)
    try:
        wa.migrate('account', 0, 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda w: w.inherit_guest('account', GUEST, 1), [wa, wb]))
        assert [r['credits']['remaining'] for r in results] == [1, 1]
        wa.operate('account', 'creation-request-01', 'reserve')
        wa.operate('account', 'creation-request-01', 'commit')
        assert wb.inherit_guest('account', GUEST, 2)['credits']['remaining'] == 0
    finally:
        a.close(); b.close()
    reopened = RentRoomStore(path)
    try:
        wallet = CreditWallets(reopened)
        assert wallet.migrate('account', 0, 2)['remaining'] == 0
        assert wallet.inherit_guest('account', GUEST, 1)['credits']['remaining'] == 0
        assert reopened.db.execute('SELECT count(*) FROM rr_guest_credit_claims').fetchone()[0] == 1
    finally:
        reopened.close()

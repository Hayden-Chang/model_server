import json
import sqlite3

from fastapi.testclient import TestClient
from app.rent_room_main import create_app
from app.rent_room_settings import RentRoomSettings
from app.rent_room_store import RentRoomStore


def test_anonymous_feedback_is_persisted_and_idempotent(tmp_path):
    path = str(tmp_path / 'ledger.sqlite3')
    with TestClient(create_app(RentRoomSettings(_env_file=None), store=RentRoomStore(path))) as client:
        body = {'requestId': 'feedback-request-0001', 'ease': '方便', 'message': '  希望增加功能  '}
        for _ in range(2):
            response = client.post('/api/rent-room/feedback', json=body)
            assert response.status_code == 201
            assert response.json() == {'receipt': body['requestId']}
        assert client.post('/api/rent-room/feedback', json={**body, 'ease': '一般'}).status_code == 409
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT request_id,body FROM rr_feedback').fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][1])['message'] == '希望增加功能'
        assert 'account' not in json.loads(rows[0][1])


def test_feedback_requires_one_valid_answer_and_limits_text(tmp_path):
    with TestClient(create_app(RentRoomSettings(_env_file=None), store=RentRoomStore(':memory:'))) as client:
        base = {'requestId': 'feedback-request-0002'}
        for extra in ({}, {'message': '   '}, {'ease': 'invalid'}, {'message': 'x' * 2001}, {'account': 'forged'}):
            assert client.post('/api/rent-room/feedback', json={**base, **extra}).status_code == 422
        assert client.post('/api/rent-room/feedback', json={**base, 'functionality': '遇到问题'}).status_code == 201


def test_feedback_rate_limit_does_not_require_wechat_login():
    with TestClient(create_app(RentRoomSettings(_env_file=None), store=RentRoomStore(':memory:'))) as client:
        for n in range(20):
            assert client.post('/api/rent-room/feedback', json={'requestId': f'feedback-rate-{n:04}', 'checklist': '合理'}).status_code == 201
        assert client.post('/api/rent-room/feedback', json={'requestId': 'feedback-rate-0021', 'checklist': '合理'}).status_code == 429

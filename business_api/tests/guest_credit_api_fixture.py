"""Disposable loopback fixture for rent_room's guest-credit-api.mjs contract test.

PYTHONPATH=.:tests python -m uvicorn guest_credit_api_fixture:app --host 127.0.0.1 --port 18794
Never use production credentials, a persistent database, or an external listener.
"""
from app.rent_room_main import create_app
from app.rent_room_store import RentRoomStore
from test_rent_room_capacity import FakeWeChat, settings

config = settings.__wrapped__().model_copy(update={"free_capacity": 2})
app = create_app(config, store=RentRoomStore(":memory:"), provider=FakeWeChat(config))

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.observability import ModelCallCapture, TokenUsage
from app.usage_store import InferenceCapture, UsageStore


NOW = datetime(2026, 8, 25, 6, 30, tzinfo=timezone.utc)


def model_call(
    *,
    call_index: int = 1,
    usage: TokenUsage | None = TokenUsage(10, 4, 14),
    at: datetime = NOW,
) -> ModelCallCapture:
    return ModelCallCapture(
        call_index=call_index,
        pipeline="time-fragment-plan-v2",
        started_at=at,
        completed_at=at + timedelta(milliseconds=120),
        duration_ms=120,
        input_content='{"text":"安排任务"}',
        output_content='{"operations":[]}',
        provider_model="provider/model-a",
        usage=usage,
        usage_complete=usage is not None,
        error_type=None,
        error_message=None,
        request_method="POST",
        request_url="http://litellm:4000/v1/chat/completions",
        request_headers={"Authorization": "Bearer ${LITELLM_MASTER_KEY}"},
        request_body={"model": "deepseek/deepseek-flash", "messages": []},
        response_status_code=200,
        response_body={"model": "deepseek-chat", "choices": []},
    )


def capture(
    *,
    request_id: str,
    device_key: str,
    at: datetime = NOW,
    calls: list[ModelCallCapture] | None = None,
) -> InferenceCapture:
    model_calls = [model_call(at=at)] if calls is None else calls
    reported = [call.usage for call in model_calls if call.usage is not None]
    usage = None
    if reported:
        usage = TokenUsage(
            sum(item.prompt_tokens for item in reported),
            sum(item.completion_tokens for item in reported),
            sum(item.total_tokens for item in reported),
        )
    return InferenceCapture(
        request_id=request_id,
        device_key=device_key,
        route="/api/plan/parse",
        pipeline="time-fragment-plan-v2",
        started_at=at,
        completed_at=at + timedelta(milliseconds=250),
        duration_ms=250,
        status_code=200,
        request_content={"text": "安排任务"},
        response_content={"proposal": {}},
        model_calls=model_calls,
        usage=usage,
        usage_complete=len(reported) == len(model_calls),
    )


def test_records_nested_model_calls_and_filters_and_aggregates_by_device() -> None:
    store = UsageStore(":memory:", content_retention_days=30)
    two_calls = [
        model_call(call_index=1, usage=TokenUsage(10, 4, 14)),
        model_call(call_index=2, usage=TokenUsage(20, 6, 26)),
    ]
    store.record(capture(request_id="request-a", device_key="guest_a", calls=two_calls))
    store.record(capture(request_id="request-b", device_key="guest_b"))

    records, total = store.list_requests(
        device_key="guest_a",
        start_time=NOW - timedelta(minutes=1),
        end_time=NOW + timedelta(minutes=1),
        limit=10,
        offset=0,
    )
    totals, devices = store.summarize(device_key=None, start_time=None, end_time=None)

    assert total == 1
    assert records[0]["request_content"] == {"text": "安排任务"}
    assert records[0]["response_content"] == {"proposal": {}}
    assert records[0]["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 10,
        "total_tokens": 40,
    }
    assert [call["call_index"] for call in records[0]["model_calls"]] == [1, 2]
    assert records[0]["model_calls"][1]["input_content"] == '{"text":"安排任务"}'
    assert records[0]["model_calls"][1]["request_body"]["model"] == "deepseek/deepseek-flash"
    assert records[0]["model_calls"][1]["response_body"]["model"] == "deepseek-chat"
    by_request, request_total = store.list_requests(
        request_id="request-a",
        device_key=None,
        start_time=None,
        end_time=None,
        limit=10,
        offset=0,
    )
    assert request_total == 1
    assert by_request[0]["request_id"] == "request-a"
    assert totals["request_count"] == 2
    assert totals["model_call_count"] == 3
    assert totals["total_tokens"] == 54
    assert [device["device_key"] for device in devices] == ["guest_a", "guest_b"]


def test_content_retention_redacts_payloads_but_keeps_usage_metadata() -> None:
    store = UsageStore(":memory:", content_retention_days=30)
    old_time = datetime.now(timezone.utc) - timedelta(days=31)
    store.record(capture(request_id="old-request", device_key="guest_old", at=old_time))

    records, total = store.list_requests(
        device_key="guest_old",
        start_time=None,
        end_time=None,
        limit=10,
        offset=0,
    )

    assert total == 1
    assert records[0]["request_content"] is None
    assert records[0]["response_content"] is None
    assert records[0]["model_calls"][0]["input_content"] is None
    assert records[0]["model_calls"][0]["output_content"] is None
    assert records[0]["model_calls"][0]["request_headers"] is None
    assert records[0]["model_calls"][0]["request_body"] is None
    assert records[0]["model_calls"][0]["response_body"] is None
    assert records[0]["model_calls"][0]["request_url"] == "http://litellm:4000/v1/chat/completions"
    assert records[0]["model_calls"][0]["response_status_code"] == 200
    assert records[0]["usage"]["total_tokens"] == 14


def test_empty_summary_returns_zero_totals_and_no_devices() -> None:
    store = UsageStore(":memory:", content_retention_days=30)

    totals, devices = store.summarize(device_key=None, start_time=None, end_time=None)

    assert totals == {
        "request_count": 0,
        "successful_requests": 0,
        "failed_requests": 0,
        "model_call_count": 0,
        "token_reported_requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "average_duration_ms": 0.0,
        "first_request_at": None,
        "last_request_at": None,
    }
    assert devices == []


def test_file_backed_database_is_owner_readable_and_writable_only(tmp_path: Path) -> None:
    database_path = tmp_path / "usage.sqlite3"

    store = UsageStore(str(database_path), content_retention_days=30)

    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    store.close()

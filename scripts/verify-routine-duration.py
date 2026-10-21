"""Five public semantic checks plus two preflight batch boundaries; synthetic only.

Run inside a candidate container with `python verify-routine-duration.py preflight`,
or use `public` to verify HTTPS and exactly five requests on a synthetic device.
There are no retries; a semantic failure exits nonzero and retains its response.
"""
import asyncio
import copy
import json
import math
import sys
import urllib.request
import uuid
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "preflight"
report = {"syntheticOnly": True, "mode": mode, "checks": []}
token = None
tracker = None


def task(identity, title, **fields):
    return {"id": identity, "title": title, "note": "", **fields}


def http(path, body=None, authenticated=False):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.keeline.xyz" + path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200
        return json.loads(response.read())


async def verify():
    global tracker, token
    before = None
    if mode == "preflight":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "business_api"))
        sys.path.insert(0, "/app")
        from app.duration_relative import RelativeRequest, analyze_relative
        from app.model_client import LiteLLMClient
        from app.observability import TrackedModelClient
        from app.settings import Settings
        class ThinkingClient:
            def __init__(self):
                self.client = LiteLLMClient(Settings())

            async def complete(self, pipeline, user_input):
                output = await self.client.complete(pipeline, user_input)
                usage = output.usage or {}
                reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                report.setdefault("thinkingCalls", []).append({
                    "operation": json.loads(user_input)["operation"], "thinking": pipeline.thinking_mode,
                    "reasoningEffort": pipeline.reasoning_effort, "reasoningTokens": reasoning_tokens})
                assert pipeline.thinking_mode == "enabled" and pipeline.reasoning_effort == "high"
                assert type(reasoning_tokens) is int and reasoning_tokens > 0, "provider did not report positive reasoning tokens"
                return output

        tracker = TrackedModelClient(ThinkingClient())
    elif mode == "public":
        token = http("/api/auth/guest", {"device_id": "routine-duration-deployment-probe"})["access_token"]
        before = http("/api/account/quota", authenticated=True)["used"]
    else:
        raise ValueError("unsupported probe mode")

    async def analyze(tasks, category=None, references=None, operation="score"):
        body = {"requestID": str(uuid.uuid4()), "operation": operation, "tasks": tasks,
                "categories": [category] if category is not None else [], "references": references or []}
        check = {"request": copy.deepcopy(body)}
        report["checks"].append(check)
        if mode == "preflight":
            value = (await analyze_relative(tracker, RelativeRequest.model_validate(body))).model_dump(mode="json", by_alias=True)
        else:
            value = http("/api/duration/relative-analysis", body, authenticated=True)
        check["response"] = copy.deepcopy(value)
        return value

    history = task("meal-history", "吃饭")
    classified = await analyze([history, task("meal-current", "吃饭"), task("shower-history", "洗澡")], operation="classify")
    categories = {category["id"]: category for category in classified["categories"]}
    assignments = {item["id"]: item["categoryID"] for item in classified["items"]}
    assert assignments["meal-history"] is not None and assignments["meal-history"] == assignments["meal-current"]
    assert assignments["shower-history"] is not None
    assert all(category.get("anchor") is None and category.get("examples", []) == [] for category in categories.values())
    meal = categories[assignments["meal-history"]]
    result = await analyze([history], meal)
    assert result["anchorTaskID"] == history["id"] and result["items"][0]["difficulty"] == 1, "ordinary meal failed to establish its single-meal anchor"
    assert result["items"][0]["references"] == []
    meal["anchor"] = {**history, "difficulty": 1}
    result = await analyze([task("meal-current", "吃饭")], meal,
                           [{**history, "categoryID": meal["id"], "difficulty": 1}])
    item = result["items"][0]
    assert result["anchorTaskID"] == history["id"] and item["difficulty"] == 1, "identical meal did not retain the existing scale"
    assert [reference["id"] for reference in item["references"]] == [history["id"]], "completed identical meal was not selected as reference"
    report["localSuggestedMinutes"] = math.ceil((30 / 1 * item["difficulty"]) / 15) * 15
    assert report["localSuggestedMinutes"] == 30

    shower = categories[assignments["shower-history"]]
    result = await analyze([task("shower-history", "洗澡")], shower)
    assert result["anchorTaskID"] == "shower-history" and result["items"][0]["difficulty"] == 1, "ordinary shower failed to establish a natural-unit anchor"
    development = {"id": "development", "name": "开发", "definition": "软件开发",
                   "anchor": task("page-anchor", "实现三个设置项的静态页面", difficulty=1)}
    result = await analyze([task("budget-30", "专注开发30分钟"), task("budget-60", "专注开发60分钟")], development,
                           [task("page-history", "实现三个设置项的静态页面", categoryID="development", difficulty=1)])
    assert all(item["difficulty"] is None and item["references"] == [] for item in result["items"]), "time budgets invented a workload score"
    if mode == "preflight":
        batch = [task("batch-" + str(index), "吃饭") for index in range(32)]
        result = await analyze(batch, meal, operation="classify")
        assert len(result["items"]) == 32 and all(item["categoryID"] == meal["id"] for item in result["items"])
        result = await analyze(batch, meal, [{**history, "categoryID": meal["id"], "difficulty": 1}])
        assert len(result["items"]) == 32 and all(item["difficulty"] == 1 for item in result["items"])
        assert all([reference["id"] for reference in item["references"]] == [history["id"]] for item in result["items"])
    if before is not None:
        report["quotaDelta"] = http("/api/account/quota", authenticated=True)["used"] - before
        assert report["quotaDelta"] == 5


try:
    asyncio.run(verify())
    report["status"] = "passed"
except BaseException as error:
    report["status"] = "failed"
    report["failureClass"] = type(error).__name__
    if isinstance(error, AssertionError):
        report["failure"] = str(error)
finally:
    token = None
    if tracker is not None:
        report["modelCalls"] = [{"pipeline": call.pipeline, "providerModel": call.provider_model,
                                 "output": call.output_content, "errorType": call.error_type}
                                for call in tracker.calls]
    print(json.dumps(report, ensure_ascii=False))
if report["status"] != "passed":
    raise SystemExit(1)

"""Four bounded synthetic semantic checks; no user history or time values are sent.

Run inside a candidate container with `python verify-routine-duration.py preflight`,
or use `public` to verify HTTPS and exactly four requests on a synthetic device.
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
        tracker = TrackedModelClient(LiteLLMClient(Settings()))
    elif mode == "public":
        token = http("/api/auth/guest", {"device_id": "routine-duration-deployment-probe"})["access_token"]
        before = http("/api/account/quota", authenticated=True)["used"]
    else:
        raise ValueError("unsupported probe mode")

    async def analyze(tasks, category, references=None):
        body = {"requestID": str(uuid.uuid4()), "operation": "score", "tasks": tasks,
                "categories": [category], "references": references or []}
        if mode == "preflight":
            value = (await analyze_relative(tracker, RelativeRequest.model_validate(body))).model_dump(mode="json", by_alias=True)
        else:
            value = http("/api/duration/relative-analysis", body, authenticated=True)
        report["checks"].append({"request": copy.deepcopy(body), "response": value})
        return value

    meal = {"id": "meal", "name": "吃饭", "definition": "进食一餐的日常活动"}
    history = task("meal-history", "吃饭")
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

    shower = {"id": "shower", "name": "洗澡", "definition": "完成一次日常洗浴"}
    result = await analyze([task("shower-history", "洗澡")], shower)
    assert result["anchorTaskID"] == "shower-history" and result["items"][0]["difficulty"] == 1, "ordinary shower failed to establish a natural-unit anchor"
    development = {"id": "development", "name": "开发", "definition": "软件开发",
                   "anchor": task("page-anchor", "实现三个设置项的静态页面", difficulty=1)}
    result = await analyze([task("budget-30", "专注开发30分钟"), task("budget-60", "专注开发60分钟")], development,
                           [task("page-history", "实现三个设置项的静态页面", categoryID="development", difficulty=1)])
    assert all(item["difficulty"] is None and item["references"] == [] for item in result["items"]), "time budgets invented a workload score"
    if before is not None:
        report["quotaDelta"] = http("/api/account/quota", authenticated=True)["used"] - before
        assert report["quotaDelta"] == 4


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

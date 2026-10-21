"""Bounded real-model regression: run inside a candidate container, then against HTTPS.

Usage: python verify-relative-duration.py preflight|public
Preflight uses the runtime model settings and prints a synthetic-only report. Save it
as preflight.json beside this script before public mode; public uses one fixed
synthetic device, two successful requests and negative auth/input checks.
Credentials stay in memory. Neither mode retries a semantic failure.
"""
import asyncio
import datetime
import json
import math
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

root = Path(__file__).resolve().parent
mode = sys.argv[1]
report = {"syntheticOnly": True, "mode": mode, "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "checks": []}
token = None
model_tracker = None


def task(id, title, note="", **extra):
    return {"id": id, "title": title, "note": note, **extra}


def request(operation, tasks, categories, references=None):
    body = {"requestID": str(uuid.uuid4()), "operation": operation, "tasks": tasks, "categories": categories}
    if references is not None: body["references"] = references
    return body


def dev_category():
    return {"id": "software", "name": "软件开发", "definition": "开发软件，包括界面交付和技术问题排查", "anchor": task("dev-anchor", "实现一个 SwiftUI 静态设置页面", "三个设置项：名称、开关、说明；无网络、持久化或其他业务逻辑。", difficulty=1)}


def ui_references():
    return [task("gpu-bug", "排查无法稳定复现的 Linux GPU 驱动死锁", "需要内核调试和硬件联调", categoryID="software", difficulty=32)] + [task("ui-" + str(i), "实现一个 SwiftUI 静态设置页面", "三个设置项：名称、开关、说明；无网络、持久化或其他业务逻辑。", categoryID="software", difficulty=1) for i in range(6, 0, -1)]


def call(path, payload=None, auth=False, expected=200, capture=True):
    http_id = str(uuid.uuid4())
    headers = {"Content-Type": "application/json", "X-Request-ID": http_id}
    if auth: headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("https://api.keeline.xyz" + path, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    started = time.monotonic()
    try: response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as error: response = error
    with response:
        body = json.loads(response.read())
        entry = {"path": path, "status": response.status, "requestID": http_id, "elapsedSeconds": round(time.monotonic()-started, 3)}
        if capture: entry["response"] = body
        report["checks"].append(entry)
        assert response.status == expected, path + ": HTTP " + str(response.status) + ", expected " + str(expected)
    return body


async def preflight():
    global model_tracker
    sys.path.insert(0, str(root.parent / "business_api"))
    sys.path.insert(0, "/app")
    from app.duration_relative import RelativeRequest, analyze_relative
    from app.model_client import LiteLLMClient
    from app.observability import TrackedModelClient
    from app.settings import Settings
    tracker = TrackedModelClient(LiteLLMClient(Settings()))
    model_tracker = tracker
    async def analyze(body):
        started = time.monotonic()
        response = await analyze_relative(tracker, RelativeRequest.model_validate(body))
        value = response.model_dump(mode="json", by_alias=True)
        report["checks"].append({"input": body, "response": value, "elapsedSeconds": round(time.monotonic()-started, 3)})
        return value
    script = {"id": "script", "name": "口播文案", "definition": "为短视频撰写口播脚本"}
    body = request("classify", [task("script-synonym", "写短视频口播文案"), task("piano", "练习钢琴C大调双手音阶"), task("vague", "处理一下")], [script])
    response = await analyze(body); items = {x["id"]: x for x in response["items"]}
    assert items["script-synonym"]["categoryID"] == "script", "synonym did not reuse existing category"
    assert items["vague"]["categoryID"] is None, "vague task received an invented category"
    new = {x["id"]: x for x in response["categories"]}
    piano = items["piano"]["categoryID"]
    assert piano in new and piano != "script", "new practice category missing"
    report["practiceCategory"] = new[piano]
    response = await analyze(request("score", [task("budget-30", "专注开发30分钟"), task("budget-60", "专注开发60分钟")], [dev_category()], ui_references()))
    assert all(x["difficulty"] is None and x["references"] == [] for x in response["items"]), "effort budget incorrectly produced difficulty or comparable references"
    report["modelCallCount"] = len(tracker.calls)
    report["modelCalls"] = [{"pipeline": c.pipeline, "providerModel": c.provider_model, "totalTokens": c.usage.total_tokens if c.usage else None, "errorType": c.error_type} for c in tracker.calls]
    assert len(tracker.calls) == 2


def public():
    global token
    assert call("/health/live")["status"] == "ok"
    assert call("/health/ready")["status"] == "ready"
    route = "/api/duration/relative-analysis"
    body = request("score", [task("saved", "实现六个设置项的 SwiftUI 静态设置页面", "无网络、持久化或其他业务逻辑。", difficulty=1.75), task("new-page", "实现一个 SwiftUI 静态设置页面", "三个设置项：名称、开关、说明；无网络、持久化或其他业务逻辑。")], [dev_category()], ui_references())
    call(route, body, expected=401)
    token = call("/api/auth/guest", {"device_id": "relative-difficulty-deployment-probe"}, capture=False)["access_token"]
    before = call("/api/account/quota", auth=True)
    invalid = json.loads(json.dumps(body)); invalid["requestID"] = str(uuid.uuid4()); invalid["references"][0]["actualMinutes"] = 90
    call(route, invalid, auth=True, expected=422)
    after_invalid = call("/api/account/quota", auth=True)
    assert after_invalid["used"] == before["used"], "invalid time-field input consumed quota"
    response = call(route, body, auth=True); values = {x["id"]: x for x in response["items"]}
    assert response["anchorTaskID"] == "dev-anchor", "fixed anchor changed"
    assert values["saved"]["difficulty"] == 1.75, "persisted difficulty changed"
    current = values["new-page"]
    assert type(current["difficulty"]) in (int, float) and math.isfinite(current["difficulty"]) and current["difficulty"] > 0
    selected = [x["id"] for x in current["references"]]
    assert 1 <= len(selected) <= 5 and selected == ["ui-6", "ui-5", "ui-4", "ui-3", "ui-2", "ui-1"][:len(selected)], "did not select newest genuinely comparable references"
    report["selectedReferences"] = selected
    after_one = call("/api/account/quota", auth=True)
    assert after_one["used"] == before["used"] + 1, "one successful model call did not consume exactly one quota"
    category = json.loads((root / "preflight.json").read_text())["practiceCategory"]
    bootstrap = request("score", [task("practice-anchor", "练习钢琴C大调双手一个八度音阶", "慢速连奏，完成规定音阶练习")], [category], [])
    response = call(route, bootstrap, auth=True)
    assert response["anchorTaskID"] == "practice-anchor" and response["items"][0]["difficulty"] == 1 and response["items"][0]["references"] == [], "history-free category did not establish exactly one text anchor"
    after_two = call("/api/account/quota", auth=True)
    assert after_two["used"] == before["used"] + 2, "two successful requests consumed unexpected quota"
    report["quotaDelta"] = 2
    report["publicModelRequests"] = 2


try:
    if mode == "preflight": asyncio.run(preflight())
    elif mode == "public": public()
    else: raise ValueError("unsupported probe mode")
    report["status"] = "passed"
except BaseException as error:
    report["status"] = "failed"
    report["failureClass"] = type(error).__name__
    if isinstance(error, ModuleNotFoundError): report["missingModule"] = error.name
    if isinstance(error, AssertionError): report["failure"] = str(error)
finally:
    token = None
    if mode == "preflight":
        if model_tracker is not None:
            report["modelCallCount"] = len(model_tracker.calls)
            report["modelCalls"] = [{"pipeline": c.pipeline, "providerModel": c.provider_model, "totalTokens": c.usage.total_tokens if c.usage else None, "output": c.output_content, "errorType": c.error_type} for c in model_tracker.calls]
        print(json.dumps(report, ensure_ascii=False))
    else:
        (root / "live-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "quotaDelta": report.get("quotaDelta"), "selectedReferences": report.get("selectedReferences")}, ensure_ascii=False))
if report["status"] != "passed": raise SystemExit(1)

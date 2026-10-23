import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError, SecretStr
from app.account_api import create_account_api
from app.account_backend import AccountBackend, Actor
from app.duration_dynamic import (AnalysisRunRequest, DynamicStepRequest, DynamicCategory,
                                  analyze_dynamic, category_steps, classification_step)
from app.duration_runs import DurationRuns
from app.factory import create_app
from app.planning_auth import PlanningCredentials
from test_account_api import configuration, SECRET
from test_time_fragment_api import FakeModelClient, raw_output, settings, guest_headers


def text(id, category=None):
    return dict(id=id, title="吃饭", note="", categoryID=category)


def body(count=1):
    return dict(runID="dynamic-1", categories=[dict(id="meal", name="吃饭", definition="日常进餐")],
                currentTasks=[text("t"+str(i), "meal") for i in range(count)], historyTasks=[])


def joint():
    return DynamicStepRequest(requestID="run", operation="joint", categories=body()["categories"],
        currentTasks=[dict(id="now", title="开发", note="")], historyTasks=[dict(**text("old", "meal"),
            completedAt="2026-10-21T18:00:00Z", minutes=30)])


def output():
    return dict(baselineTaskID="old", baselineReason="以最近一项为动态基准", items=[
        dict(id="old", difficulty=1, reason="已完成的自然单元", references=[]),
        dict(id="now", difficulty=1.5, reason="按已有信息推断常规范围", references=[dict(id="old", reason="可比")])])


def analyze(request, response):
    client=FakeModelClient([raw_output(json.dumps(response,ensure_ascii=False))])
    return asyncio.run(analyze_dynamic(client,request)),client


def test_joint_scores_sparse_text_and_history_on_same_positive_dynamic_scale():
    result,client=analyze(joint(),output())
    assert result["items"][1]["difficulty"]==1.5
    assert json.loads(client.calls[0][1])["historyTasks"][0]["minutes"]==30
    pipeline=client.calls[0][0]
    assert pipeline.thinking_mode=="enabled" and pipeline.reasoning_effort=="high"
    assert pipeline.timeout_seconds==45
    request=joint().model_copy(update={"history_tasks":[]})
    response=dict(baselineTaskID="now",baselineReason="推断常规单次范围",items=[dict(id="now",difficulty=1,reason="信息有限，按常规范围推断",references=[])])
    assert analyze(request,response)[0]["items"][0]["difficulty"]==1


@pytest.mark.parametrize("invalid",[None,0,-1,True,"1",float("inf"),float("nan")])
def test_joint_rejects_nonpositive_missing_or_nonfinite_scores(invalid):
    response=output();response["items"][1]["difficulty"]=invalid
    with pytest.raises(Exception,match="invalid dynamic duration response"):
        analyze(joint(),response)


@pytest.mark.parametrize("change",["missing","foreign","baseline","self","duplicate"])
def test_joint_rejects_incomplete_or_incoherent_snapshots(change):
    response=output()
    if change=="missing":response["items"].pop()
    if change=="foreign":response["items"][1]["references"][0]["id"]="not-supplied"
    if change=="baseline":response["items"][0]["difficulty"]=2
    if change=="self":response["items"][0]["references"]=[dict(id="old",reason="self")]
    if change=="duplicate":response["items"][1]["id"]="old"
    with pytest.raises(Exception,match="invalid dynamic duration response"):
        analyze(joint(),response)


def test_classification_has_no_scoring_prerequisite_and_checkpoints_large_bootstrap():
    request=body();request["currentTasks"][0]["categoryID"]=None
    request["historyTasks"]=[dict(**text("h"+str(i)),completedAt="2026-10-21T18:00:00Z",minutes=30) for i in range(270)]
    run=AnalysisRunRequest.model_validate(request)
    first=classification_step(run);assert len(first["tasks"])==128
    response=dict(categories=[],items=[dict(id=x["id"],categoryID="meal",reason="同类活动") for x in first["tasks"]])
    assert len(analyze(DynamicStepRequest.model_validate(first),response)[0]["items"])==128
    second=classification_step(run,response);assert len(second["tasks"])==128
    assert set(x["id"] for x in first["tasks"]).isdisjoint(x["id"] for x in second["tasks"])
    with pytest.raises(ValidationError):DynamicCategory(id="__classify__",name="x",definition="x")
    with pytest.raises(Exception,match="invalid dynamic duration response"):
        analyze(DynamicStepRequest.model_validate(first),dict(categories=[],items=[dict(id=x["id"],categoryID="foreign",reason="x") for x in first["tasks"]]))


def test_recent_five_and_minutes_are_in_snapshot_fingerprint_without_twenty_total_cap():
    request=body(27);request["categories"]=[]
    for i,task in enumerate(request["currentTasks"]):
        task["categoryID"]="cat"+str(i)
        request["categories"].append(dict(id=task["categoryID"],name="类别"+str(i),definition="活动"+str(i)))
    request["historyTasks"]=[dict(**text("h"+str(i),"cat0"),
        completedAt=(datetime(2026,10,21,tzinfo=timezone.utc)-timedelta(days=i)).isoformat(),minutes=30+i*15) for i in reversed(range(7))]
    steps=category_steps(AnalysisRunRequest.model_validate(request));assert len(steps)==27
    meal=next(x for x in steps if x["key"]=="cat0")
    assert [x["id"] for x in meal["input"]["historyTasks"]]==["h0","h1","h2","h3","h4"]
    changed=deepcopy(request);changed["historyTasks"][-1]["minutes"]=90
    after=next(x for x in category_steps(AnalysisRunRequest.model_validate(changed)) if x["key"]=="cat0")
    assert meal["inputHash"]!=after["inputHash"]


def test_internal_dynamic_route_requires_body_bound_auth_and_keeps_private_inputs(settings):
    payload=joint().model_dump(mode="json",by_alias=True)
    client=FakeModelClient([raw_output(json.dumps(output(),ensure_ascii=False))])
    config=settings.model_copy(update={"planning_internal_secret":SecretStr(SECRET),"planning_internal_only":True})
    app=create_app(config,client)
    with TestClient(app) as api:
        assert api.post('/internal/time-fragment/dynamic-duration',json=payload).status_code==401
        token=PlanningCredentials(SECRET).issue('guest_'+'a'*24,payload,'attempt')
        result=api.post('/internal/time-fragment/dynamic-duration',json=payload,headers={'Authorization':'Bearer '+token})
        assert result.status_code==200,result.text
        assert result.json()['items'][1]['difficulty']==1.5


def test_public_run_route_uses_device_identity_and_one_durable_rpc(configuration):
    calls=[]
    async def handle(request):
        value=json.loads(request.content);calls.append(value)
        if request.url.path.endswith('ai_quota_service'):
            return httpx.Response(200,json={'supportCode':'TF-AAAA-AAAA','used':0,'limit':30,'remaining':30,'enabled':False})
        assert request.url.path.endswith('duration_analysis_run_service')
        return httpx.Response(200,json={'runID':'dynamic-1','status':'running','steps':[],'chargedPoints':0,'classification':None,'startedAt':'2026-10-22T00:00:00Z'})
    backend=AccountBackend(configuration,httpx.MockTransport(handle))
    with TestClient(create_account_api(configuration,backend,run_workers=False)) as api:
        headers=guest_headers(api,'dynamic-installation-test-1234')
        calls.clear()
        result=api.post('/api/duration/analysis-runs',json=body(),headers=headers)
        assert result.status_code==202,result.text
        assert [x['p_action'] for x in calls]==['start']
        data=calls[0]['p_data'];assert data['principal'].startswith('guest_')
        assert data['billingEnvironment']=='sandbox' and len(data['inputHash'])==64
        assert api.get('/api/duration/analysis-runs/dynamic-1',headers=headers).status_code==200
        assert api.post('/api/duration/analysis-runs/dynamic-1/retry',headers=headers).status_code==202
        assert [x['p_action'] for x in calls]==['start','get','retry']
        assert api.get('/api/duration/analysis-runs/dynamic-1').status_code==401


@pytest.mark.parametrize("failure",[None,"model","save","invalid"])
def test_worker_saves_validated_result_and_retains_success_on_save_failure(configuration,failure):
    class Backend:
        settings=configuration
        def __init__(self):self.calls=[]
        async def _rpc(self,name,data):
            self.calls.append((name,data))
            if failure=="save":raise RuntimeError("ambiguous checkpoint failure")
            return {"status":"completed"}
        async def _inference(self,actor,payload,attempt,request_id,route):
            assert route=="/internal/time-fragment/dynamic-duration"
            if failure=="model":raise RuntimeError("upstream unavailable")
            value=output()
            if failure=="invalid":value['items'][1]['difficulty']=None
            return value
    backend=Backend();worker=DurationRuns(backend)
    claim=dict(principal='guest_'+'a'*24,billingEnvironment='sandbox',runID='run',key='meal',
               attempt='attempt',input=joint().model_dump(mode='json',by_alias=True),inputHash='digest')
    if failure=="save":
        with pytest.raises(RuntimeError,match="checkpoint"):
            asyncio.run(worker.execute_one(claim))
    else:asyncio.run(worker.execute_one(claim))
    actions=[x[1]['p_action'] for x in backend.calls]
    assert actions==["fail" if failure in ("model","invalid") else "complete"]
    if failure is None:
        result=backend.calls[0][1]['p_data']['result']
        assert result['scaleID']=='run:meal' and result['historyTasks'][0]['minutes']==30
        assert result['items'][1]['difficulty']==1.5


def test_api_lifespan_starts_recovery_worker_and_cancels_before_closing_backend(configuration,monkeypatch):
    events=[]
    started=asyncio.Event()
    async def worker(self):
        events.append('worker-start');started.set()
        try:await asyncio.Event().wait()
        finally:events.append('worker-stop')
    class Backend(AccountBackend):
        async def close(self):
            events.append('backend-close');await super().close()
    monkeypatch.setattr(DurationRuns,'worker',worker)
    backend=Backend(configuration,httpx.MockTransport(lambda _:httpx.Response(200,json={})))
    api=create_account_api(configuration,backend)
    async def run():
        async with api.router.lifespan_context(api):
            await asyncio.wait_for(started.wait(),1)
    asyncio.run(run())
    assert events==['worker-start','worker-stop','backend-close']

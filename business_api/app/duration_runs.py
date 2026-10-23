"""Recoverable queue consumer; database leases fence work across API processes."""
import asyncio
import logging
from uuid import uuid4

from .account_backend import Actor, failure, support_code
from .duration_dynamic import (AnalysisRunRequest, DynamicStepRequest, category_steps,
                               classification_step, fingerprint, validate_dynamic_result)

LOGGER = logging.getLogger(__name__)


class DurationRuns:
    def __init__(self, backend):
        self.backend = backend

    async def rpc(self, action, **data):
        data.setdefault("billingEnvironment", self.backend.settings.apple_environment)
        result = await self.backend._rpc("duration_analysis_run_service", {"p_action": action, "p_data": data})
        if code := result.pop("code", None):
            raise failure(code, {"ANALYSIS_RUN_NOT_FOUND": 404, "DEVICE_REQUIRED": 401,
                                 "AI_QUOTA_EXHAUSTED": 429, "AI_DAILY_QUOTA_EXHAUSTED": 429}.get(code, 409))
        return result

    def identity(self, actor, run_id):
        return dict(principal=actor.principal, billingEnvironment=self.backend.settings.apple_environment, runID=run_id)

    async def start(self, actor, request: AnalysisRunRequest):
        body = request.model_dump(mode="json", by_alias=True)
        if request.language is None:
            body.pop("language", None)  # Preserve the hash of pre-language requests on replay.
        classification = classification_step(request)
        steps = ([{"key": "__classify__", "input": classification, "inputHash": fingerprint(classification)}]
                 if classification else category_steps(request))
        return await self.rpc("start", **self.identity(actor, request.run_id), input=body, inputHash=fingerprint(body),
            attempt=str(uuid4()), steps=steps, quotaData=dict(supportCode=support_code(actor.principal),
                freeLimit=self.backend.settings.time_fragment_guest_quota_limit,
                memberLimit=self.backend.settings.time_fragment_member_quota_limit))

    async def status(self, actor, run_id, retry=False):
        return await self.rpc("retry" if retry else "get", **self.identity(actor, run_id), attempt=str(uuid4()))

    async def execute_one(self, claim=None):
        claim = claim or await self.rpc("claim", attempt=str(uuid4()))
        if not claim:
            return False
        identity = {k: claim[k] for k in ("principal", "billingEnvironment", "runID", "key", "attempt")}
        try:
            payload = DynamicStepRequest.model_validate(claim["input"])
            result = await self.backend._inference(Actor(claim["principal"]),
                payload.model_dump(mode="json", by_alias=True, exclude_none=True), claim["attempt"], claim["runID"],
                "/internal/time-fragment/dynamic-duration")
            result = validate_dynamic_result(payload, result)
            steps = []
            next_input = None
            if payload.operation == "classify":
                previous = claim.get("classification") or {"categories": [], "items": []}
                result = {"categories": previous["categories"] + result["categories"],
                          "items": previous["items"] + result["items"]}
                original = AnalysisRunRequest.model_validate(claim["runInput"])
                next_input = classification_step(original, result)
                if next_input is None:
                    steps = category_steps(original, result)
            else:
                result = dict(result, rulesVersion=1, categoryID=claim["key"], inputHash=claim["inputHash"],
                    scaleID=claim["runID"] + ":" + claim["key"], language=payload.language or "zh-Hans",
                    category=result.get("category") or claim["input"]["categories"][0],
                    categoryInput=claim["input"]["categories"][0],
                    currentTasks=claim["input"]["currentTasks"], historyTasks=claim["input"]["historyTasks"])
        except asyncio.CancelledError:
            raise  # Leave the lease recoverable; never mark cancellation as success.
        except Exception:
            LOGGER.warning("duration_analysis_step_failed", extra={"runID": claim["runID"], "step": claim["key"]})
            await self.rpc("fail", **identity)
            return True
        # A persistence failure must NOT turn a successful paid response into a
        # model failure. RPC's same-attempt transport retry can replay this write.
        await self.rpc("complete", **identity, result=result, steps=steps, nextInput=next_input)
        return True

    async def worker(self):
        active = set()
        async def execute(claim):
            try:
                await self.execute_one(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("duration_analysis_queue_unavailable")
        try:
            while True:
                active = {task for task in active if not task.done()}
                if len(active) == 20:
                    await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    claim = await self.rpc("claim", attempt=str(uuid4()))
                except Exception:
                    LOGGER.warning("duration_analysis_queue_unavailable")
                    await asyncio.sleep(5)
                    continue
                if claim:
                    active.add(asyncio.create_task(execute(claim)))
                else:
                    await asyncio.sleep(1)
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

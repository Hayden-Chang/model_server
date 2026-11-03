# WeChat payment diagnostics (2026-11-03)

An HTTP 200 response from WeChat can still contain a nonzero `errcode`. Previously the log omitted order identity and `errmsg`, so an incident could not reliably be tied to the customer's order. Native payment failures were also absent from the server. This change records bounded payment-specific evidence without changing entitlement or refund decisions.

## Contract and data boundaries

- The v3 catalog advertises `paymentDiagnostics: 1`. Older catalog contracts are unchanged; older clients continue working.
- Authenticated WeChat clients may POST `/api/rent-room/orders/request/{requestId}/diagnostics` for an order owned by their account. Cross-account or non-WeChat requests receive 404.
- Accepted stages: `native_start`, `native_success`, `native_fail`, `retry_confirmed`. Context: numeric `errCode`, sanitized `errMsg`, duration, platform, device model, OS, WeChat version, SDK version and mini-program version. Unknown fields are rejected.
- Device reports are untrusted observations, never proof of payment. A native success cannot grant credits, and a native failure cannot close, delete or refund an order.
- `rr_payment_events` stores at most 60 events per order, deduplicated by `(order_id,event_id)`. Records expire after 30 days; insertion and the existing reconciliation worker prune them. This retention applies to the table; stdout retention remains the deployment's log policy.
- Neither full request/response payloads nor session keys, bearer tokens, OpenIDs, signatures or payment parameters are logged. Error text is redacted, normalized and truncated to 240 characters. Context fields are length-limited.

## Investigating the next incident

Filter structured stdout entries with `event=rent_room_payment` and the order's `orderId` or `requestId`. Stages cover checkout, client callback, upstream business/transport failure, query result, reconciliation result and encrypted notification handling. `upstream_failed` distinguishes HTTP status from business `errCode` and keeps a sanitized `errMsg`; do not infer payment status from HTTP 200 alone.

Read persisted client evidence with a parameterized query (use the existing read-only production connection):

```sql
SELECT e.created, o.id AS order_id, o.request_id, e.detail
FROM rr_payment_events e JOIN rr_orders o ON o.id = e.order_id
WHERE o.id = ? ORDER BY e.created, e.event_id;
```

Old orders have no retroactive device/native-error data. Missing diagnostic reports are inconclusive: delivery is best effort and a device may be offline. Upstream `268490002` is a request-parameter error whose detailed `errmsg` matters, not proof that the user refused payment or that the order is missing.

Official contracts: [native virtual payment](https://developers.weixin.qq.com/miniprogram/dev/api/payment/wx.requestVirtualPayment.html), [virtual-payment order query](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order).

## Validation and delivery boundary

Base: `c088ec6bc1d7eb8418c639132c419739b516c713` in the isolated `codex/rent-room-payment-diagnostics` worktree. New diagnostics regressions failed before implementation (missing route/log correlation), then the following incremental selection passed: **98 tests**.

```sh
PYTHONPATH=business_api python -m pytest -q \
  business_api/tests/test_rent_room_payment_diagnostics.py \
  business_api/tests/test_rent_room_capacity.py \
  business_api/tests/test_rent_room_capacity_policy.py \
  business_api/tests/test_rent_room_credits.py
```

Coverage includes persistence/reopen, ownership, idempotency, size/rate/retention boundaries, redaction, HTTP-200 business failures, encrypted notification correlation, reconciliation, legacy capacity and v3 credit behavior. Local evidence is under `artifacts/payment-retry/`; final commit identity is recorded in the PR. Providers and databases are fixtures, not a live payment test.

Deploy the backend before releasing the new client to enable diagnostics. The additive table can remain on rollback; do not remove order/event rows during rollback. This PR does not deploy, alter historical orders, perform a payment or establish the original incident's native error.

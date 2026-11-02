"""Web Push subscriptions of the signed-in user (see dashboard_api/web_push.py).

Every route acts on the caller's own browsers only. What a subscription will
receive is decided at send time from the workspaces the user is granted, so
registering one never widens what anybody sees.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from dashboard_api import auth
from dashboard_api.web_push import MAX_KEY_LENGTH, endpoint_allowed


router = APIRouter()


def _main():
    # Late, for the same reason as routes/orders.py.
    from dashboard_api import main

    return main


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=MAX_KEY_LENGTH)
    auth: str = Field(min_length=1, max_length=MAX_KEY_LENGTH)


class SubscriptionBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class EndpointBody(BaseModel):
    endpoint: str


@router.get("/api/push/config")
async def push_config():
    auth.require_current_user()
    main = _main()
    return {"public_key": await asyncio.to_thread(main.push_store.public_key)}


@router.post("/api/push/subscriptions")
async def subscribe(body: SubscriptionBody, request: Request):
    user = auth.require_current_user()
    if not endpoint_allowed(body.endpoint):
        raise HTTPException(status_code=400, detail="not a browser push service endpoint")
    await asyncio.to_thread(
        _main().push_store.save,
        user_id=int(user["id"]), endpoint=body.endpoint,
        p256dh=body.keys.p256dh, auth=body.keys.auth,
        user_agent=request.headers.get("user-agent", ""),
    )
    return {"ok": True}


@router.post("/api/push/subscriptions/delete")
async def unsubscribe(body: EndpointBody):
    user = auth.require_current_user()
    removed = await asyncio.to_thread(
        _main().push_store.delete, body.endpoint, user_id=int(user["id"]),
    )
    return {"removed": removed}


@router.post("/api/push/test")
async def send_test():
    user = auth.require_current_user()
    delivered = await asyncio.to_thread(_main().web_push.send_test, int(user["id"]))
    return {"delivered": delivered}

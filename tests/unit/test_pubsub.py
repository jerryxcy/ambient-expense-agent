"""Unit tests for Pub/Sub subscription normalization."""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.app_utils.pubsub import (
    PubSubSubscriptionMiddleware,
    normalize_pubsub_body,
    normalize_subscription,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("projects/my-proj/subscriptions/expense-sub", "expense-sub"),
        ("/projects/my-proj/subscriptions/expense-sub/", "expense-sub"),
        ("expense-sub", "expense-sub"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_subscription(raw, expected):
    assert normalize_subscription(raw) == expected


def test_normalize_pubsub_body_rewrites_subscription():
    body = json.dumps(
        {"message": {"data": "e30="}, "subscription": "projects/p/subscriptions/s"}
    ).encode()
    envelope = json.loads(normalize_pubsub_body(body))
    assert envelope["subscription"] == "s"
    assert envelope["message"] == {"data": "e30="}


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"message": {}}'])
def test_normalize_pubsub_body_leaves_other_bodies_untouched(body):
    assert normalize_pubsub_body(body) == body


def _echo_client() -> TestClient:
    app = FastAPI()

    @app.post("/apps/{app_name}/trigger/pubsub")
    async def pubsub(request: Request):
        return await request.json()

    @app.post("/other")
    async def other(request: Request):
        return await request.json()

    app.add_middleware(PubSubSubscriptionMiddleware)
    return TestClient(app)


def test_middleware_normalizes_trigger_requests_only():
    client = _echo_client()
    payload = {"message": {"data": "e30="}, "subscription": "projects/p/subscriptions/s"}

    assert client.post("/apps/app/trigger/pubsub", json=payload).json()["subscription"] == "s"
    assert (
        client.post("/other", json=payload).json()["subscription"]
        == "projects/p/subscriptions/s"
    )

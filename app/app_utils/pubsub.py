# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pub/Sub push-trigger helpers.

ADK's built-in ``/apps/{app}/trigger/pubsub`` route derives the session
``user_id`` from the push envelope's ``subscription`` field. Pub/Sub sends the
fully-qualified path (``projects/<p>/subscriptions/<s>``), which ADK turns into
``projects--<p>--subscriptions--<s>``. This middleware rewrites it to ``<s>``
before the route sees it, so session records stay readable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

PUBSUB_TRIGGER_SUFFIX = "/trigger/pubsub"


def normalize_subscription(subscription: str | None) -> str | None:
    """Returns the short subscription name from a fully-qualified path.

    ``projects/my-proj/subscriptions/expense-sub`` -> ``expense-sub``.
    Short names pass through unchanged; empty values become ``None``.
    """
    if subscription is None:
        return None
    short = subscription.strip().strip("/").rsplit("/", 1)[-1]
    return short or None


def normalize_pubsub_body(body: bytes) -> bytes:
    """Rewrites the ``subscription`` field of a push envelope to its short name.

    Bodies that are not a JSON object with a string ``subscription`` are
    returned unchanged, so ADK's own validation still reports malformed input.
    """
    try:
        envelope: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(envelope, dict) or not isinstance(envelope.get("subscription"), str):
        return body

    short = normalize_subscription(envelope["subscription"])
    if short == envelope["subscription"]:
        return body
    envelope["subscription"] = short
    return json.dumps(envelope).encode("utf-8")


class PubSubSubscriptionMiddleware:
    """ASGI middleware that normalizes the subscription on Pub/Sub trigger requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].endswith(PUBSUB_TRIGGER_SUFFIX)
        ):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = normalize_pubsub_body(b"".join(chunks))

        headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
        scope = {**scope, "headers": headers}

        body_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

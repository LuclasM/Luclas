"""
adapters/whatsapp.py — Meta WhatsApp Business Cloud API adapter

Flow:
  Meta → GET /whatsapp/callback  (webhook verification)
  Meta → POST /whatsapp/callback (incoming messages, signature-verified)
       → respond 200 immediately
       → process in background thread (adapters/dispatch.py)
       → send result back via Graph API
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Query, Request, Response

from adapters import dispatch

router = APIRouter()

PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET      = os.environ.get("WHATSAPP_APP_SECRET", "")

_GRAPH_URL = "https://graph.facebook.com/v19.0/{phone_number_id}/messages"

_warned_no_secret = False


def send_text(phone: str, content: str) -> None:
    """Send a text message to a WhatsApp number."""
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return
    try:
        dispatch.post_with_retry(
            _GRAPH_URL.format(phone_number_id=PHONE_NUMBER_ID),
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "text",
                "text": {"body": content},
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[whatsapp] failed to deliver message to {phone} after retries: {e}")


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 (HMAC-SHA256 over the raw body, keyed
    by the app secret). Without WHATSAPP_APP_SECRET configured, anyone who
    discovers the callback URL could inject fake messages — so this warns
    loudly once and stays permissive rather than breaking existing setups
    that haven't added the new env var yet."""
    global _warned_no_secret
    if not APP_SECRET:
        if not _warned_no_secret:
            print(
                "[whatsapp] WARNING: WHATSAPP_APP_SECRET not set — webhook signature "
                "verification is disabled. Anyone who finds your callback URL can "
                "inject fake messages. Set WHATSAPP_APP_SECRET (App Dashboard → "
                "Settings → Basic) to enable it."
            )
            _warned_no_secret = True
        return True
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/whatsapp/callback")
async def whatsapp_verify(
    hub_mode:         str = Query("", alias="hub.mode"),
    hub_challenge:    str = Query("", alias="hub.challenge"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
):
    """Meta webhook URL verification."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return Response(hub_challenge, media_type="text/plain")
    return Response("Forbidden", status_code=403)


@router.post("/whatsapp/callback")
async def whatsapp_receive(request: Request):
    """Receive incoming WhatsApp messages."""
    raw_body = await request.body()
    if not _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        return Response("signature error", status_code=403)

    try:
        data = json.loads(raw_body)
    except Exception:
        return Response("ok")

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "statuses" in value and "messages" not in value:
            return Response("ok")
        msg     = value["messages"][0]
        phone   = msg["from"]
        content = msg.get("text", {}).get("body", "").strip()
        if not content:
            return Response("ok")

        dispatch.handle_incoming(
            channel_label="WhatsApp",
            notify_channel=f"whatsapp:{phone}",
            session_id=f"whatsapp_{phone}",
            sender_id=phone,
            content=content,
            send=lambda msg: send_text(phone, msg),
        )
    except (KeyError, IndexError):
        pass

    return Response("ok")

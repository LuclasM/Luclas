"""
adapters/whatsapp.py — Meta WhatsApp Business Cloud API adapter

Flow:
  Meta → GET /whatsapp/callback  (webhook verification)
  Meta → POST /whatsapp/callback (incoming messages)
       → respond 200 immediately
       → process in background thread
       → poll Luclas API for result
       → send result back via Graph API
"""
from __future__ import annotations

import os
import threading
import time

import requests
from fastapi import APIRouter, Query, Request, Response

router = APIRouter()

PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
LUC_API_BASE    = os.environ.get("LUC_API_BASE", "http://localhost:8080")
LUC_API_KEY     = os.environ.get("LUC_API_KEY", "")

_GRAPH_URL = "https://graph.facebook.com/v19.0/{phone_number_id}/messages"


def send_text(phone: str, content: str) -> None:
    """Send a text message to a WhatsApp number."""
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return
    requests.post(
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


# ---------------------------------------------------------------------------
# Background task: submit → poll → reply
# ---------------------------------------------------------------------------

def _run_command_and_reply(phone: str, line: str) -> None:
    headers = {"X-API-Key": LUC_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{LUC_API_BASE}/command",
            json={"line": line},
            headers=headers,
            timeout=15,
        ).json()
        send_text(phone, r.get("output", "✅ Done"))
    except Exception as e:
        send_text(phone, f"❌ Command failed: {e}")


def _process_and_reply(phone: str, message: str) -> None:
    headers = {"X-API-Key": LUC_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{LUC_API_BASE}/chat",
            json={"message": message, "session_id": f"whatsapp_{phone}"},
            headers=headers,
            timeout=10,
        ).json()
        task_id = r["task_id"]
    except Exception as e:
        send_text(phone, f"❌ Failed to submit: {e}")
        return

    for _ in range(150):
        time.sleep(2)
        try:
            res = requests.get(
                f"{LUC_API_BASE}/result/{task_id}",
                headers=headers,
                timeout=10,
            ).json()
        except Exception:
            continue
        if res["status"] == "done":
            send_text(phone, res["result"] or "✅ Done")
            return
        if res["status"] == "failed":
            send_text(phone, f"❌ Task failed: {res.get('result', '')}")
            return

    send_text(phone, "⏱ Task timed out")


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
    try:
        data = await request.json()
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

        if content.startswith("/"):
            threading.Thread(
                target=_run_command_and_reply,
                args=(phone, content),
                daemon=True,
            ).start()
        else:
            send_text(phone, "⏳ Processing…")
            contexted = (
                f"[WhatsApp user {phone}, "
                f"set notify_channel=whatsapp:{phone} for scheduled tasks] {content}"
            )
            threading.Thread(
                target=_process_and_reply,
                args=(phone, contexted),
                daemon=True,
            ).start()
    except (KeyError, IndexError):
        pass

    return Response("ok")

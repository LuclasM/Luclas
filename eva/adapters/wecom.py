"""
adapters/wecom.py — 企业微信消息接收与回复

流程：
  企微 → POST /wecom/callback（XML+AES）
       → 立即回复"处理中"
       → 后台提交 EVA4 API
       → 轮询结果
       → 主动推送结果给用户
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from Crypto.Cipher import AES
from fastapi import APIRouter, Query, Request, Response

router = APIRouter()

# ---------------------------------------------------------------------------
# 配置（从环境变量读，.env 已加载）
# ---------------------------------------------------------------------------
CORP_ID          = os.environ.get("WECOM_CORP_ID", "")
AGENT_ID         = os.environ.get("WECOM_AGENT_ID", "")
SECRET           = os.environ.get("WECOM_SECRET", "")
TOKEN            = os.environ.get("WECOM_TOKEN", "")
ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")
EVA_API_BASE     = os.environ.get("EVA_API_BASE", "http://localhost:8080")
EVA_API_KEY      = os.environ.get("EVA_API_KEY", "")

# ---------------------------------------------------------------------------
# Access token 缓存
# ---------------------------------------------------------------------------
_token_cache: dict = {"token": "", "expires_at": 0}
_token_lock = threading.Lock()


def _get_access_token() -> str:
    with _token_lock:
        if time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        r = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": CORP_ID, "corpsecret": SECRET},
            timeout=10,
        ).json()
        _token_cache["token"]      = r["access_token"]
        _token_cache["expires_at"] = time.time() + r["expires_in"]
        return _token_cache["token"]


# ---------------------------------------------------------------------------
# 消息加解密（企微 AES-256-CBC，PKCS7）
# ---------------------------------------------------------------------------

def _aes_key() -> bytes:
    return base64.b64decode(ENCODING_AES_KEY + "=")


def _decrypt(encrypt_b64: str) -> str:
    data    = base64.b64decode(encrypt_b64)
    cipher  = AES.new(_aes_key(), AES.MODE_CBC, data[:16])
    plain   = cipher.decrypt(data[16:])
    pad     = plain[-1]
    plain   = plain[:-pad]
    # 企微加密格式：msg_len(4B) + msg + corp_id（无随机前缀）
    msg_len = struct.unpack(">I", plain[0:4])[0]
    return plain[4 : 4 + msg_len].decode("utf-8")


def _verify_signature(signature: str, timestamp: str, nonce: str, echostr_or_encrypt: str) -> bool:
    items = sorted([TOKEN, timestamp, nonce, echostr_or_encrypt])
    computed = hashlib.sha1("".join(items).encode("utf-8")).hexdigest()
    return computed == signature


# ---------------------------------------------------------------------------
# 发送消息给用户
# ---------------------------------------------------------------------------

def _send_text(user_id: str, content: str) -> None:
    token = _get_access_token()
    requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/message/send",
        params={"access_token": token},
        json={
            "touser":  user_id,
            "msgtype": "text",
            "agentid": int(AGENT_ID),
            "text":    {"content": content},
        },
        timeout=10,
    )


# ---------------------------------------------------------------------------
# 后台任务：提交 → 轮询 → 回复
# ---------------------------------------------------------------------------

def _run_command_and_reply(user_id: str, line: str) -> None:
    headers = {"X-API-Key": EVA_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{EVA_API_BASE}/command",
            json={"line": line},
            headers=headers,
            timeout=15,
        ).json()
        _send_text(user_id, r.get("output", "✅ 完成"))
    except Exception as e:
        _send_text(user_id, f"❌ 命令执行失败：{e}")


def _process_and_reply(user_id: str, message: str) -> None:
    headers = {"X-API-Key": EVA_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{EVA_API_BASE}/chat",
            json={"message": message, "session_id": f"wecom_{user_id}"},
            headers=headers,
            timeout=10,
        ).json()
        task_id = r["task_id"]
    except Exception as e:
        _send_text(user_id, f"❌ 提交任务失败：{e}")
        return

    # 轮询结果，最多等 5 分钟
    for _ in range(150):
        time.sleep(2)
        try:
            res = requests.get(
                f"{EVA_API_BASE}/result/{task_id}",
                headers=headers,
                timeout=10,
            ).json()
        except Exception:
            continue
        if res["status"] == "done":
            _send_text(user_id, res["result"] or "✅ 完成")
            return
        if res["status"] == "failed":
            _send_text(user_id, f"❌ 任务失败：{res.get('result','')}")
            return

    _send_text(user_id, "⏱ 任务超时，请稍后用 /status 查询")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/wecom/callback")
async def wecom_verify(
    msg_signature: str = Query(""),
    timestamp:     str = Query(""),
    nonce:         str = Query(""),
    echostr:       str = Query(""),
):
    """企微回调 URL 验证"""
    if not _verify_signature(msg_signature, timestamp, nonce, echostr):
        return Response("signature error", status_code=403)
    plain = _decrypt(echostr)
    return Response(plain, media_type="text/plain")


@router.post("/wecom/callback")
async def wecom_receive(
    request:       Request,
    msg_signature: str = Query(""),
    timestamp:     str = Query(""),
    nonce:         str = Query(""),
):
    """接收企微用户消息"""
    body = await request.body()
    try:
        root    = ET.fromstring(body)
        encrypt = root.findtext("Encrypt", "")
    except Exception:
        return Response("xml error", status_code=400)

    if not _verify_signature(msg_signature, timestamp, nonce, encrypt):
        return Response("signature error", status_code=403)

    plain   = _decrypt(encrypt)
    msg_xml = ET.fromstring(plain)

    msg_type = msg_xml.findtext("MsgType", "")
    user_id  = msg_xml.findtext("FromUserName", "")

    if msg_type == "text":
        content = msg_xml.findtext("Content", "").strip()
        if content.startswith("/"):
            threading.Thread(
                target=_run_command_and_reply,
                args=(user_id, content),
                daemon=True,
            ).start()
        else:
            # 立即回复"处理中"，避免企微超时重试
            _send_text(user_id, "⏳ 收到，处理中…")
            # 注入用户上下文，让 LLM 设置正确的 notify_channel
            contexted = f"[来自企业微信用户 {user_id}，如需创建定时任务请设 notify_channel=wecom:{user_id}] {content}"
            threading.Thread(
                target=_process_and_reply,
                args=(user_id, contexted),
                daemon=True,
            ).start()

    # 企微要求返回空字符串表示已收到
    return Response("", media_type="text/plain")

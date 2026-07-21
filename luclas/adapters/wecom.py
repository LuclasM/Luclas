"""
adapters/wecom.py — 企业微信消息接收与回复

流程：
  企微 → POST /wecom/callback（XML+AES）
       → 立即回复"处理中"
       → 后台提交 Luclas API
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

import requests
from Crypto.Cipher import AES
from fastapi import APIRouter, Query, Request, Response

from adapters import dispatch

router = APIRouter()

# ---------------------------------------------------------------------------
# 配置（从环境变量读，.env 已加载）
# ---------------------------------------------------------------------------
CORP_ID          = os.environ.get("WECOM_CORP_ID", "")
AGENT_ID         = os.environ.get("WECOM_AGENT_ID", "")
SECRET           = os.environ.get("WECOM_SECRET", "")
TOKEN            = os.environ.get("WECOM_TOKEN", "")
ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")

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
        if "access_token" not in r:
            raise RuntimeError(f"WeChat token error: {r.get('errmsg', r)}")
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
    if not CORP_ID or not SECRET or not AGENT_ID:
        return
    token = _get_access_token()
    try:
        dispatch.post_with_retry(
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
    except Exception as e:
        print(f"[wecom] failed to deliver message to {user_id} after retries: {e}")


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
        msg_id  = msg_xml.findtext("MsgId", "")
        dispatch.handle_incoming(
            channel_label="WeCom",
            notify_channel=f"wecom:{user_id}",
            session_id=f"wecom_{user_id}",
            sender_id=user_id,
            content=content,
            send=lambda msg: _send_text(user_id, msg),
            message_id=msg_id,
        )

    # 企微要求返回空字符串表示已收到
    return Response("", media_type="text/plain")

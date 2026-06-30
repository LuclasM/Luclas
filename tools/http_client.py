"""
tools/http_client.py — HTTP 请求（调用 REST API）
"""

import json
import requests

HTTP_REQUEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "http_request",
        "description": (
            "Send an HTTP request, supporting GET/POST/PUT/DELETE and other methods. "
            "Useful for calling REST APIs, submitting forms, and interacting with external services. "
            "Returns status code, response headers, and response body."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url":     {"type": "string", "description": "Request URL"},
                "method":  {"type": "string", "description": "HTTP method: GET POST PUT DELETE PATCH, default GET", "default": "GET"},
                "headers": {"type": "object", "description": "Request headers dict, optional"},
                "params":  {"type": "object", "description": "URL query parameters, optional"},
                "body":    {"type": "object", "description": "Request body (JSON), optional"},
                "body_raw":{"type": "string", "description": "Raw request body string (alternative to body)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default 15", "default": 15},
            },
            "required": ["url"],
        },
    },
}


def http_request(url: str, method: str = "GET", headers: dict = None,
                 params: dict = None, body: dict = None,
                 body_raw: str = None, timeout: int = 15) -> dict:
    method = method.upper()
    req_headers = headers or {}

    try:
        if body is not None:
            resp = requests.request(
                method, url, headers=req_headers,
                params=params, json=body, timeout=timeout
            )
        elif body_raw is not None:
            resp = requests.request(
                method, url, headers=req_headers,
                params=params, data=body_raw, timeout=timeout
            )
        else:
            resp = requests.request(
                method, url, headers=req_headers,
                params=params, timeout=timeout
            )

        # 尝试解析 JSON，失败则返回文本
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text[:8000]

        return {
            "status":   resp.status_code,
            "headers":  dict(resp.headers),
            "body":     resp_body,
            "ok":       resp.ok,
        }
    except requests.exceptions.Timeout:
        return {"status": -1, "error": f"Request timed out (>{timeout}s)", "ok": False}
    except requests.exceptions.ConnectionError as e:
        return {"status": -1, "error": f"Connection failed: {e}", "ok": False}
    except Exception as e:
        return {"status": -1, "error": str(e), "ok": False}

import json
import time
import requests
from config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY


class LLMClient:

    def __init__(self, base_url=None, model=None):
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model    = model or LLM_MODEL
        self._headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type":  "application/json",
        }

    def chat(self, messages: list, temperature=0.7) -> str:
        payload = {
            "model":    self.model,
            "messages": messages,
            "stream":   False,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = self._post(payload)
        return resp["choices"][0]["message"]["content"] or ""

    def chat_json(self, messages: list, temperature=0.1) -> dict:
        raw = self.chat(messages, temperature=temperature).strip()
        for start_ch, end_ch in [('{', '}'), ('[', ']')]:
            s = raw.find(start_ch)
            e = raw.rfind(end_ch) + 1
            if s >= 0 and e > s:
                try:
                    return json.loads(raw[s:e])
                except json.JSONDecodeError:
                    pass
        return {}

    def agent_turn(self, messages: list, tools: list) -> dict:
        payload = {
            "model":       self.model,
            "messages":    messages,
            "stream":      False,
            "tools":       tools,
            "tool_choice": "auto",
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = self._post(payload)
        msg = resp["choices"][0]["message"]
        return {
            "content":    msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "raw":        msg,
        }

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/models",
                             headers=self._headers, timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _post(self, payload: dict, retries: int = 3, retry_delay: float = 2.0) -> dict:
        last_err: RuntimeError | None = None
        for attempt in range(retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                    timeout=300,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError:
                last_err = RuntimeError(f"Could not connect to LLM service ({self.base_url})")
            except requests.exceptions.Timeout:
                last_err = RuntimeError("LLM response timed out")
            except requests.exceptions.HTTPError as e:
                # HTTP 4xx/5xx 不重试
                raise RuntimeError(f"LLM request failed: {e.response.status_code} — {e.response.text[:300]}")
            if attempt < retries - 1:
                time.sleep(retry_delay)
        raise last_err

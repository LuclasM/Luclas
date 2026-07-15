import json
import time
import requests
from config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY


class LLMClient:

    def __init__(self, base_url=None, model=None, router=None):
        self._default_base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self._default_model    = model or LLM_MODEL
        self._default_api_key  = LLM_API_KEY
        self._router           = router
        self._model_queue: list = []   # list[ModelConfig] populated by set_goal()
        self._current_idx: int  = 0

    # ── Routing API ──────────────────────────────────────────────────────────

    def reload_router(self) -> None:
        """Re-read models.json and rebuild the router in place."""
        from llm_router import ModelRouter, load_models
        from config import MODELS_CONFIG_PATH
        models = load_models(MODELS_CONFIG_PATH)
        if models:
            self._router = ModelRouter(models)
            print(f"[router] reloaded {len(models)} model(s)")
        else:
            self._router = None
        self._model_queue = []
        self._current_idx = 0

    def set_goal(self, goal: str) -> None:
        """Classify the goal and build the ordered model queue for this task."""
        if self._router:
            self._model_queue = self._router.select_models(goal)
            self._current_idx = 0
            if self._model_queue:
                m = self._model_queue[0]
                print(f"[router] → {m.name}  (queue: {len(self._model_queue)} model(s))")
        else:
            self._model_queue = []
            self._current_idx = 0

    def clone(self) -> "LLMClient":
        """A fresh client sharing the (read-only) router but with its own
        mutable model-queue/escalation state — required so concurrently
        running delegate branches don't stomp on each other's set_goal()/
        escalate() calls when they share the same base LLMClient instance.
        """
        c = LLMClient(base_url=self._default_base_url, model=self._default_model,
                      router=self._router)
        c._default_api_key = self._default_api_key
        return c

    def escalate(self) -> bool:
        """Switch to the next model in the queue. Returns True if a next model exists."""
        if self._model_queue and self._current_idx < len(self._model_queue) - 1:
            self._current_idx += 1
            m = self._model_queue[self._current_idx]
            print(f"[router] escalated → {m.name}")
            return True
        return False

    # ── Active model properties ──────────────────────────────────────────────

    @property
    def _active(self):
        if self._model_queue and self._current_idx < len(self._model_queue):
            return self._model_queue[self._current_idx]
        return None

    @property
    def base_url(self) -> str:
        return self._active.base_url if self._active else self._default_base_url

    @property
    def model(self) -> str:
        return self._active.name if self._active else self._default_model

    @property
    def _headers(self) -> dict:
        api_key = self._active.api_key if self._active else self._default_api_key
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }

    # ── LLM calls ────────────────────────────────────────────────────────────

    def chat(self, messages: list, temperature=0.7, **kwargs) -> str:
        payload = {
            "model":       self.model,
            "messages":    messages,
            "stream":      False,
            "temperature": temperature,
        }
        if "qwen" in self.model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload.update(kwargs)
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
        }
        if "qwen" in self.model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        resp = self._post(payload)
        msg = resp["choices"][0]["message"]
        return {
            "content":    msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "raw":        msg,
        }

    def is_available(self) -> bool:
        try:
            # 5s was too tight for LAN/remote deployments: chat completions get a
            # 300s timeout and go through fine over the same path, but this cold
            # health check on process startup would occasionally miss a 5s window
            # and print "offline" even though the server was reachable and working.
            r = requests.get(f"{self.base_url}/models",
                             headers=self._headers, timeout=15)
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
                code = e.response.status_code
                raise RuntimeError(f"LLM request failed: {code} — {e.response.text[:300]}")
            if attempt < retries - 1:
                time.sleep(retry_delay)
        raise last_err

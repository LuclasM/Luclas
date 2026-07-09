"""
llm_router.py — Multi-model routing with LLM-based task classification.

models.json schema (list of objects):
  {
    "id":         "local-strong",
    "name":       "qwen3.6-27b-awq-int4",
    "base_url":   "http://localhost:8003/v1",
    "api_key":    "none",
    "priority":   1,              # lower = higher priority within same complexity
    "complexity": "high",         # "low" | "mid" | "high"
    "task_types": ["general", "legal", "analysis"],
    "classifier": true            # optional — designates this as the classification model
  }

Selection order:
  1. Filter by task_type (or "general" as wildcard)
  2. Start at classified complexity, escalate upward on failure
  3. Within same complexity tier, sort by priority
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import requests


_COMPLEXITY_LEVELS = ["low", "mid", "high"]

TASK_TYPES = [
    "general",        # catch-all
    "legal",          # mandamus documents, affidavits, IR forms
    "data_extraction",# JX_CRM access, file download, OCR
    "coding",         # python scripts, shell automation
    "analysis",       # multi-source synthesis, report writing
    "reflection",     # nightly self-reflection tasks
]


@dataclass
class ModelConfig:
    id:         str
    name:       str
    base_url:   str
    api_key:    str
    priority:   int
    complexity: str                      # "low" | "mid" | "high"
    task_types: list[str] = field(default_factory=lambda: ["general"])
    classifier: bool = False             # use this model for task classification

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }


def load_models(path: str) -> list[ModelConfig]:
    """Load model configs from a JSON file. Returns [] if file absent or invalid."""
    if not os.path.isfile(path):
        return []
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
        models = []
        for item in raw:
            models.append(ModelConfig(
                id         = item["id"],
                name       = item["name"],
                base_url   = item["base_url"].rstrip("/"),
                api_key    = item.get("api_key", "none"),
                priority   = int(item.get("priority", 1)),
                complexity = item.get("complexity", "mid"),
                task_types = item.get("task_types", ["general"]),
                classifier = bool(item.get("classifier", False)),
            ))
        return models
    except Exception as e:
        print(f"[router] Failed to load models.json: {e}")
        return []


class ModelRouter:

    def __init__(self, models: list[ModelConfig]) -> None:
        self.models = models
        self._classifier: ModelConfig | None = self._pick_classifier()

    # ── Public API ────────────────────────────────────────────────────────────

    def select_models(self, goal: str) -> list[ModelConfig]:
        """
        Return an ordered list of ModelConfig objects to try for this goal.
        Start at the classified complexity level, escalate upward on failure.
        """
        task_type, complexity = self._classify(goal)
        return self._ordered(task_type, complexity)

    # ── Classification ────────────────────────────────────────────────────────

    def _classify(self, goal: str) -> tuple[str, str]:
        """Ask the classifier model to categorise the goal. Falls back to (general, mid)."""
        if self._classifier is None:
            return "general", "mid"

        prompt = [{
            "role": "user",
            "content": (
                f'Classify this task (one line of JSON, nothing else):\n\n"{goal[:600]}"\n\n'
                f'task_type must be one of: {TASK_TYPES}\n'
                f'complexity must be one of: low, mid, high\n\n'
                f'low  = single-step, simple lookup or direct action\n'
                f'mid  = multi-step, requires planning or moderate reasoning\n'
                f'high = complex analysis, long document generation, multi-source synthesis\n\n'
                f'Return exactly: {{"task_type": "...", "complexity": "..."}}'
            ),
        }]
        try:
            payload = {
                "model":       self._classifier.name,
                "messages":    prompt,
                "temperature": 0.0,
                "stream":      False,
            }
            # Suppress thinking tokens on local Qwen models so JSON extraction is clean.
            if "qwen" in self._classifier.name.lower():
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            resp = requests.post(
                f"{self._classifier.base_url}/chat/completions",
                headers=self._classifier.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"] or ""
            # Use non-nested brace match to avoid capturing thinking-text fragments.
            for m in re.finditer(r'\{[^{}]*\}', content):
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    continue
                if "task_type" not in data and "complexity" not in data:
                    continue
                task_type  = data.get("task_type",  "general")
                complexity = data.get("complexity", "mid")
                if task_type  not in TASK_TYPES:          task_type  = "general"
                if complexity not in _COMPLEXITY_LEVELS:  complexity = "mid"
                return task_type, complexity
        except Exception as e:
            print(f"[router] classification failed: {e}")
        return "general", "mid"

    # ── Selection / ordering ──────────────────────────────────────────────────

    def _ordered(self, task_type: str, complexity: str) -> list[ModelConfig]:
        """
        Filter by task_type, then order by complexity tier (start at target,
        escalate upward), then by priority within each tier.
        """
        candidates = [
            m for m in self.models
            if task_type in m.task_types or "general" in m.task_types
        ]
        if not candidates:
            candidates = list(self.models)

        start_idx = _COMPLEXITY_LEVELS.index(complexity) if complexity in _COMPLEXITY_LEVELS else 1
        # Escalate upward from start_idx; if no higher tier exists, fall back in descending order.
        # e.g. low→ [low, mid, high]  mid→ [mid, high, low]  high→ [high, mid, low]
        tier_order = _COMPLEXITY_LEVELS[start_idx:] + list(reversed(_COMPLEXITY_LEVELS[:start_idx]))

        return sorted(candidates, key=lambda m: (
            tier_order.index(m.complexity) if m.complexity in tier_order else 99,
            m.priority,
        ))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pick_classifier(self) -> ModelConfig | None:
        explicit = [m for m in self.models if m.classifier]
        if explicit:
            return explicit[0]
        # Fall back: first mid-complexity model, then any
        mids = [m for m in self.models if m.complexity == "mid"]
        return mids[0] if mids else (self.models[0] if self.models else None)

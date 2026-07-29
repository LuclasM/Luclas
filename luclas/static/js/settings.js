// settings.js — .env form (general/wecom/whatsapp/discord) + models.json CRUD.

(() => {
  const GROUPS = {
    "env-general": ["LUC_LANG", "LUC_LLM_BASE_URL", "LUC_LLM_MODEL", "LUC_LLM_API_KEY",
                     "LUC_EMBED_MODEL", "LUC_API_KEY", "LUC_API_PORT",
                     "LUC_ADMIN_NOTIFY", "LUC_DAILY_TOKEN_BUDGET"],
    "env-wecom": ["WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN", "WECOM_ENCODING_AES_KEY"],
    "env-whatsapp": ["WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN"],
    "env-discord": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
  };

  const modelsBody = document.querySelector("#models-table tbody");
  const detectResult = document.getElementById("models-detect-result");

  async function renderEnv() {
    const env = await Api.get("/api/settings/env");
    for (const [groupId, keys] of Object.entries(GROUPS)) {
      const el = document.getElementById(groupId);
      el.innerHTML = keys.map((key) => {
        const info = env[key] || {};
        if (info.secret) {
          const note = info.configured ? `<span class="configured-note">已配置(留空则不修改)</span>` : `<span class="muted small">未配置</span>`;
          return `<div class="field"><label>${key}</label>
            <input type="password" data-key="${key}" placeholder="输入新值以修改…" autocomplete="off">
            ${note}</div>`;
        }
        return `<div class="field"><label>${key}</label>
          <input type="text" data-key="${key}" value="${(info.value || "").replace(/"/g, "&quot;")}"></div>`;
      }).join("");
    }
  }

  document.getElementById("env-save-btn").addEventListener("click", async () => {
    const statusEl = document.getElementById("env-save-status");
    const values = {};
    document.querySelectorAll(Object.keys(GROUPS).map((id) => `#${id} input[data-key]`).join(",")).forEach((input) => {
      if (input.type === "password") {
        if (input.value.trim()) values[input.dataset.key] = input.value.trim();
      } else {
        values[input.dataset.key] = input.value;
      }
    });
    try {
      const r = await Api.post("/api/settings/env", { values });
      statusEl.textContent = r.restart_required
        ? "已保存,需要重启 API 服务才会生效(系统管理页有重启按钮)"
        : "已保存";
      renderEnv();
    } catch (e) {
      statusEl.textContent = "保存失败: " + e.message;
    }
  });

  function complexityLabel(c) {
    return { low: "低", mid: "中", high: "高" }[c] || c;
  }

  async function renderModels() {
    const models = await Api.get("/api/settings/models");
    modelsBody.innerHTML = models.map((m) => `
      <tr data-id="${m.id}">
        <td>${m.classifier ? "◉" : "○"}</td>
        <td>${m.id}</td>
        <td>${escapeHtml(m.name)}</td>
        <td class="muted small">${escapeHtml(m.base_url)}</td>
        <td>${complexityLabel(m.complexity)}</td>
        <td>${m.priority}</td>
        <td><button class="btn btn-small" data-action="delete">删除</button></td>
      </tr>`).join("") || `<tr><td colspan="7" class="muted">暂无模型配置</td></tr>`;
  }

  modelsBody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.dataset.action !== "delete") return;
    const id = btn.closest("tr").dataset.id;
    if (!confirm(`删除模型 ${id}?`)) return;
    await Api.del(`/api/settings/models/${id}`);
    renderModels();
  });

  document.getElementById("models-add-btn").addEventListener("click", async () => {
    const values = await Modal.form("新增模型", [
      { name: "id", label: "ID", type: "text", placeholder: "local-mid" },
      { name: "name", label: "名称", type: "text" },
      { name: "base_url", label: "Base URL", type: "text", placeholder: "http://localhost:8003/v1" },
      { name: "api_key", label: "API Key", type: "text", placeholder: "none" },
      { name: "complexity", label: "复杂度", type: "select", options: ["low", "mid", "high"] },
      { name: "priority", label: "优先级", type: "text", placeholder: "1" },
    ]);
    if (!values) return;
    try {
      await Api.post("/api/settings/models", {
        id: values.id, name: values.name, base_url: values.base_url,
        api_key: values.api_key || "none", complexity: values.complexity || "mid",
        priority: parseInt(values.priority, 10) || 1,
      });
      renderModels();
    } catch (e) {
      alert("创建失败: " + e.message);
    }
  });

  document.getElementById("models-detect-btn").addEventListener("click", async () => {
    detectResult.innerHTML = `<p class="muted small">探测中…</p>`;
    const found = await Api.get("/api/settings/models/detect");
    if (!found.length) {
      detectResult.innerHTML = `<p class="muted small">没有探测到本地运行的 LLM 服务(Ollama / LM Studio / vLLM)</p>`;
      return;
    }
    detectResult.innerHTML = found.map((f) => `
      <div class="panel" style="margin-top:10px">
        <b>${f.provider}</b> — <span class="muted small">${escapeHtml(f.base_url)}</span>
        <div class="muted small">${(f.models || []).join(", ")}</div>
      </div>`).join("");
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  Router.onEnter("settings", () => {
    renderEnv();
    renderModels();
  });
})();

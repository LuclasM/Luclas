// system.js — status cards, scheduled tasks, core.md viewer, restart button.

(() => {
  const statusCards = document.getElementById("status-cards");
  const scheduleBody = document.querySelector("#schedule-table tbody");
  const coreContent = document.getElementById("core-content");
  const coreHistorySelect = document.getElementById("core-history-select");
  const restartBtn = document.getElementById("restart-btn");
  const restartStatus = document.getElementById("restart-status");
  const connDot = document.getElementById("conn-dot");
  const connLabel = document.getElementById("conn-label");

  function card(label, value, ok) {
    const cls = ok === undefined ? "" : (ok ? "ok" : "bad");
    return `<div class="card"><div class="card-label">${label}</div>
      <div class="card-value ${cls}">${value}</div></div>`;
  }

  async function refreshStatus() {
    try {
      const s = await Api.get("/status");
      connDot.className = "conn-dot online";
      connLabel.textContent = "在线";
      const ch = s.channels || {};
      const chLabel = (name) => {
        const c = ch[name];
        if (!c || !c.configured) return `${name}: 未配置`;
        return `${name}: ${c.healthy === false ? "异常" : "正常"}`;
      };
      statusCards.innerHTML = [
        card("LLM", s.llm === "online" ? "在线" : "离线", s.llm === "online"),
        card("模型", s.model || "-"),
        card("记忆条数", s.memory_count ?? "-"),
        card("进行中任务", s.pending ?? 0),
        card("24h Token", (s.token_usage_24h && s.token_usage_24h.total_tokens) ?? 0),
        card("渠道", ["wecom", "whatsapp", "discord"].map(chLabel).join(" · ")),
      ].join("");
    } catch (e) {
      connDot.className = "conn-dot offline";
      connLabel.textContent = "离线";
    }
  }

  async function refreshSchedule() {
    const tasks = await Api.get("/api/system/schedule");
    scheduleBody.innerHTML = tasks.map((t) => `
      <tr data-id="${t.id}">
        <td><span class="badge ${t.enabled ? "on" : "off"}">${t.enabled ? "开" : "关"}</span></td>
        <td>${escapeHtml(t.name)}</td>
        <td>${t.schedule_type === "weekly" ? t.schedule_day + " " : ""}${t.schedule_time}</td>
        <td class="muted small">${escapeHtml((t.goal || "").slice(0, 60))}</td>
        <td class="small">${escapeHtml(t.notify_channel || "")}</td>
        <td class="small muted">${t.last_run || "从未"}</td>
        <td>
          <button class="btn btn-small" data-action="toggle">${t.enabled ? "关闭" : "开启"}</button>
          <button class="btn btn-small" data-action="delete">删除</button>
        </td>
      </tr>`).join("") || `<tr><td colspan="7" class="muted">暂无定时任务</td></tr>`;
  }

  scheduleBody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("tr");
    const id = row.dataset.id;
    if (btn.dataset.action === "delete") {
      if (!confirm("确定删除这个定时任务?")) return;
      await Api.del(`/api/system/schedule/${id}`);
    } else if (btn.dataset.action === "toggle") {
      const enabled = row.querySelector(".badge").classList.contains("off");
      await Api.patch(`/api/system/schedule/${id}`, { enabled });
    }
    refreshSchedule();
  });

  document.getElementById("schedule-add-btn").addEventListener("click", async () => {
    const values = await Modal.form("新建定时任务", [
      { name: "name", label: "名称", type: "text" },
      { name: "goal", label: "目标(交给 agent 执行的任务描述)", type: "text" },
      { name: "schedule_type", label: "频率", type: "select", options: ["daily", "weekly"] },
      { name: "schedule_day", label: "星期(仅 weekly 需要)", type: "select",
        options: ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"] },
      { name: "schedule_time", label: "时间 (HH:MM)", type: "text", placeholder: "09:00" },
      { name: "notify_channel", label: "通知渠道", type: "text", placeholder: "terminal / wecom:user_id" },
    ]);
    if (!values) return;
    try {
      await Api.post("/api/system/schedule", values);
      refreshSchedule();
    } catch (e) {
      alert("创建失败: " + e.message);
    }
  });

  async function refreshCoreHistory() {
    const snaps = await Api.get("/api/system/core/history");
    coreHistorySelect.innerHTML = `<option value="">当前版本</option>` +
      snaps.map((s) => `<option value="${s.name}">${s.name} — ${escapeHtml(s.reason || "")}</option>`).join("");
  }

  async function loadCore(name) {
    if (!name) {
      const r = await Api.get("/api/system/core");
      coreContent.textContent = r.content || "(未设置)";
    } else {
      const r = await Api.get(`/api/system/core/history/${encodeURIComponent(name)}`);
      coreContent.textContent = r.content;
    }
  }

  coreHistorySelect.addEventListener("change", () => loadCore(coreHistorySelect.value));

  restartBtn.addEventListener("click", async () => {
    if (!confirm("重启 API 服务会短暂中断所有对话连接,确定吗?")) return;
    restartStatus.textContent = "重启中…";
    try {
      const r = await Api.post("/api/system/restart");
      restartStatus.textContent = r.detail || "已重启";
    } catch (e) {
      restartStatus.textContent = "失败: " + e.message;
    }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  Router.onEnter("system", () => {
    refreshStatus();
    refreshSchedule();
    refreshCoreHistory().then(() => loadCore(""));
  });

  document.addEventListener("luc:key-ready", refreshStatus);
  setInterval(() => { if (Api.getKey()) refreshStatus(); }, 30000);
})();

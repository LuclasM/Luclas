// chat.js — chat view: POST /chat to send, SSE (/sse/chat) to receive.
// session_id lives in sessionStorage (not localStorage) so each new tab is
// a fresh conversation by default; the same id can be copied into another
// tab manually to share one conversation.

const Chat = (() => {
  const SESSION_STORAGE_KEY = "luc_web_session_id";
  const messagesEl = document.getElementById("chat-messages");
  const formEl = document.getElementById("chat-form");
  const inputEl = document.getElementById("chat-input");
  let source = null;
  let historyLoaded = false;
  // Seq of the newest assistant push already accounted for — starts at 0
  // (replay everything) but loadHistory() moves it forward past whatever
  // GET /chat/history already rendered, so connectStream()'s first SSE
  // connect doesn't re-append those same replies from the backlog.
  let lastSeq = 0;

  function sessionId() {
    let id = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!id) {
      id = "web_" + Math.random().toString(16).slice(2) + Date.now().toString(16);
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
    }
    return id;
  }

  function appendMessage(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  // Persisted conversation history (ConversationStore, keyed through
  // identity resolution same as a live turn) — this is the source of truth
  // for anything already said; the SSE backlog below is only for messages
  // pushed after this loaded (see lastSeq/`since`). Guarded by historyLoaded
  // so a second trigger (key-ready firing after Router.onEnter already ran,
  // or vice versa) doesn't render everything twice.
  async function loadHistory() {
    if (historyLoaded) return;
    historyLoaded = true;
    try {
      const data = await Api.get(`/chat/history?session_id=${encodeURIComponent(sessionId())}`);
      (data.messages || []).forEach((m) => appendMessage(m.role, m.content));
      lastSeq = data.last_seq || 0;
    } catch (e) {
      historyLoaded = false; // let the next trigger retry instead of leaving history blank forever
    }
  }

  function connectStream() {
    // On a first-ever visit the key modal is still showing and Api.getKey()
    // is still empty at this point (DOMContentLoaded's initial show("chat")
    // fires before the user has had a chance to type anything in) — skip
    // connecting with a key we already know is blank rather than burning a
    // guaranteed-401 round trip; luc:key-ready fires this again once a key
    // is actually submitted.
    if (!Api.getKey()) return;
    if (source) source.close();
    const url = `/sse/chat?session_id=${encodeURIComponent(sessionId())}&key=${encodeURIComponent(Api.getKey())}&since=${lastSeq}`;
    source = new EventSource(url);
    source.onmessage = (e) => {
      appendMessage("assistant", e.data);
      // Keep pace with the stream so a later manual reconnect (onerror's
      // retry below) resumes from here instead of replaying from the point
      // history left off — EventSource's own auto-reconnect already tracks
      // this via Last-Event-ID, but that manual retry builds a brand new
      // EventSource and only has whatever `since` closed over.
      if (e.lastEventId) lastSeq = parseInt(e.lastEventId, 10) || lastSeq;
    };
    source.onerror = async () => {
      // A transient network blip leaves the browser auto-retrying on its
      // own (readyState CONNECTING) — nothing to do here. CLOSED means the
      // browser has given up for good, which is exactly what a 401 (bad/
      // rotated key) produces, but EventSource never exposes the actual
      // status code. Tell them apart with a real auth check: if the key
      // turns out to be invalid, Api.get() already clears it and pops the
      // key-entry modal (see api.js) — wait for luc:key-ready to reconnect
      // instead of retrying blindly with a key we now know is bad. If the
      // key is fine, the failure was something else (e.g. the API service
      // restarting), so retry after a short delay.
      if (source.readyState !== EventSource.CLOSED) return;
      try {
        await Api.get("/status");
        setTimeout(connectStream, 3000);
      } catch (e) {
        // key was invalid — Api.get() already surfaced the key modal.
      }
    };
  }

  async function send(text) {
    appendMessage("user", text);
    try {
      await Api.post("/chat", { message: text, session_id: sessionId() });
    } catch (e) {
      appendMessage("assistant", "❌ 发送失败: " + e.message);
    }
  }

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
  }

  formEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    autoResize();
    send(text);
  });

  inputEl.addEventListener("input", autoResize);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formEl.requestSubmit();
    }
  });

  async function start() {
    if (!Api.getKey()) return;
    await loadHistory();
    connectStream();
  }

  document.addEventListener("luc:key-ready", start);
  Router.onEnter("chat", () => { if (!source) start(); });
})();

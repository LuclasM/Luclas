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

  function connectStream() {
    // On a first-ever visit the key modal is still showing and Api.getKey()
    // is still empty at this point (DOMContentLoaded's initial show("chat")
    // fires before the user has had a chance to type anything in) — skip
    // connecting with a key we already know is blank rather than burning a
    // guaranteed-401 round trip; luc:key-ready fires this again once a key
    // is actually submitted.
    if (!Api.getKey()) return;
    if (source) source.close();
    const url = `/sse/chat?session_id=${encodeURIComponent(sessionId())}&key=${encodeURIComponent(Api.getKey())}`;
    source = new EventSource(url);
    source.onmessage = (e) => appendMessage("assistant", e.data);
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

  document.addEventListener("luc:key-ready", connectStream);
  Router.onEnter("chat", () => { if (!source) connectStream(); });
})();

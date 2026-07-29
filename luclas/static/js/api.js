// api.js — fetch wrapper that attaches X-API-Key, and the key-entry modal.

const Api = (() => {
  const KEY_STORAGE = "luc_api_key";

  function getKey() {
    return localStorage.getItem(KEY_STORAGE) || "";
  }

  function setKey(key) {
    localStorage.setItem(KEY_STORAGE, key);
  }

  function showKeyModal() {
    document.getElementById("key-modal").hidden = false;
  }

  function hideKeyModal() {
    document.getElementById("key-modal").hidden = true;
  }

  async function request(path, options = {}) {
    const headers = Object.assign({}, options.headers, { "X-API-Key": getKey() });
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, Object.assign({}, options, { headers }));
    if (res.status === 401) {
      localStorage.removeItem(KEY_STORAGE);
      showKeyModal();
      throw new Error("unauthorized");
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  }

  function get(path) { return request(path); }
  function post(path, body) { return request(path, { method: "POST", body: JSON.stringify(body || {}) }); }
  function patch(path, body) { return request(path, { method: "PATCH", body: JSON.stringify(body || {}) }); }
  function del(path) { return request(path, { method: "DELETE" }); }

  document.addEventListener("DOMContentLoaded", () => {
    if (!getKey()) showKeyModal();
    document.getElementById("key-submit").addEventListener("click", () => {
      const v = document.getElementById("key-input").value.trim();
      if (v) { setKey(v); hideKeyModal(); document.dispatchEvent(new Event("luc:key-ready")); }
    });
    document.getElementById("key-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") document.getElementById("key-submit").click();
    });
  });

  return { get, post, patch, del, getKey, setKey, showKeyModal, hideKeyModal };
})();

// modal.js — small generic form-in-a-modal helper, reused by System and
// Settings for "add a scheduled task" / "add a model" style forms.

const Modal = (() => {
  function form(title, fields) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML = `
        <div class="modal">
          <h2>${title}</h2>
          ${fields.map((f) => `
            <div class="field" style="margin-bottom:10px">
              <label>${f.label}</label>
              ${f.type === "select"
                ? `<select name="${f.name}">${f.options.map((o) => `<option value="${o}">${o || "(空)"}</option>`).join("")}</select>`
                : `<input name="${f.name}" type="text" placeholder="${f.placeholder || ""}">`}
            </div>`).join("")}
          <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:14px">
            <button class="btn" data-action="cancel">取消</button>
            <button class="btn btn-primary" data-action="ok">确定</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);

      function close(result) {
        overlay.remove();
        resolve(result);
      }

      overlay.querySelector('[data-action="cancel"]').addEventListener("click", () => close(null));
      overlay.querySelector('[data-action="ok"]').addEventListener("click", () => {
        const values = {};
        fields.forEach((f) => {
          values[f.name] = overlay.querySelector(`[name="${f.name}"]`).value.trim();
        });
        close(values);
      });
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(null); });
    });
  }

  return { form };
})();

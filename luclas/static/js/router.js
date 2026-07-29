// router.js — hash-based view switching (#chat / #system / #settings).
// A single page (not 3 separate HTML files) so the chat SSE connection
// survives switching to System/Settings and back.

const Router = (() => {
  const views = document.querySelectorAll(".view");
  const navItems = document.querySelectorAll(".nav-item");
  const listeners = {};

  function currentView() {
    return (location.hash || "#chat").slice(1);
  }

  function show(view) {
    views.forEach((v) => { v.hidden = v.dataset.view !== view; });
    navItems.forEach((n) => { n.classList.toggle("active", n.dataset.view === view); });
    (listeners[view] || []).forEach((fn) => fn());
  }

  function onEnter(view, fn) {
    listeners[view] = listeners[view] || [];
    listeners[view].push(fn);
  }

  window.addEventListener("hashchange", () => show(currentView()));
  document.addEventListener("DOMContentLoaded", () => show(currentView()));

  return { onEnter, currentView };
})();

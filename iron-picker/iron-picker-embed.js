(() => {
  "use strict";

  let overlay = null;
  let picker = null;

  function ensureOverlay() {
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.id = "iron-object-overlay";
    overlay.className = "iron-ms-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `
      <div class="iron-ms-scrim" data-iron-close></div>
      <div class="iron-ms-dialog" role="dialog" aria-labelledby="iron-ms-title">
        <header class="iron-ms-header">
          <h2 id="iron-ms-title">Iron Object</h2>
          <button type="button" class="iron-ms-close" data-iron-close aria-label="Close">✕</button>
        </header>
        <div class="iron-ms-body iron-picker-root"></div>
      </div>
    `;

    document.body.appendChild(overlay);

    overlay.querySelectorAll("[data-iron-close]").forEach((el) => {
      el.addEventListener("click", () => window.closeIronObjectDialog());
    });

    overlay.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") window.closeIronObjectDialog();
    });

    const root = overlay.querySelector(".iron-picker-root");
    root.innerHTML = window.IronPicker.template();
    picker = new window.IronPicker(root, {
      embedded: true,
      onClose: () => window.closeIronObjectDialog(),
    });

    return overlay;
  }

  window.openIronObjectDialog = function openIronObjectDialog() {
    const el = ensureOverlay();
    el.hidden = false;
    document.body.classList.add("iron-ms-open");
    picker.open();
    el.querySelector(".iron-ms-close")?.focus();
  };

  window.closeIronObjectDialog = function closeIronObjectDialog() {
    if (!overlay) return;
    overlay.hidden = true;
    document.body.classList.remove("iron-ms-open");
    picker?.close();
  };
})();
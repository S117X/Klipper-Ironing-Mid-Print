(() => {
  "use strict";

  const main = document.querySelector(".dialog-body");
  if (!main) return;

  const mount = document.createElement("div");
  mount.className = "iron-picker-root";
  mount.innerHTML = window.IronPicker.template();
  main.appendChild(mount);

  const picker = new window.IronPicker(mount, { embedded: false });
  const back = document.querySelector(".dialog-header [data-ip='btn-back']");
  if (back) {
    back.addEventListener("click", () => {
      if (history.length > 1) history.back();
      else location.href = "/";
    });
  }
  picker.open();
})();
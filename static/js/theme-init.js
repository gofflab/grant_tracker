// Runs before first paint so the saved theme never flashes.
(function () {
  try {
    var t = localStorage.getItem("gt-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the OS setting */ }
})();

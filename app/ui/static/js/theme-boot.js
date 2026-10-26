// Runs before the stylesheet: apply the saved theme so the page never flashes.
(function () {
  try {
    var theme = localStorage.getItem("twin.theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch (e) { /* storage blocked: follow the system theme */ }
})();

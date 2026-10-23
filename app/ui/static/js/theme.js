// Theme toggle: System → Light → Dark. "System" follows the OS (prefers-color-scheme).

const KEY = "twin.theme";
const ORDER = ["system", "light", "dark"];
const LABEL = { system: "◐ System", light: "☀ Light", dark: "☾ Dark" };

function saved() {
  try {
    const value = localStorage.getItem(KEY);
    return ORDER.includes(value) ? value : "system";
  } catch {
    return "system";
  }
}

function apply(theme, button) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch { /* not persisted; still applied for this page */ }
  button.textContent = LABEL[theme];
  button.title = `Theme: ${theme}. Click for ${ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]}.`;
}

export function setupTheme() {
  const button = document.getElementById("theme-toggle");
  let current = saved();
  apply(current, button);
  button.addEventListener("click", () => {
    current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
    apply(current, button);
  });
}

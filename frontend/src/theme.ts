// Light / dark / system theme. The choice is a per-browser convenience (localStorage); "system" follows the OS.
export type Theme = "light" | "dark" | "system";
const KEY = "abteilo.theme";

export function getTheme(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    // storage unavailable (private window): the choice lasts for this page only
  }
  applyTheme(theme);
}

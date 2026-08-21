// 主题切换：dark（默认，Ponder 夜间视觉）/ light（白天模式，
// 调色板取自概率论复习资料真品 CSS——暖米白 #faf8f5 + 墨蓝黑 #1f2430 + 陶土橙 #D97757）。
// 与语言切换同一套持久化思路：localStorage + <html data-theme>。

export const themeStorageKey = "ff-theme";
export const themes = ["dark", "light"];

export function getTheme() {
  const saved = typeof window !== "undefined" ? window.localStorage?.getItem(themeStorageKey) : null;
  return themes.includes(saved) ? saved : "dark";
}

export function applyTheme(theme, root = document) {
  const next = themes.includes(theme) ? theme : "dark";
  if (next === "dark") {
    // 暗色是 :root 默认值，不留属性，保证与既有视觉逐像素一致
    root.documentElement.removeAttribute("data-theme");
  } else {
    root.documentElement.setAttribute("data-theme", next);
  }
  for (const button of root.querySelectorAll("[data-theme-set]")) {
    button.setAttribute("aria-current", String(button.dataset.themeSet === next));
  }
  return next;
}

export function setTheme(theme, root = document) {
  const next = applyTheme(theme, root);
  window.localStorage?.setItem(themeStorageKey, next);
  root.dispatchEvent?.(new CustomEvent("ff:theme-change", {detail: {theme: next}}));
  return next;
}

export function initTheme(root = document) {
  const theme = applyTheme(getTheme(), root);
  root.addEventListener("click", (event) => {
    const trigger = event.target.closest?.("[data-theme-set]");
    if (trigger) setTheme(trigger.dataset.themeSet, root);
  });
  return theme;
}

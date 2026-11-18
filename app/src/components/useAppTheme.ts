import { useEffect, useState } from "react";

/* The theme the page is showing: data-theme on <html> where the person picked one (ThemeToggle), else the
   system's (prefers-color-scheme), read again as either changes. */
export function currentAppTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "dark";
  const picked = document.documentElement.dataset.theme;
  if (picked === "light" || picked === "dark") return picked;
  try {
    return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function useAppTheme(): "light" | "dark" {
  const [theme, setTheme] = useState(currentAppTheme);
  useEffect(() => {
    const update = () => setTheme(currentAppTheme());
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const media = window.matchMedia?.("(prefers-color-scheme: light)");
    media?.addEventListener?.("change", update);
    return () => {
      observer.disconnect();
      media?.removeEventListener?.("change", update);
    };
  }, []);
  return theme;
}

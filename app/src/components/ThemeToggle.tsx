import { useEffect, useState } from "react";

/* Light and dark: the system's choice by default (styles.css follows
   prefers-color-scheme), or one the person picks, kept in localStorage and
   applied as data-theme on <html>. */
export type Theme = "system" | "light" | "dark";
export const THEME_KEY = "brindle.theme";
const ORDER: Theme[] = ["system", "light", "dark"];
const LABEL: Record<Theme, string> = { system: "System theme", light: "Light theme", dark: "Dark theme" };

export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme): void {
  const html = document.documentElement;
  if (theme === "system") delete html.dataset.theme;
  else html.dataset.theme = theme;
}

export function ThemeToggle({ className = "", choices = false }: { className?: string; choices?: boolean }) {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    applyTheme(theme);
    try {
      if (theme === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      // No storage (a private window): the choice lasts for this window.
    }
  }, [theme]);
  const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
  if (choices) {
    return (
      <div role="group" aria-label="Theme" className="flex items-center gap-1">
        {(["dark", "light", "system"] as Theme[]).map((choice) => (
          <button key={choice} type="button" aria-pressed={theme === choice} className={`flex-1 rounded-md px-2 py-1 text-[11px] hover:text-ink ${theme === choice ? "bg-surface-2 text-ink" : "text-ink-3"}`} onClick={() => setTheme(choice)}>
            {choice.charAt(0).toUpperCase() + choice.slice(1)}
          </button>
        ))}
      </div>
    );
  }
  return (
    <button
      type="button"
      className={className}
      title={`${LABEL[theme]}. Switch to ${LABEL[next].toLowerCase()}.`}
      aria-label={`${LABEL[theme]}; switch to ${LABEL[next].toLowerCase()}`}
      onClick={() => setTheme(next)}
    >
      {theme === "system" ? "◐" : theme === "light" ? "○" : "●"}
      <span>{theme === "system" ? "Auto" : theme === "light" ? "Light" : "Dark"}</span>
    </button>
  );
}

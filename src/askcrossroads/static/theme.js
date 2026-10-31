// AskCrossroads: light or dark, for the whole page.
//
// The page follows the computer's own light or dark setting until the person presses the
// moon/sun button in the top-right corner. Their choice is then remembered in this browser.
// Choosing the mode the computer already uses forgets the choice again, so the page goes
// back to following the computer.
//
// This file loads in <head>, before anything is drawn, so the page never flashes the wrong
// colours first. It sets data-theme="light" or "dark" on the page, and style.css does the
// rest.

"use strict";

const THEME_STORAGE_KEY = "askcrossroads-theme";
const systemPrefersDark = window.matchMedia("(prefers-color-scheme: dark)");

// "light", "dark", or null to follow the computer.
let themeChoice = savedTheme();

// The choice saved in this browser, if any. A browser can have storage switched off, and
// then there is none.
function savedTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : null;
  } catch (error) {
    return null;
  }
}

function saveTheme(choice) {
  try {
    if (choice) {
      localStorage.setItem(THEME_STORAGE_KEY, choice);
    } else {
      localStorage.removeItem(THEME_STORAGE_KEY);
    }
  } catch (error) {
    // Storage is switched off: the choice still works, until the page is closed.
  }
}

function systemTheme() {
  return systemPrefersDark.matches ? "dark" : "light";
}

function pageTheme() {
  return themeChoice || systemTheme();
}

// Puts the theme on the page, and keeps the button's label true to what pressing it does.
function applyTheme() {
  const theme = pageTheme();
  // Only when it changes: app.js redraws the chart whenever this attribute is written.
  if (document.documentElement.dataset.theme !== theme) {
    document.documentElement.dataset.theme = theme;
  }
  const button = document.getElementById("theme-toggle");
  if (button) {
    const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    button.setAttribute("aria-label", label);
    button.title = label;
  }
}

function toggleTheme() {
  const next = pageTheme() === "dark" ? "light" : "dark";
  // Choosing what the computer already uses means "follow the computer" again.
  themeChoice = next === systemTheme() ? null : next;
  saveTheme(themeChoice);
  applyTheme();
}

applyTheme();
systemPrefersDark.addEventListener("change", applyTheme);

// The button does not exist yet while <head> is being read; wire it up once it does.
document.addEventListener("DOMContentLoaded", () => {
  applyTheme();
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
});

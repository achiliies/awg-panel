/**
 * i18n setup for the panel.
 *
 * The catalogs are bundled rather than fetched: the panel is served from a
 * secret base path with a strict CSP, and a language file that arrives late
 * would show raw keys on the login screen. Two small JSON files cost less than
 * the loading machinery.
 *
 * A language also decides `dir` on <html>, so that adding a right-to-left one
 * needs nothing beyond its entry below. That is done here, not in a component,
 * because the attribute has to be right for the very first render, including
 * the error boundary.
 */

import i18next from "i18next";
import { initReactI18next, useTranslation } from "react-i18next";

import en from "./en.json";
import ru from "./ru.json";

/** localStorage key holding the language chosen in the UI. */
export const LANG_STORAGE_KEY = "awg-panel-lang";

export type SupportedLanguage = "en" | "ru";

export type TextDirection = "ltr" | "rtl";

export interface LanguageOption {
  code: SupportedLanguage;
  /** Written in the language itself, so the menu is readable before switching. */
  nativeName: string;
  /** English name, for the settings list and for screen readers. */
  englishName: string;
  dir: TextDirection;
}

export const LANGUAGES: readonly LanguageOption[] = [
  { code: "en", nativeName: "English", englishName: "English", dir: "ltr" },
  { code: "ru", nativeName: "Русский", englishName: "Russian", dir: "ltr" },
];

export const FALLBACK_LANGUAGE: SupportedLanguage = "en";

export function isSupportedLanguage(value: unknown): value is SupportedLanguage {
  return value === "en" || value === "ru";
}

/** "ru-RU", "RU_ru" and "ru" all mean the same catalog here. */
function normalizeLanguage(tag: string | null | undefined): SupportedLanguage | null {
  if (!tag) {
    return null;
  }
  const base = tag.toLowerCase().split(/[-_]/)[0];
  return isSupportedLanguage(base) ? base : null;
}

function storedLanguage(): SupportedLanguage | null {
  try {
    return normalizeLanguage(window.localStorage.getItem(LANG_STORAGE_KEY));
  } catch {
    // Storage blocked: fall through to the browser's own preference.
    return null;
  }
}

function persistLanguage(lang: SupportedLanguage): void {
  try {
    window.localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch {
    // The choice holds for this tab; it just will not survive a reload.
  }
}

function detectLanguage(): SupportedLanguage {
  const stored = storedLanguage();
  if (stored) {
    return stored;
  }
  if (typeof navigator !== "undefined") {
    const tags = [...(navigator.languages ?? []), navigator.language];
    for (const tag of tags) {
      const match = normalizeLanguage(tag);
      if (match) {
        return match;
      }
    }
  }
  return FALLBACK_LANGUAGE;
}

/** Reading direction for a language tag. Unknown tags read left to right. */
export function directionOf(tag: string | null | undefined): TextDirection {
  const code = normalizeLanguage(tag);
  return LANGUAGES.find((entry) => entry.code === code)?.dir ?? "ltr";
}

function applyDocumentLanguage(tag: string | null | undefined): void {
  if (typeof document === "undefined") {
    return;
  }
  const code = normalizeLanguage(tag) ?? FALLBACK_LANGUAGE;
  const root = document.documentElement;
  root.lang = code;
  root.dir = directionOf(code);
}

const initial = detectLanguage();

void i18next.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    ru: { translation: ru },
  },
  lng: initial,
  fallbackLng: FALLBACK_LANGUAGE,
  supportedLngs: LANGUAGES.map((entry) => entry.code),
  load: "languageOnly",
  defaultNS: "translation",
  ns: ["translation"],
  // React escapes everything it renders; escaping again turns an apostrophe in
  // a client name into &#39;.
  interpolation: { escapeValue: false },
  // A missing key must read as the key, never as an empty label.
  returnNull: false,
  returnEmptyString: false,
  // Every catalog is already in the bundle, so there is nothing to suspend on.
  react: { useSuspense: false },
  debug: false,
});

i18next.on("languageChanged", (lng: string) => {
  applyDocumentLanguage(lng);
  const code = normalizeLanguage(lng);
  if (code) {
    persistLanguage(code);
  }
});

applyDocumentLanguage(initial);

/** The language actually in use, collapsed to one of the supported codes. */
export function currentLanguage(): SupportedLanguage {
  return normalizeLanguage(i18next.language) ?? FALLBACK_LANGUAGE;
}

/**
 * Switch languages. Unknown tags fall back to English rather than leaving the
 * UI half translated, and the choice is persisted by the languageChanged
 * handler above.
 */
export function setLanguage(tag: string): void {
  const next = normalizeLanguage(tag) ?? FALLBACK_LANGUAGE;
  if (next === currentLanguage()) {
    return;
  }
  void i18next.changeLanguage(next);
}

export const i18n = i18next;

export { useTranslation as useT };

export default i18next;

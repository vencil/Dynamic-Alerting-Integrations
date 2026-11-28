/**
 * Dark mode is one mechanism: <html data-theme> (ADR-015, #4290).
 *
 * The loader sets only `data-theme` — never the class `dark`, and no
 * Tailwind `darkMode` — and its own chrome follows `[data-theme="dark"]`:
 * the body and the back link are checked here (a rule left on `html.dark`
 * stays light on the dark page); one that set the class again fails the
 * class check. No component is loaded: this is the loader's own behaviour.
 */
import { test, expect, Page } from '@playwright/test';

test.use({ colorScheme: 'light' }); // "system" then reads as light

const DARK_BODY = 'rgb(15, 23, 42)'; // #0f172a
const LIGHT_BODY = 'rgb(248, 250, 252)'; // #f8fafc
const BACK_LINK = { dark: 'rgb(148, 163, 184)', light: 'rgb(100, 116, 139)' }; // #94a3b8 / #64748b

async function themeState(page: Page) {
  return page.evaluate(() => ({
    theme: document.documentElement.getAttribute('data-theme'),
    darkClass: document.documentElement.classList.contains('dark'),
    body: getComputedStyle(document.body).backgroundColor,
    backLink: getComputedStyle(document.querySelector('.back-home') as Element).color,
    tailwindDarkMode:
      ((window as unknown as { tailwind?: { config?: { darkMode?: unknown } } }).tailwind?.config
        ?.darkMode ?? null),
  }));
}

for (const [pref, theme, body, afterClick, bodyAfter] of [
  // The cycle is light → dark → system.
  ['dark', 'dark', DARK_BODY, 'light', LIGHT_BODY],
  ['light', 'light', LIGHT_BODY, 'dark', DARK_BODY],
] as const) {
  test(`stored "${pref}": data-theme alone, and the toggle switches it`, async ({ page }) => {
    await page.addInitScript((p) => {
      try {
        localStorage.setItem('__da_theme_pref', p);
      } catch {
        /* storage blocked: the test below then fails on the theme */
      }
    }, pref);
    await page.goto('../assets/jsx-loader.html');

    expect(await themeState(page)).toEqual({
      theme,
      darkClass: false,
      body,
      backLink: BACK_LINK[theme],
      tailwindDarkMode: null,
    });

    await page.locator('#theme-toggle').click();
    // The back link's colour has a 0.15s transition: wait for it to settle.
    await expect
      .poll(() => themeState(page))
      .toEqual({
        theme: afterClick,
        darkClass: false,
        body: bodyAfter,
        backLink: BACK_LINK[afterClick],
        tailwindDarkMode: null,
      });
  });
}

// Focused review of the standalone artifact; no app or sandbox policy changes.
// Full review + screenshots: node scripts/review_sandbox_options.mjs
// Without browser privileges: node scripts/review_sandbox_options.mjs --dom-only
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { JSDOM } from '../app/node_modules/jsdom/lib/api.js';
import { chromium } from '../app/node_modules/playwright/index.mjs';

const pageUrl = new URL('../design/sandbox-options.html', import.meta.url);
const html = await readFile(pageUrl, 'utf8');
// Exercise every event branch in both system themes without requiring a GUI.
for (const light of [true, false]) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    beforeParse(window) { window.matchMedia = () => ({ matches: light }); },
  });
  const { document, Event } = dom.window;
  const one = selector => document.querySelector(selector);
  const all = selector => [...document.querySelectorAll(selector)];
  for (const service of all('[data-component]')) {
    service.click();
    assert.equal(one('.pipes .active').dataset.pipe, service.dataset.component);
    for (const checked of [false, true]) {
      one('#defense').checked = checked;
      one('#defense').dispatchEvent(new Event('change'));
      assert.equal(one('#attack').classList.contains('safe'), checked);
      for (let index = 0; index < all('#steps li').length; index++) {
        assert.equal(all('#steps li').indexOf(one('[aria-current="step"]')), index);
        one('#next').click();
      }
      one('#next').click();
      one('#back').click();
      assert(one('#back').disabled);
    }
  }
  const presetStates = new Set();
  for (const preset of all('button[data-preset]')) {
    preset.click();
    assert.equal(one('#settings').dataset.preset, preset.dataset.preset);
    assert.equal(all('[data-choice][aria-pressed="true"]').length, all('#settings select').length);
    const state = all('#settings select').map(node => node.value).join(',');
    assert(!presetStates.has(state));
    presetStates.add(state);
  }
  for (const choice of all('[data-choice]:enabled')) {
    choice.click();
    assert.equal(one(`#setting-${choice.dataset.row}`).value, choice.dataset.choice);
    assert.equal(choice.getAttribute('aria-pressed'), 'true');
  }
  for (const field of all('#settings select')) {
    field.value = field.options[0].value;
    field.dispatchEvent(new Event('change'));
    const row = field.id.replace('setting-', '');
    assert.equal(one(`[data-row="${row}"][aria-pressed="true"]`).dataset.choice, field.value);
    one(`[data-scenario="${row}"]`).click();
    one('#review-allowance').click();
    assert.equal(document.activeElement, field);
    assert.equal(one('#failure').dataset.scenario, row);
  }
  one('#save-preview').click();
  const saved = JSON.parse(one('#settings').dataset.saved);
  for (const field of all('#settings select')) assert.equal(saved[field.id.replace('setting-', '')], field.value);
  const theme = document.documentElement.dataset.theme;
  one('#theme').click();
  assert.notEqual(document.documentElement.dataset.theme, theme);
  one('#theme').click();
  assert.equal(document.documentElement.dataset.theme, theme);
  for (const link of all('a[href^="#"], use')) assert(document.getElementById(link.getAttribute('href').slice(1)));
  dom.window.close();
}
console.log('Passed DOM interactions: attack/defense paths, presets, matrix/settings synchronization, failure navigation, save preview, themes and internal anchors.');

if (!process.argv.includes('--dom-only')) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, colorScheme: 'light', reducedMotion: 'reduce' });
    const page = await context.newPage();
    const errors = [];
    const network = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/^https?:/.test(request.url())) network.push(request.url()); });
    await page.goto(pageUrl.href);
    // Check actual browser hit targets and focus; DOM checks cannot verify these.
    for (const button of await page.locator('.service, button[data-preset], [data-choice]:enabled, [data-scenario]').all()) {
      await button.click();
      assert.equal(await button.getAttribute('aria-pressed'), 'true');
    }
    await page.locator('#review-allowance').click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'setting-keychain');
    for (const detail of await page.locator('details').all()) {
      await detail.locator('summary').click();
      assert.equal(await detail.getAttribute('open'), '');
    }
    for (const width of [375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1100 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `Viewport overflow at ${width}px`);
    }
    await page.locator('details').evaluateAll(nodes => nodes.forEach(node => { node.open = false; }));
    await page.locator('button[data-preset="balanced"]').click();
    await page.locator('[data-component="tmux"]').click();
    await page.locator('#next').click();
    await page.locator('#next').click();
    await page.locator('[data-scenario="docker"]').click();
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    const backgrounds = new Set();
    for (const theme of ['light', 'dark']) {
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      backgrounds.add(await page.evaluate(() => getComputedStyle(document.body).backgroundColor));
      await page.screenshot({ path: fileURLToPath(new URL(`../design/sandbox-options-${theme}.png`, import.meta.url)), fullPage: true });
      await page.locator('#theme').click();
    }
    assert.equal(backgrounds.size, 2);
    assert.deepEqual(errors, []);
    assert.deepEqual(network, [], 'The standalone page must render without network requests');
    console.log('Passed browser hit targets, focus, responsive layout and offline rendering. Saved 1440px light/dark screenshots.');
  } finally {
    await browser.close();
  }
} else {
  console.log('Browser rendering and screenshots were not checked (--dom-only).');
}

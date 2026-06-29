/**
 * Getting Started Wizard — unit tests (#811 role-scoped IA refactor).
 *
 * Covers the behaviours the #811 refactor introduces / must preserve:
 *   - role-scoped step-1: picking a role shows ONLY that role's options,
 *     never another role's branches (the cross-role-noise removal);
 *   - per-role lifecycle axis: platform/tenant render bucket <h4> headings,
 *     domain stays flat (db types, not stages);
 *   - within-role PathCompare: the compare dropdown lists only same-role
 *     paths (the ALL_PATHS → pathsForRole fix);
 *   - deep-link #role=tenant&option=routing lands on step-2 (bucketing is
 *     display-only over `option`);
 *   - read-progress toggles round-trip through the URL hash.
 *
 * jsdom env + window.__t=English are provided by test-setup.ts. The wizard
 * reads window.location.hash at mount (readHash), so deep-link tests set the
 * hash BEFORE render. The module-level `const t` is captured at import time
 * with window.__t already set by the setup file — so all assertions match the
 * English branch.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import GettingStartedWizard from '../src/getting-started/wizard.jsx';

const CLEAN_PATH = '/getting-started/';

function resetUrl() {
  window.history.replaceState(null, '', CLEAN_PATH);
}

describe('GettingStartedWizard — #811 role-scoped IA', () => {
  beforeEach(() => {
    resetUrl();
    // __flowSave is read defensively (if present); provide a no-op so the
    // role-select handler exercises the same path it does in the browser.
    (window as any).__flowSave = () => {};
  });
  afterEach(() => {
    resetUrl();
    delete (window as any).__flowSave;
  });

  it('(a) picking Platform shows ONLY platform options — no other role leaks in', () => {
    render(<GettingStartedWizard />);
    // Step 0 → pick Platform Engineer
    fireEvent.click(screen.getByRole('button', { name: /Platform Engineer/i }));

    // Platform's own goals are present…
    expect(screen.getByRole('button', { name: /Initial Setup/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Federation/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Monitoring & Scaling/i })).toBeInTheDocument();

    // …and NO branch from the other two roles is rendered.
    // domain branch (a db type) must not appear as an option:
    expect(screen.queryByRole('button', { name: /^MariaDB$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /PostgreSQL/i })).toBeNull();
    // tenant branch must not appear:
    expect(screen.queryByRole('button', { name: /Onboard to Platform/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /Set Up Alert Routing/i })).toBeNull();
  });

  it('(a2) platform renders lifecycle bucket headings (Provision / Operate)', () => {
    render(<GettingStartedWizard />);
    fireEvent.click(screen.getByRole('button', { name: /Platform Engineer/i }));
    // Non-empty buckets surface as <h4> sub-headings.
    expect(screen.getByRole('heading', { level: 4, name: /Provision/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 4, name: /Operate/i })).toBeInTheDocument();
    // federation is deliberately filed under Provision, not its own stage.
    const provision = screen.getByRole('heading', { level: 4, name: /Provision/i }).parentElement!;
    expect(within(provision).getByRole('button', { name: /Federation/i })).toBeInTheDocument();
  });

  it('(a3) domain stays FLAT — no lifecycle sub-headings, db types shown', () => {
    render(<GettingStartedWizard />);
    fireEvent.click(screen.getByRole('button', { name: /Domain Expert/i }));
    // db-type options are present…
    expect(screen.getByRole('button', { name: /^MariaDB$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Redis/i })).toBeInTheDocument();
    // …and there are NO lifecycle bucket headings forced onto them.
    expect(screen.queryByRole('heading', { level: 4, name: /Provision/i })).toBeNull();
    expect(screen.queryByRole('heading', { level: 4, name: /Operate/i })).toBeNull();
    expect(screen.queryByRole('heading', { level: 4, name: /Maintain/i })).toBeNull();
  });

  it('(b) deep-link #role=tenant&option=routing lands directly on step-2', () => {
    window.history.replaceState(null, '', CLEAN_PATH + '#role=tenant&option=routing');
    render(<GettingStartedWizard />);
    // Step-2 recommendations title for tenant-routing is rendered (level-2
    // heading), proving bucketing did not break the option= deep link.
    expect(
      screen.getByRole('heading', { level: 2, name: /Set Up Alert Routing & Notifications/i })
    ).toBeInTheDocument();
    // The grow-ops handoff seam is present for the tenant role.
    expect(screen.getByRole('heading', { name: /Ready to act\?/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Recipe Builder/i })).toHaveAttribute('href', 'recipe-builder.html');
  });

  it('(b2) PathCompare lists only same-role paths (no cross-role leak)', () => {
    window.history.replaceState(null, '', CLEAN_PATH + '#role=tenant&option=routing');
    render(<GettingStartedWizard />);
    // open the compare panel
    fireEvent.click(screen.getByRole('button', { name: /Compare Paths/i }));
    const combo = screen.getByRole('combobox') as HTMLSelectElement;
    const optionTexts = Array.from(combo.options).map(o => o.textContent || '');
    // Every non-placeholder option must be a TENANT path. Tenant titles
    // ("...Team Onboarded", "Configure Alerts...", "...Maintenance...") —
    // none of the platform/domain titles may appear.
    const real = optionTexts.filter(txt => txt && !txt.includes('Select a path'));
    expect(real.length).toBeGreaterThan(0);
    expect(real.some(txt => /MariaDB|PostgreSQL|Redis|Federation|Platform Initial Setup/i.test(txt))).toBe(false);
    // current path (routing) is excluded from its own compare list
    expect(real.some(txt => /Set Up Alert Routing/i.test(txt))).toBe(false);
    // a sibling tenant path IS offered
    expect(real.some(txt => /Configure Alerts|Onboarded|Maintenance/i.test(txt))).toBe(true);
  });

  it('(c) read-progress toggle round-trips through the URL hash', () => {
    window.history.replaceState(null, '', CLEAN_PATH + '#role=tenant&option=routing');
    render(<GettingStartedWizard />);
    // counter starts at 0/N
    expect(screen.getByText(/^0\/\d+$/)).toBeInTheDocument();

    // toggle the first doc's read checkbox (the "Mark as read" button)
    const markRead = screen.getAllByRole('button', { name: /Mark as read/i })[0];
    fireEvent.click(markRead);

    // URL hash now carries a read= segment (round-trip persistence)…
    expect(window.location.hash).toMatch(/read=/);
    // …and the counter advanced to 1/N.
    expect(screen.getByText(/^1\/\d+$/)).toBeInTheDocument();

    // toggling back removes it from the hash and resets the counter.
    const markUnread = screen.getAllByRole('button', { name: /Mark as unread/i })[0];
    fireEvent.click(markUnread);
    expect(window.location.hash).not.toMatch(/read=/);
    expect(screen.getByText(/^0\/\d+$/)).toBeInTheDocument();
  });
});

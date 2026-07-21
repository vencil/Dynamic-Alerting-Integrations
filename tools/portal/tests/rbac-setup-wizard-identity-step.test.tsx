/**
 * Interaction coverage for the RBAC wizard's "Identity Conditions" step
 * (StepIdentity, ADR-027 / LD-6 P7d).
 *
 * The generator unit tests (rbac-setup-wizard-generators.test.ts) + the Go
 * tripwire cover rbacGenerateYaml/rbacValidate — the PURE functions. They do
 * NOT exercise the React component that feeds them: whether "Add claim
 * condition" wires a claim row into group state, whether a value chip commits,
 * whether the org-scope foot-gun warning renders, and whether all of that flows
 * into the emitted YAML. This drives the real <RBACSetupWizard/> through the new
 * step and back to Review to close that gap.
 *
 * Text is English (test-setup.ts sets window.__t to the English arm). Stepper
 * buttons carry role="listitem" (a11y list semantics), so navigation queries by
 * that role, not by "button".
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import RBACSetupWizard from '../src/interactive/tools/rbac-setup-wizard.jsx';

function addGroup(name: string) {
  const input = screen.getByPlaceholderText('Enter group name...');
  fireEvent.change(input, { target: { value: name } });
  fireEvent.keyDown(input, { key: 'Enter' });
}

// Stepper buttons are <button role="listitem"> — listitem does not derive an
// accessible name from its content, so query by the exact label text. Steps
// ahead of the current one carry no "✓ " prefix, so the label is the full text
// (and won't collide with the step's own "Step N: ..." heading).
function gotoStep(label: string) {
  fireEvent.click(screen.getByText(label));
}

function reviewYaml(container: HTMLElement): string {
  return container.querySelector('pre')?.textContent ?? '';
}

describe('RBAC wizard — Identity Conditions step (StepIdentity, P7d)', () => {
  it('adding a claim key+value and an org-scope flows into the generated YAML', () => {
    const { container } = render(<RBACSetupWizard />);
    addGroup('org-ops');

    gotoStep('Identity Conditions (optional)');

    // Add a claim condition, then a key and a value.
    fireEvent.click(screen.getByRole('button', { name: /Add claim condition/ }));
    fireEvent.change(screen.getByLabelText('Claim key'), { target: { value: 'org-code' } });
    const valInput = screen.getByPlaceholderText('Type an allowed value, press Enter');
    fireEvent.change(valInput, { target: { value: '006000J' } });
    fireEvent.keyDown(valInput, { key: 'Enter' });
    // the committed value renders as a chip
    expect(screen.getByText('006000J')).toBeInTheDocument();

    // Set the org-scope axis.
    fireEvent.change(screen.getByLabelText('Org-scope claim key'), { target: { value: 'org-code' } });

    // Review: the emitted YAML reflects both identity axes, with `name` copied
    // into match.groups (the matcher stays honest) — the exact shape the Go
    // parser accepts.
    gotoStep('Review & Export');
    const yaml = reviewYaml(container);
    expect(yaml).toContain('- name: org-ops');
    expect(yaml).toContain('match:');
    expect(yaml).toContain('groups: ["org-ops"]');
    expect(yaml).toContain('"org-code": ["006000J"]');
    expect(yaml).toContain('org-scope: org-code');
  });

  it('the org-scope foot-gun warning renders only once org-scope is set', () => {
    render(<RBACSetupWizard />);
    addGroup('org-ops');
    gotoStep('Identity Conditions (optional)');

    // Absent before opt-in.
    expect(screen.queryByText('Before enabling org-scope, confirm:')).toBeNull();

    fireEvent.change(screen.getByLabelText('Org-scope claim key'), { target: { value: 'org-code' } });

    // The four-point warning appears, including the load-bearing shadow-mode
    // caveat (a caller missing/mismatched claim is denied under BOTH modes and
    // is invisible to the would-deny soak metric).
    expect(screen.getByText('Before enabling org-scope, confirm:')).toBeInTheDocument();
    expect(screen.getByText(/Shadow mode does NOT protect/)).toBeInTheDocument();
  });

  it('no claims configured → legacy shape (no match block) in the YAML', () => {
    const { container } = render(<RBACSetupWizard />);
    addGroup('sre');
    gotoStep('Review & Export');
    const yaml = reviewYaml(container);
    expect(yaml).toContain('- name: sre');
    expect(yaml).not.toContain('match:');
  });
});

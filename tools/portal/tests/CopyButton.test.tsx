/**
 * Component tests for the shared CopyButton (_common/components/CopyButton.jsx),
 * portal ROI refactor Cycle 4b.
 *
 * CopyButton canonicalizes the `✓ Copied` / label content + useCopyToClipboard
 * wiring that was hand-duplicated across the static-styled copy buttons
 * (alert-builder, routing-trace). These tests are the safety net written
 * BEFORE the call sites were swapped: they pin the idle label, the
 * className / data-testid / type pass-through, and the click → writes text →
 * shows "Copied" behaviour. jsdom ships no navigator.clipboard, so we install
 * a resolving writeText mock (same pattern as useCopyToClipboard.test.tsx) —
 * without it the copy silently fails and `copied` never flips.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CopyButton } from '../src/interactive/tools/_common/components/CopyButton.jsx';

let writeText: ReturnType<typeof vi.fn>;

function installClipboard(fn: ReturnType<typeof vi.fn>) {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: fn }, configurable: true, writable: true });
}

describe('CopyButton', () => {
  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the idle label (en via test-setup __t) and passes through className/testid/type', () => {
    render(<CopyButton text="hello" labelZh="複製 YAML" labelEn="Copy YAML" className="my-cls" testId="cb" />);
    const btn = screen.getByTestId('cb');
    expect(btn).toHaveTextContent('Copy YAML');
    expect(btn).toHaveClass('my-cls');
    expect(btn.getAttribute('type')).toBe('button');
  });

  it('omits data-testid when no testId prop is given', () => {
    render(<CopyButton text="x" labelZh="複製" labelEn="Copy" className="c" />);
    expect(screen.getByRole('button').hasAttribute('data-testid')).toBe(false);
  });

  it('writes the given text to the clipboard and flips to "Copied" on click', async () => {
    render(<CopyButton text="the-yaml-body" labelZh="複製" labelEn="Copy" className="c" testId="cb" />);
    const btn = screen.getByTestId('cb');
    expect(btn).toHaveTextContent('Copy');

    fireEvent.click(btn);

    // async copy resolves → copied flips true → content re-renders to "Copied"
    expect(await screen.findByText('Copied')).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith('the-yaml-body');
    expect(btn).toHaveTextContent('✓ Copied');
  });

  it('stays on the idle label when the clipboard write rejects', async () => {
    installClipboard(vi.fn().mockRejectedValue(new Error('denied')));
    render(<CopyButton text="x" labelZh="複製" labelEn="Copy" className="c" testId="cb" />);
    const btn = screen.getByTestId('cb');
    fireEvent.click(btn);
    // give the rejected promise a tick to settle; copied must remain false
    await Promise.resolve();
    expect(btn).toHaveTextContent('Copy');
    expect(btn).not.toHaveTextContent('Copied');
  });
});

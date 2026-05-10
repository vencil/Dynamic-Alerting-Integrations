/**
 * Unit tests for useModalFocusTrap hook — Vitest next-batch (PR-2 of expansion).
 *
 * Hook contract from `_common/hooks/useModalFocusTrap.js`:
 *   - Returns a ref the caller attaches to the modal container.
 *   - When modalType is truthy, auto-focuses modalRef + installs a
 *     keydown listener on document that:
 *     * Escape → setModalType(null)
 *     * Tab    → cycles forward (last → first if at end)
 *     * Shift+Tab → cycles backward (first → last if at start)
 *   - When modalType becomes falsy or hook unmounts, removes the listener.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { useModalFocusTrap } from '../src/interactive/tools/_common/hooks/useModalFocusTrap.js';

/** Build a modal DOM with N focusable buttons, attached to document.body. */
function buildModal(n: number): { container: HTMLDivElement; buttons: HTMLButtonElement[] } {
  const container = document.createElement('div');
  container.tabIndex = -1; // ref'd container itself is focusable for auto-focus
  const buttons: HTMLButtonElement[] = [];
  for (let i = 0; i < n; i++) {
    const btn = document.createElement('button');
    btn.textContent = `btn-${i}`;
    container.appendChild(btn);
    buttons.push(btn);
  }
  document.body.appendChild(container);
  return { container, buttons };
}

/** Fire a keydown event with the given key (and shiftKey) on document. */
function pressKey(key: string, shiftKey = false): KeyboardEvent {
  const ev = new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true });
  document.dispatchEvent(ev);
  return ev;
}

describe('useModalFocusTrap', () => {
  let setModalTypeSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    setModalTypeSpy = vi.fn();
    document.body.innerHTML = '';
  });

  afterEach(() => {
    document.body.innerHTML = '';
  });

  // ─────────────────────────────────────────────────────────────────
  // Ref + initial state
  // ─────────────────────────────────────────────────────────────────

  it('returns a ref object the caller can attach to the modal', () => {
    const { result } = renderHook(() => useModalFocusTrap(null, setModalTypeSpy));
    // ref starts as { current: null } — attach happens in callsite JSX.
    expect(result.current).toEqual({ current: null });
  });

  it('does NOT install keydown listener when modalType is null', () => {
    renderHook(() => useModalFocusTrap(null, setModalTypeSpy));
    // No listener → keydown should not invoke setModalType.
    pressKey('Escape');
    expect(setModalTypeSpy).not.toHaveBeenCalled();
  });

  // ─────────────────────────────────────────────────────────────────
  // Activation when modalType is truthy + ref is attached
  // ─────────────────────────────────────────────────────────────────

  it('auto-focuses the modal element when activated', () => {
    const { container } = buildModal(2);
    const focusSpy = vi.spyOn(container, 'focus');

    // Use a wrapper that attaches ref to our pre-built container, since
    // the ref normally fills in via JSX ref={...}.
    const Hook = () => {
      const ref = useModalFocusTrap('maintenance', setModalTypeSpy);
      // Mimic JSX-side ref attachment.
      // useLayoutEffect runs in commit phase BEFORE production hook's
      // useEffect — so by the time the trap effect runs, ref.current is
      // already attached. Mimics what the consumer JSX `<div ref={ref}>`
      // does synchronously after mount.
      useLayoutEffect(() => {
        (ref as any).current = container;
      }, [ref]);
      return ref;
    };
    renderHook(Hook);
    // The hook's effect runs after mount; container.focus should fire.
    expect(focusSpy).toHaveBeenCalled();
  });

  it('Escape calls setModalType(null) to close', () => {
    const { container } = buildModal(2);
    renderHookWithRef('confirm-delete', container, setModalTypeSpy);

    pressKey('Escape');
    expect(setModalTypeSpy).toHaveBeenCalledWith(null);
  });

  // ─────────────────────────────────────────────────────────────────
  // Tab focus trap
  // ─────────────────────────────────────────────────────────────────

  it('Tab from last focusable cycles back to first (forward wrap)', () => {
    const { container, buttons } = buildModal(3);
    renderHookWithRef('open', container, setModalTypeSpy);

    buttons[2].focus(); // last focusable
    expect(document.activeElement).toBe(buttons[2]);

    pressKey('Tab');
    expect(document.activeElement).toBe(buttons[0]);
  });

  it('Shift+Tab from first focusable cycles to last (backward wrap)', () => {
    const { container, buttons } = buildModal(3);
    renderHookWithRef('open', container, setModalTypeSpy);

    buttons[0].focus();
    pressKey('Tab', /* shiftKey */ true);
    expect(document.activeElement).toBe(buttons[2]);
  });

  it('Tab from middle does NOT trap (browser handles natural cycle)', () => {
    const { container, buttons } = buildModal(3);
    renderHookWithRef('open', container, setModalTypeSpy);

    buttons[1].focus();
    const ev = pressKey('Tab');
    // We only preventDefault when at the edges. Middle Tab → not prevented.
    expect(ev.defaultPrevented).toBe(false);
  });

  it('Tab is no-op when modal contains no focusable elements', () => {
    // Build a modal with just text — no buttons / inputs / etc.
    const container = document.createElement('div');
    container.tabIndex = -1;
    container.textContent = 'just text';
    document.body.appendChild(container);

    renderHookWithRef('open', container, setModalTypeSpy);

    expect(() => pressKey('Tab')).not.toThrow();
  });

  // ─────────────────────────────────────────────────────────────────
  // Cleanup
  // ─────────────────────────────────────────────────────────────────

  it('removes keydown listener on unmount', () => {
    const { container } = buildModal(2);
    const { unmount } = renderHookWithRef('open', container, setModalTypeSpy);
    unmount();
    pressKey('Escape');
    // After unmount, Escape should NOT call setModalType.
    expect(setModalTypeSpy).not.toHaveBeenCalled();
  });

  it('removes keydown listener when modalType transitions to null', () => {
    const { container } = buildModal(2);
    let setOpenInternal: ((v: any) => void) | undefined;
    const Hook = ({ open }: { open: string | null }) => {
      const ref = useModalFocusTrap(open, setModalTypeSpy);
      // useLayoutEffect runs in commit phase BEFORE production hook's
      // useEffect — so by the time the trap effect runs, ref.current is
      // already attached. Mimics what the consumer JSX `<div ref={ref}>`
      // does synchronously after mount.
      useLayoutEffect(() => {
        (ref as any).current = container;
      }, [ref]);
      return ref;
    };
    const { rerender } = renderHook(({ open }: { open: string | null }) => Hook({ open }), {
      initialProps: { open: 'maintenance' as string | null },
    });

    // While open, Escape closes.
    pressKey('Escape');
    expect(setModalTypeSpy).toHaveBeenCalledTimes(1);

    setModalTypeSpy.mockReset();
    rerender({ open: null });

    // After modalType=null, Escape should be ignored (listener removed).
    pressKey('Escape');
    expect(setModalTypeSpy).not.toHaveBeenCalled();
  });
});

/** Helper: render the hook with a ref pre-attached to `container`. */
function renderHookWithRef(
  modalType: string | null,
  container: HTMLElement,
  setModalType: (v: any) => void,
) {
  const Hook = () => {
    const ref = useModalFocusTrap(modalType, setModalType);
    useLayoutEffect(() => {
      (ref as any).current = container;
    }, [ref]);
    return ref;
  };
  return renderHook(Hook);
}

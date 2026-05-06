---
title: "_common — useModalFocusTrap hook"
purpose: |
  Modal focus trap + escape-key + auto-focus management. Hoisted above
  the `if (loading) return` early return in PR #150 (commit 2caddc2)
  to fix Rules-of-Hooks #310 — the original placement after the early
  return meant the FIRST render registered fewer hooks than the SECOND
  render once loading flipped false.

  Extracted from tenant-manager.jsx in PR-2d Phase 2 (#153). Owns
  `modalRef` and the focus-trap useEffect. Returns the ref for the
  caller to attach to the modal-content `<div ref={modalRef}>`.

  Closure dependencies (received via params):
    - modalType:    null | 'maintenance' | 'silent' | etc. — when
                    truthy, the trap activates (auto-focuses, traps
                    Tab cycling, handles Esc).
    - setModalType: ESC closes the modal by calling setModalType(null).

  Behavior contract: identical to the inline version. ARIA / keyboard
  semantics preserved — Esc closes, Tab cycles within focusable
  descendants of modalRef.current, focus auto-applies on open.
---

const { useRef, useEffect } = React;

function useModalFocusTrap(modalType, setModalType) {
  const modalRef = useRef(null);

  useEffect(() => {
    if (modalType && modalRef.current) {
      modalRef.current.focus();
      const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
          setModalType(null);
          return;
        }
        // Focus trap: cycle Tab within modal
        if (e.key === 'Tab' && modalRef.current) {
          const focusable = modalRef.current.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
          );
          if (focusable.length === 0) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
          } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }
      };
      document.addEventListener('keydown', handleKeyDown);
      return () => document.removeEventListener('keydown', handleKeyDown);
    }
  }, [modalType]);

  return modalRef;
}

// Register on window for orchestrator pickup.
window.__useModalFocusTrap = useModalFocusTrap;

// TD-030c: ESM export for esbuild bundle + Vitest. Both `window.__X`
// and `export { X }` removed in TD-030z when jsx-loader retires.
// <!-- jsx-loader-compat: ignore -->
export { useModalFocusTrap };

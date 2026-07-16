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

  Focus restoration (TRK-335, WCAG 2.4.3 Focus Order): on open the hook
  remembers the element that had focus (the trigger that launched the
  modal), and on close/unmount it hands focus back to it — so keyboard
  and screen-reader users return to where they were instead of being
  dropped at the top of the document. The restore is guarded: if the
  trigger unmounted while the modal was open it is skipped, never thrown.
---

import { useRef, useEffect } from "react";  // TRK-233 ESM import

function useModalFocusTrap(modalType, setModalType) {
  const modalRef = useRef(null);

  useEffect(() => {
    if (modalType && modalRef.current) {
      // Capture the trigger BEFORE we steal focus into the modal, so the
      // cleanup can return focus to it on close (return-focus-on-close).
      const trigger = document.activeElement;
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
      return () => {
        document.removeEventListener('keydown', handleKeyDown);
        // WCAG 2.4.3: return focus to the launching trigger. Guard for the
        // trigger having unmounted while the modal was open (e.g. the list
        // row that opened it got filtered away) — only refocus a real,
        // still-connected, focusable element, and never document.body.
        if (
          trigger &&
          trigger !== document.body &&
          typeof trigger.focus === 'function' &&
          document.contains(trigger)
        ) {
          trigger.focus();
        }
      };
    }
  }, [modalType]);

  return modalRef;
}

export { useModalFocusTrap };

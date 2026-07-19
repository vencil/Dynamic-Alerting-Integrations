import React from 'react';
import { useCopyToClipboard } from '../hooks/useCopyToClipboard.js';

/* ── i18n helper (live-global with fallback; dev-rules S6) ──────────── */
const t = window.__t || ((zh, en) => en);

/**
 * CopyButton — shared copy-to-clipboard button.
 *
 * Owns the useCopyToClipboard hook + the `✓ Copied` / label content that had
 * been hand-duplicated verbatim across several tools. Extracted for the
 * static-styled copy buttons (portal ROI refactor Cycle 4b); the two tools
 * whose button style is copied-DEPENDENT (deployment-wizard, release-notes)
 * and the keyed multi-copy site (cicd-setup-wizard) keep their inline buttons
 * on purpose — a single static `className` cannot express those without
 * changing behaviour.
 *
 * Styling stays per-call-site via `className` (the portal has two colour
 * conventions); `testId`, `type`, and the copy `text` pass through. The
 * copied label ("已複製" / "Copied") is identical at every call site, so it
 * lives here rather than in props.
 *
 * Props:
 *   text     — string written to the clipboard on click
 *   labelZh  — idle-state label (zh); shown via window.__t
 *   labelEn  — idle-state label (en)
 *   className — button classes (call-site owns the visual convention)
 *   testId   — optional data-testid (omitted from the DOM when absent)
 *   type     — button type attribute (default "button")
 */
function CopyButton({ text, labelZh, labelEn, className, testId, type = 'button' }) {
  const { copied, copy } = useCopyToClipboard();
  return (
    <button
      type={type}
      onClick={() => copy(text)}
      className={className}
      {...(testId ? { 'data-testid': testId } : {})}
    >
      {copied ? <><span aria-hidden="true">✓</span> {t('已複製', 'Copied')}</> : t(labelZh, labelEn)}
    </button>
  );
}

export { CopyButton };

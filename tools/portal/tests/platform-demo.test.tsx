/**
 * Platform Demo — run timer vs. typing animation.
 *
 * A phase is marked complete when handleRun's timeout fires; that flips
 * isRunning off, which snaps the terminal to the full transcript. The
 * timeout used to be `lines × speed + 1000` while the animation reveals
 * CHARS_PER_TICK characters per tick, so every phase finished early — the
 * 100-line baseline transcript after 3 s of a ~34 s animation.
 *
 * platform-demo.jsx itself can't be rendered here: it imports lucide-react,
 * which is not a dependency (build.mjs virtualises it to window.lucideReact)
 * and vite fails to resolve it before vi.mock applies. So the invariant is
 * pinned on the shared timing module that both the animation and the timer
 * call.
 */
import { describe, it, expect } from 'vitest';
import { CHARS_PER_TICK, transcriptChars, runDurationMs } from '../src/interactive/tools/platform-demo/utils/typing.js';
import { DEMO_TRANSCRIPTS } from '../src/interactive/tools/platform-demo/fixtures/transcripts.js';

// Ticks until the animation's stop condition (idx >= totalChars) is reached.
const animationMs = (lines: string[], tickMs: number) =>
  Math.ceil(transcriptChars(lines) / CHARS_PER_TICK) * tickMs;

const SPEEDS_MS = [5, 20, 50]; // the page's Fast / Normal / Slow options

describe('Platform Demo run timer', () => {
  for (const [phase, { terminal }] of Object.entries(DEMO_TRANSCRIPTS) as [string, { terminal: string[] }][]) {
    for (const speed of SPEEDS_MS) {
      it(`${phase} @ ${speed}ms: the run outlasts the typing animation`, () => {
        expect(runDurationMs(terminal, speed)).toBeGreaterThan(animationMs(terminal, speed));
      });
    }
  }

  it('the baseline transcript is long enough that the old formula cut it short', () => {
    // Keeps the checks above from passing only because every fixture is short.
    const { terminal } = DEMO_TRANSCRIPTS.baseline;
    expect(terminal.length * 20 + 1000).toBeLessThan(animationMs(terminal, 20));
  });

  it('counts each line plus its newline, as the animation walks them', () => {
    expect(transcriptChars(['ab', '', 'c'])).toBe(3 + 1 + 2);
  });
});

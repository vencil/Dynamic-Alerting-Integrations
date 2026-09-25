---
title: "Platform Demo — typing animation timing"
purpose: |
  One source for how long a phase's terminal playback takes, shared by the
  typing animation (PhaseContent) and the run timer (handleRun). The run
  timer used to be `lines × speed + 1000` while the animation reveals
  CHARS_PER_TICK characters per tick, so every phase was marked complete —
  snapping the terminal to its full output — before typing finished.

  Public API:
    CHARS_PER_TICK        characters revealed per animation tick
    transcriptChars(l)    characters the animation walks (each line + newline)
    runDurationMs(l, ms)  run timer: full animation + RUN_BUFFER_MS
---

const CHARS_PER_TICK = 2;
const RUN_BUFFER_MS = 1000;

function transcriptChars(lines) {
  return lines.reduce((s, l) => s + l.length + 1, 0);
}

function runDurationMs(lines, tickMs) {
  return Math.ceil(transcriptChars(lines) / CHARS_PER_TICK) * tickMs + RUN_BUFFER_MS;
}

export { CHARS_PER_TICK, transcriptChars, runDurationMs };

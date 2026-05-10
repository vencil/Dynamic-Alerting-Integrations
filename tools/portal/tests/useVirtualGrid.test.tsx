/**
 * Unit tests for useVirtualGrid hook — Vitest next-batch (PR-2 of expansion).
 *
 * Hook contract from `_common/hooks/useVirtualGrid.js`:
 *   - Given items[], rowHeight, columnCount, containerRef, returns
 *     { visibleItems, totalHeight, startRow, endRow }.
 *   - visibleItems is the windowed slice (overscan-padded) plus
 *     positioning info (top px, left as CSS percentage).
 *   - totalHeight = ceil(items.length / columnCount) * rowHeight.
 *   - Subscribes to container scroll + ResizeObserver for height updates.
 *
 * jsdom limitations:
 *   - ResizeObserver is NOT in jsdom by default — we stub it where needed.
 *   - container.scrollTop is settable but doesn't trigger native scroll —
 *     we dispatch the scroll event manually.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useRef } from 'react';
import { useVirtualGrid } from '../src/interactive/tools/_common/hooks/useVirtualGrid.js';

/** Build a flat items array of N strings 'item-0', 'item-1', ... */
function makeItems(n: number): string[] {
  return Array.from({ length: n }, (_, i) => `item-${i}`);
}

/**
 * Build a wrapper hook that owns its containerRef + attaches a real DOM
 * element (jsdom) so the production effects can call addEventListener
 * + read clientHeight without crashing.
 */
function useGridHarness(opts: {
  items: string[];
  rowHeight?: number;
  columnCount?: number;
  overscan?: number;
  clientHeight?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  // First render — attach a fresh div + override clientHeight.
  if (containerRef.current === null) {
    const div = document.createElement('div');
    Object.defineProperty(div, 'clientHeight', {
      configurable: true,
      get: () => opts.clientHeight ?? 800,
    });
    containerRef.current = div;
  }
  return {
    containerRef,
    grid: useVirtualGrid({
      items: opts.items,
      rowHeight: opts.rowHeight ?? 100,
      columnCount: opts.columnCount ?? 3,
      containerRef: containerRef as React.RefObject<HTMLDivElement>,
      overscan: opts.overscan,
    }),
  };
}

describe('useVirtualGrid', () => {
  let originalRO: any;

  beforeEach(() => {
    // Stub ResizeObserver to a no-op so the production effect doesn't
    // crash in jsdom. Tests that need to exercise the resize path
    // dispatch their own setContainerHeight via re-render.
    originalRO = (globalThis as any).ResizeObserver;
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  });

  afterEach(() => {
    (globalThis as any).ResizeObserver = originalRO;
  });

  // ─────────────────────────────────────────────────────────────────
  // totalHeight
  // ─────────────────────────────────────────────────────────────────

  it('totalHeight = ceil(items / columnCount) * rowHeight', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(10), rowHeight: 100, columnCount: 3 }),
    );
    // 10 items / 3 cols = 4 rows (ceil) → 400
    expect(result.current.grid.totalHeight).toBe(400);
  });

  it('totalHeight is 0 for empty items', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: [], rowHeight: 100, columnCount: 3 }),
    );
    expect(result.current.grid.totalHeight).toBe(0);
  });

  it('handles single column (columnCount=1) — every item is a row', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(5), rowHeight: 50, columnCount: 1 }),
    );
    expect(result.current.grid.totalHeight).toBe(250);
  });

  // ─────────────────────────────────────────────────────────────────
  // visibleItems window
  // ─────────────────────────────────────────────────────────────────

  it('initial render at scrollTop=0 shows the top window + overscan', () => {
    // 100 items, 3 cols → 34 rows. clientHeight=300 / row=100 → 3 visible rows
    // + overscan=2 → up to row 4 (5 rows total). 5 rows × 3 cols = 15 cards.
    const { result } = renderHook(() =>
      useGridHarness({
        items: makeItems(100),
        rowHeight: 100,
        columnCount: 3,
        clientHeight: 300,
        overscan: 2,
      }),
    );
    // Initial container measure happens after the effect; default
    // effectiveHeight=800 from the conservative-fallback branch.
    expect(result.current.grid.startRow).toBe(0);
    // visibleItems should be a contiguous prefix.
    const indices = result.current.grid.visibleItems.map((v) => v.index);
    expect(indices[0]).toBe(0);
    // Indices monotonic.
    for (let i = 1; i < indices.length; i++) {
      expect(indices[i]).toBe(indices[i - 1] + 1);
    }
  });

  it('visibleItems carry top + left positioning info', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(6), rowHeight: 100, columnCount: 3 }),
    );
    const first = result.current.grid.visibleItems[0];
    expect(first).toMatchObject({
      item: 'item-0',
      index: 0,
      top: 0,
    });
    // left is "0.0000%" formatted (rounded to 4 decimals).
    expect(first.left).toBe('0.0000%');
  });

  it('visibleItems left positions are evenly spaced across columns', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(3), rowHeight: 100, columnCount: 3 }),
    );
    const lefts = result.current.grid.visibleItems.map((v) => v.left);
    expect(lefts).toEqual([
      '0.0000%',
      '33.3333%',
      '66.6667%',
    ]);
  });

  it('visibleItems top advances by rowHeight per row', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(6), rowHeight: 100, columnCount: 3 }),
    );
    const tops = Array.from(
      new Set(result.current.grid.visibleItems.map((v) => v.top)),
    );
    expect(tops).toEqual([0, 100]);
  });

  // ─────────────────────────────────────────────────────────────────
  // Defensive math (Math.max(1, …))
  // ─────────────────────────────────────────────────────────────────

  it('clamps columnCount=0 to 1 (defensive — every item becomes its own row)', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(3), rowHeight: 100, columnCount: 0 }),
    );
    // safeColumnCount=1 → 3 rows of 1.
    expect(result.current.grid.totalHeight).toBe(300);
  });

  it('clamps rowHeight=0 to 1 (defensive — totalHeight = rowCount × 1)', () => {
    const { result } = renderHook(() =>
      useGridHarness({ items: makeItems(5), rowHeight: 0, columnCount: 2 }),
    );
    // safeRowHeight=1 → 3 rows × 1 = 3.
    expect(result.current.grid.totalHeight).toBe(3);
  });

  // ─────────────────────────────────────────────────────────────────
  // Window bounds — endRow caps at last row
  // ─────────────────────────────────────────────────────────────────

  it('endRow capped at last row (does not over-render past items)', () => {
    const { result } = renderHook(() =>
      useGridHarness({
        items: makeItems(6),
        rowHeight: 100,
        columnCount: 3,
        // 6 items / 3 cols = 2 rows. clientHeight=800 + overscan=2 would
        // ask for row 9, but the cap brings it back to row 1 (last).
      }),
    );
    expect(result.current.grid.endRow).toBe(1);
    expect(result.current.grid.visibleItems).toHaveLength(6);
  });

  it('startRow never goes negative (overscan can ask for row -2 at scrollTop=0)', () => {
    const { result } = renderHook(() =>
      useGridHarness({
        items: makeItems(60),
        rowHeight: 100,
        columnCount: 3,
        overscan: 5,
      }),
    );
    expect(result.current.grid.startRow).toBe(0);
  });

  // ─────────────────────────────────────────────────────────────────
  // Returned items match input by reference
  // ─────────────────────────────────────────────────────────────────

  it('returned visibleItems[].item references same objects as input array', () => {
    const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    const { result } = renderHook(() =>
      useGridHarness({ items, rowHeight: 100, columnCount: 3 }),
    );
    expect(result.current.grid.visibleItems[0].item).toBe(items[0]);
    expect(result.current.grid.visibleItems[1].item).toBe(items[1]);
    expect(result.current.grid.visibleItems[2].item).toBe(items[2]);
  });

  // ─────────────────────────────────────────────────────────────────
  // ResizeObserver-absent fallback
  // ─────────────────────────────────────────────────────────────────

  it('no crash when ResizeObserver is undefined (older-browser fallback)', () => {
    delete (globalThis as any).ResizeObserver;
    expect(() => {
      renderHook(() =>
        useGridHarness({ items: makeItems(3), rowHeight: 100, columnCount: 3 }),
      );
    }).not.toThrow();
  });
});

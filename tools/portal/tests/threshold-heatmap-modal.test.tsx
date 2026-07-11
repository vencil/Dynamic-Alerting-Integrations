/**
 * Threshold-heatmap detail-modal a11y wiring — portal ROI refactor Wave 1.
 *
 * Pins the useModalFocusTrap integration on the detailCell dialog:
 *   - clicking a heatmap cell opens the dialog
 *   - the dialog container auto-focuses on open (tabIndex={-1} + hook)
 *   - Escape closes it (hook calls setDetailCell(null))
 *   - the existing ✕ / Close buttons keep working
 *
 * The component reads window.__PLATFORM_DATA at module-eval time, so the
 * stub is installed BEFORE the dynamic import (static import would
 * evaluate the module too early).
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

let ThresholdHeatmap: React.ComponentType;

beforeAll(async () => {
  (window as any).__PLATFORM_DATA = {
    packOrder: ['testpack'],
    rulePacks: {
      testpack: {
        label: 'Test Pack',
        category: 'database',
        defaults: { test_metric: { value: 80, unit: '%', desc: 'test threshold' } },
        metrics: ['test_metric'],
      },
    },
  };
  ThresholdHeatmap = (await import('../src/interactive/tools/threshold-heatmap.jsx')).default;
});

afterAll(() => {
  delete (window as any).__PLATFORM_DATA;
});

describe('ThresholdHeatmap detail modal — focus-trap wiring', () => {
  it('opens the dialog on cell click, auto-focuses it, and Escape closes it', () => {
    render(<ThresholdHeatmap />);

    expect(screen.queryByRole('dialog')).toBeNull();

    const cells = screen.getAllByRole('gridcell');
    expect(cells.length).toBeGreaterThan(0);
    fireEvent.click(cells[0]);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    // useModalFocusTrap auto-focuses the dialog container on open.
    expect(document.activeElement).toBe(dialog);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('Escape is inert while no dialog is open (listener removed on close)', () => {
    render(<ThresholdHeatmap />);
    expect(() => fireEvent.keyDown(document, { key: 'Escape' })).not.toThrow();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('the existing ✕ close button still closes the dialog', () => {
    render(<ThresholdHeatmap />);
    fireEvent.click(screen.getAllByRole('gridcell')[0]);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByText('✕'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

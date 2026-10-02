/*
 * Shadow DOM styles for the termoweb-local schedule card.
 */

export const SCHEDULE_CARD_STYLES = `
  [hidden] { display: none !important; }
  :host { display: block; }
  ha-card { padding: 16px; box-sizing: border-box; font-size: calc(1em - 1px); }
  .header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
  .title { font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); }
  .note { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 4px; }
  .error { font-size: 0.85em; color: var(--error-color, #db4437); margin-top: 8px; }
  .legend { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
  .swatch { display: flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 16px;
    cursor: pointer; background: var(--secondary-background-color, rgba(127,127,127,0.1)); }
  .swatch .dot { width: 14px; height: 14px; border-radius: 50%; flex: none; }
  .swatch .label { font-size: 0.9em; color: var(--primary-text-color); }
  .legend-caption { margin-top: 0; margin-bottom: 12px; }
  .swatch-view { align-items: center; gap: 6px; }
  .swatch:not(.editing) .swatch-view { display: flex; }
  .swatch.editing .swatch-view { display: none; }
  .swatch-stepper { align-items: center; gap: 4px; }
  .swatch.editing .swatch-stepper { display: flex; }
  .swatch:not(.editing) .swatch-stepper { display: none; }
  .preset-editor-value { font-size: 0.9em; color: var(--primary-text-color); min-width: 3.5em; text-align: center; }
  .preset-step, .preset-confirm, .preset-cancel { font-size: 0.9em; width: 24px; height: 24px;
    border-radius: 50%; border: none; cursor: pointer;
    background: var(--secondary-background-color, rgba(127,127,127,0.2)); color: var(--primary-text-color); }
  .preset-confirm { color: var(--success-color, #2e7d32); }
  .preset-cancel { color: var(--error-color, #db4437); }
  .preset-step:disabled, .preset-confirm:disabled, .preset-cancel:disabled { opacity: 0.5; cursor: default; }
  .preset-editor-error { margin-top: 0; margin-bottom: 12px; }
  .grid-scroll { overflow-x: auto; }
  .grid { display: table; border-collapse: separate; border-spacing: 2px; }
  .grid-row { display: table-row; }
  .row-label { display: table-cell; vertical-align: middle; padding-right: 8px;
    font-size: 0.85em; color: var(--secondary-text-color); white-space: nowrap; position: sticky; left: 0;
    background: var(--card-background-color); }
  .cell { display: table-cell; width: 14px; height: 22px; min-width: 14px; cursor: pointer;
    border-radius: 2px; border: 1px solid var(--divider-color); touch-action: none; pointer-events: auto; }
  .cell.unset { background-image: repeating-linear-gradient(45deg, transparent, transparent 3px, rgba(0,0,0,0.15) 3px, rgba(0,0,0,0.15) 6px); }
  .cell:focus { outline: 2px solid var(--primary-color); outline-offset: -2px; }
  .row-actions { display: table-cell; padding-left: 8px; vertical-align: middle; }
  .copy-row-btn { font-size: 0.75em; color: var(--primary-color); background: none; border: none; cursor: pointer; padding: 2px 4px; }
  .copy-confirm, .copy-cancel { font-size: 0.9em; width: 22px; height: 22px; border-radius: 50%; border: none; cursor: pointer;
    background: var(--secondary-background-color, rgba(127,127,127,0.2)); }
  .copy-confirm { color: var(--success-color, #2e7d32); }
  .copy-cancel { color: var(--error-color, #db4437); margin-left: 2px; }
  .copy-target-checkbox { cursor: pointer; width: 16px; height: 16px; }
  .hour-labels { display: table-row; }
  .hour-label-spacer { display: table-cell; }
  .hour-label { display: table-cell; font-size: 0.7em; color: var(--secondary-text-color); text-align: left; }
  .actions { display: flex; gap: 8px; margin-top: 14px; align-items: center; }
  button.action { font-size: 0.9em; padding: 6px 14px; border-radius: 4px; border: none; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, #fff); }
  button.action.secondary { background: var(--secondary-background-color, rgba(127,127,127,0.2)); color: var(--primary-text-color); }
  button.action:disabled { opacity: 0.5; cursor: default; }
  .dirty-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--primary-color); display: inline-block; margin-left: 6px; }
  .copy-all-btn { font-size: 0.8em; color: var(--primary-color); background: none; border: 1px solid var(--divider-color);
    border-radius: 4px; cursor: pointer; padding: 4px 8px; }
`;

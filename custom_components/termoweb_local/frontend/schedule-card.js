/*
 * termoweb-local-schedule-card
 *
 * Weekly heater schedule editor (plans/schedule-card.md WP2/WP3). Vanilla
 * custom element, no framework, no build step: this module is served as-is
 * by the termoweb_local integration (see custom_components/termoweb_local/
 * __init__.py, _async_register_frontend) via the
 * termoweb-local-schedule-card.js entry module.
 */

import "./schedule-card-editor.js";
import { PRESETS, PRESET_BY_CODE, PRESET_STEP_C, UNSET_COLOR, presetBounds } from "./presets.js";
import { SCHEDULE_CARD_STYLES } from "./schedule-styles.js";
import {
  DAY_LABELS,
  DAYS_PER_WEEK,
  arraysEqual,
  copyDayToDays,
  formatHourLabel,
  formatTemp,
  hoursPerSlotFor,
  nextPresetValue,
  slotsPerDayFor,
} from "./schedule-grid.js";

const STALE_NOTE_TEXT =
  "The heater's schedule changed underneath your unsaved edits. Revert to load the latest, or Save to overwrite it.";

function isClimateEntityId(id) {
  return typeof id === "string" && id.startsWith("climate.");
}

class TermowebLocalScheduleCard extends HTMLElement {
  // HA custom-card contract: entities/entitiesFallback are entity id lists
  // the dashboard editor already knows about; hass.states is the fallback
  // when neither is populated yet (developers.home-assistant.io/docs/
  // frontend/custom-ui/custom-card#custom-card-preview-and-config).
  static getStubConfig(hass, entities, entitiesFallback) {
    const pool =
      (Array.isArray(entities) && entities.length && entities) ||
      (Array.isArray(entitiesFallback) && entitiesFallback.length && entitiesFallback) ||
      (hass && hass.states ? Object.keys(hass.states) : []);

    const marked = pool.find((id) => {
      if (!isClimateEntityId(id) || !hass || !hass.states || !hass.states[id]) return false;
      const attrs = hass.states[id].attributes || {};
      return attrs.dev_id !== undefined && Array.isArray(attrs.prog);
    });
    if (marked) return { entity: marked };

    const anyClimate = pool.find(isClimateEntityId);
    if (anyClimate) return { entity: anyClimate };

    return { entity: "" };
  }

  static getConfigElement() {
    return document.createElement("termoweb-local-schedule-card-editor");
  }

  setConfig(config) {
    if (config && config.entity && !isClimateEntityId(config.entity)) {
      throw new Error("termoweb-local-schedule-card: 'entity' must be a climate entity");
    }
    // setConfig runs before hass is set (custom-card contract), and the
    // picker preview calls it with no entity at all: never throw for that,
    // render a "select a heater" message instead.
    this._config = config || {};
    this._grid = null;
    this._domGrid = null;
    this._baselineProg = null;
    this._dirty = false;
    this._saving = false;
    this._error = null;
    this._staleNote = false;
    this._ptemp = null;
    this._legendPtemp = null;
    this._editingPreset = null;
    this._presetEditValue = null;
    this._presetSaving = false;
    this._presetError = null;
    this._copySourceDay = null;
    this._copyTargets = new Set();
    this._entityMissing = false;
    this._renderedSlotsPerDay = null;
    if (this._hass) {
      this._syncFromHass();
    }
    this._render();
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._syncFromHass();
    this._render();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    this._buildSkeleton();
    if (!this._docPointerUpBound) {
      // Bound once at the document level: a drag can end with the pointer
      // released outside the grid (or outside the card entirely), and a
      // listener scoped to the grid container alone would never see that.
      this._docPointerUpBound = true;
      const stopPaint = () => this._stopPainting();
      document.addEventListener("pointerup", stopPaint);
      document.addEventListener("pointercancel", stopPaint);
    }
    this._render();
  }

  _syncFromHass() {
    if (!this._config || !this._config.entity || !this._hass) return;
    const state = this._hass.states[this._config.entity];
    if (!state) {
      this._entityMissing = true;
      return;
    }
    this._entityMissing = false;
    const liveProg = Array.isArray(state.attributes.prog) ? state.attributes.prog.slice() : null;
    const ptemp = Array.isArray(state.attributes.ptemp) ? state.attributes.ptemp.slice() : null;
    this._ptemp = ptemp;
    this._liveProg = liveProg;

    if (this._grid === null) {
      this._resetGridFromLive();
      return;
    }
    if (!this._dirty) {
      if (!arraysEqual(liveProg, this._baselineProg)) {
        this._resetGridFromLive();
      }
      this._staleNote = false;
      return;
    }
    this._staleNote = !!(
      liveProg &&
      this._baselineProg &&
      !arraysEqual(liveProg, this._baselineProg)
    );
  }

  _resetGridFromLive() {
    this._baselineProg = this._liveProg ? this._liveProg.slice() : null;
    this._grid = this._liveProg ? this._liveProg.slice() : null;
    this._dirty = false;
    this._staleNote = false;
  }

  _buildSkeleton() {
    const style = document.createElement("style");
    style.textContent = SCHEDULE_CARD_STYLES;
    const card = document.createElement("ha-card");
    const content = document.createElement("div");
    content.className = "content";
    card.appendChild(content);
    this.shadowRoot.innerHTML = "";
    this.shadowRoot.appendChild(style);
    this.shadowRoot.appendChild(card);
    this._content = content;
    // A fresh skeleton has no grid built yet: force the next _render() to
    // go through the structural path rather than try to patch nodes that
    // no longer exist.
    this._renderedSlotsPerDay = null;
  }

  _render() {
    if (!this._content) return;

    const entity = this._config && this._config.entity;
    if (!entity) {
      this._renderPlaceholder("Select a heater to see its schedule.");
      return;
    }
    if (!this._hass) {
      // setConfig ran before hass was set: nothing more to show yet.
      this._renderPlaceholder("Waiting for Home Assistant...");
      return;
    }
    if (this._entityMissing) {
      this._renderPlaceholder(`Entity ${entity} not found.`, "error");
      return;
    }
    if (!this._grid) {
      this._renderPlaceholder("Waiting for schedule data...");
      return;
    }

    const slotsPerDay = slotsPerDayFor(this._grid.length);
    if (this._renderedSlotsPerDay !== slotsPerDay) {
      this._renderStructural(slotsPerDay);
    } else {
      this._patchInPlace();
    }
  }

  // Used for the non-grid states (no entity, no hass yet, missing entity,
  // no schedule data). None of these have a grid-scroll to lose scroll
  // position on, so a plain innerHTML swap is fine here.
  _renderPlaceholder(text, kind = "note") {
    this._content.innerHTML = `<div class="${kind}">${text}</div>`;
    this._renderedSlotsPerDay = null;
    this._gridEl = null;
  }

  _renderStructural(slotsPerDay) {
    const hoursPerSlot = hoursPerSlotFor(slotsPerDay);
    const title = this._config.title || "Weekly schedule";

    // Preserve horizontal scroll across a structural rebuild: HA pushes a
    // new `hass` object many times a minute, and without this the grid
    // container is a brand-new element on every push, resetting scrollLeft
    // to 0 under the user.
    const prevScroll = this._content.querySelector(".grid-scroll");
    const savedScrollLeft = prevScroll ? prevScroll.scrollLeft : 0;

    this._content.innerHTML = "";

    const header = document.createElement("div");
    header.className = "header";
    const titleEl = document.createElement("div");
    titleEl.className = "title";
    titleEl.textContent = title;
    header.appendChild(titleEl);
    this._content.appendChild(header);
    this._titleEl = titleEl;

    const staleNoteEl = document.createElement("div");
    staleNoteEl.className = "note stale-note";
    staleNoteEl.hidden = true;
    this._content.appendChild(staleNoteEl);
    this._staleNoteEl = staleNoteEl;

    const legend = document.createElement("div");
    legend.className = "legend";
    legend.setAttribute("role", "group");
    legend.setAttribute("aria-label", "Preset temperatures");
    this._legendLabelEls = {};
    this._presetEls = {};
    PRESETS.forEach((preset) => {
      const swatch = document.createElement("div");
      swatch.className = "swatch";
      swatch.dataset.preset = String(preset.code);
      swatch.setAttribute("role", "button");
      swatch.tabIndex = 0;
      swatch.setAttribute("aria-label", `Edit ${preset.label} temperature`);

      // Colour dot: stays outside both the view and the stepper so it is
      // never hidden by either -- while editing it is the only part of the
      // view that still shows, sitting left of the stepper's minus button.
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.backgroundColor = preset.color;
      swatch.appendChild(dot);

      // View state: label only, shown whenever this preset is not the one
      // being edited.
      const view = document.createElement("span");
      view.className = "swatch-view";
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = this._legendLabelText(preset);
      view.appendChild(label);
      swatch.appendChild(view);

      // Edit state: the swatch itself turns into the stepper in place
      // (owner direction) rather than opening a separate control elsewhere
      // in the card, so it and the view state share the one swatch element.
      // Visibility is driven by the swatch's own `editing` class in
      // schedule-styles.js (author `display` rules there would otherwise
      // override the UA `[hidden]` default); `hidden` is still set to keep
      // the non-visible half out of the accessibility tree.
      const stepper = document.createElement("span");
      stepper.className = "swatch-stepper";
      stepper.hidden = true;

      const minusBtn = document.createElement("button");
      minusBtn.type = "button";
      minusBtn.className = "preset-step";
      minusBtn.textContent = "−";
      minusBtn.setAttribute("aria-label", `Decrease ${preset.label} by ${PRESET_STEP_C}`);
      minusBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._stepPreset(-PRESET_STEP_C);
      });
      stepper.appendChild(minusBtn);

      const valueEl = document.createElement("span");
      valueEl.className = "preset-editor-value";
      stepper.appendChild(valueEl);

      const plusBtn = document.createElement("button");
      plusBtn.type = "button";
      plusBtn.className = "preset-step";
      plusBtn.textContent = "+";
      plusBtn.setAttribute("aria-label", `Increase ${preset.label} by ${PRESET_STEP_C}`);
      plusBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._stepPreset(PRESET_STEP_C);
      });
      stepper.appendChild(plusBtn);

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "preset-confirm";
      confirmBtn.textContent = "✓";
      confirmBtn.setAttribute("aria-label", "confirm");
      confirmBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._savePreset();
      });
      stepper.appendChild(confirmBtn);

      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "preset-cancel";
      cancelBtn.textContent = "✗";
      cancelBtn.setAttribute("aria-label", "cancel");
      cancelBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._closePresetEditor();
      });
      stepper.appendChild(cancelBtn);

      swatch.appendChild(stepper);

      swatch.addEventListener("click", () => this._openPresetEditor(preset.code));
      swatch.addEventListener("keydown", (ev) => this._onSwatchKeyDown(ev, preset.code));
      legend.appendChild(swatch);
      this._legendLabelEls[preset.code] = label;
      this._presetEls[preset.code] = {
        swatch,
        dot,
        view,
        stepper,
        minusBtn,
        valueEl,
        plusBtn,
        confirmBtn,
        cancelBtn,
      };
    });
    this._content.appendChild(legend);

    const caption = document.createElement("div");
    caption.className = "note legend-caption";
    caption.textContent = "Click a preset to edit its temperature.";
    this._content.appendChild(caption);

    const presetError = document.createElement("div");
    presetError.className = "error preset-editor-error";
    presetError.hidden = true;
    this._content.appendChild(presetError);
    this._presetErrorEl = presetError;

    const scroll = document.createElement("div");
    scroll.className = "grid-scroll";
    const grid = document.createElement("div");
    grid.className = "grid";
    grid.setAttribute("role", "grid");

    const hourRow = document.createElement("div");
    hourRow.className = "hour-labels";
    const spacer = document.createElement("div");
    spacer.className = "hour-label-spacer";
    hourRow.appendChild(spacer);
    const labelStep = slotsPerDay > 24 ? 6 : 2; // every 3h at half-hourly, every 2h at hourly
    for (let col = 0; col < slotsPerDay; col++) {
      const cell = document.createElement("div");
      cell.className = "hour-label";
      if (col % labelStep === 0) {
        cell.textContent = String(Math.floor(col * hoursPerSlot));
      }
      hourRow.appendChild(cell);
    }
    const spacer2 = document.createElement("div");
    spacer2.className = "hour-label-spacer";
    hourRow.appendChild(spacer2);
    grid.appendChild(hourRow);

    this._rowEls = {};
    for (let day = 0; day < DAYS_PER_WEEK; day++) {
      const row = document.createElement("div");
      row.className = "grid-row";
      row.setAttribute("role", "row");
      const label = document.createElement("div");
      label.className = "row-label";
      label.textContent = DAY_LABELS[day];
      row.appendChild(label);

      for (let col = 0; col < slotsPerDay; col++) {
        const idx = day * slotsPerDay + col;
        const cell = document.createElement("div");
        cell.dataset.idx = String(idx);
        cell.dataset.day = String(day);
        cell.dataset.slot = String(col);
        cell.setAttribute("role", "gridcell");
        cell.tabIndex = 0;
        this._applyCellVisual(cell, this._grid[idx], hoursPerSlot);
        row.appendChild(cell);
      }

      const actions = document.createElement("div");
      actions.className = "row-actions";

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "copy-row-btn";
      copyBtn.textContent = "Copy to";
      copyBtn.addEventListener("click", () => this._startCopyMode(day));
      actions.appendChild(copyBtn);

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "copy-confirm";
      confirmBtn.textContent = "✓";
      confirmBtn.setAttribute("aria-label", "confirm copy");
      confirmBtn.hidden = true;
      confirmBtn.addEventListener("click", () => this._confirmCopy(slotsPerDay));
      actions.appendChild(confirmBtn);

      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "copy-cancel";
      cancelBtn.textContent = "✗";
      cancelBtn.setAttribute("aria-label", "cancel copy");
      cancelBtn.hidden = true;
      cancelBtn.addEventListener("click", () => this._cancelCopyMode());
      actions.appendChild(cancelBtn);

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "copy-target-checkbox";
      checkbox.setAttribute("aria-label", `copy to ${DAY_LABELS[day]}`);
      checkbox.hidden = true;
      checkbox.addEventListener("change", () => this._toggleCopyTarget(day, checkbox.checked));
      actions.appendChild(checkbox);

      row.appendChild(actions);
      this._rowEls[day] = { actions, copyBtn, confirmBtn, cancelBtn, checkbox };

      grid.appendChild(row);
    }

    // Single delegated listener set on the grid container, per spec, rather
    // than one per cell: pointerenter does not bubble, so "cell the pointer
    // just entered" is read from pointermove (which does bubble) instead.
    grid.addEventListener("pointerdown", (ev) => this._onCellPointerDown(ev, hoursPerSlot));
    grid.addEventListener("pointermove", (ev) => this._onCellPointerMove(ev, hoursPerSlot));
    grid.addEventListener("pointerup", () => this._stopPainting());
    grid.addEventListener("pointercancel", () => this._stopPainting());
    grid.addEventListener("keydown", (ev) => this._onCellKeyDown(ev, hoursPerSlot));

    scroll.appendChild(grid);
    this._content.appendChild(scroll);
    scroll.scrollLeft = savedScrollLeft;

    const errorEl = document.createElement("div");
    errorEl.className = "error save-error";
    errorEl.hidden = true;
    this._content.appendChild(errorEl);
    this._errorEl = errorEl;

    const actionsRow = document.createElement("div");
    actionsRow.className = "actions";

    const saveBtn = document.createElement("button");
    saveBtn.className = "action";
    saveBtn.addEventListener("click", () => this._save());
    actionsRow.appendChild(saveBtn);
    this._saveBtnEl = saveBtn;

    const revertBtn = document.createElement("button");
    revertBtn.className = "action secondary";
    revertBtn.textContent = "Revert";
    revertBtn.addEventListener("click", () => {
      this._resetGridFromLive();
      this._error = null;
      this._syncCellsFromGrid(hoursPerSlot);
      this._patchLegend();
      this._patchStaleNote();
      this._patchError();
      this._updateActionsState();
      this._updateDirtyDot();
    });
    actionsRow.appendChild(revertBtn);
    this._revertBtnEl = revertBtn;

    this._content.appendChild(actionsRow);

    this._gridEl = grid;
    this._renderedSlotsPerDay = slotsPerDay;
    this._domGrid = this._grid.slice();
    this._legendPtemp = this._ptemp ? this._ptemp.slice() : null;

    this._patchStaleNote();
    this._patchError();
    this._updateSaveButtonLabel();
    this._updateActionsState();
    this._updateDirtyDot();
    this._patchPresetStepper();
    this._patchCopyMode();
  }

  // Enter/Escape while the clicked swatch is the one being edited confirm
  // or cancel it, matching the confirm/cancel buttons inside its stepper;
  // otherwise Enter/Space open the editor, same as clicking it. Returning
  // after the editing branch (rather than falling through) stops a stray
  // Space on the swatch background from reseeding an in-progress edit.
  _onSwatchKeyDown(ev, code) {
    if (this._editingPreset === code) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        this._savePreset();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        this._closePresetEditor();
      }
      return;
    }
    if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
    ev.preventDefault();
    this._openPresetEditor(code);
  }

  _openPresetEditor(code) {
    // Null-safe: with no status snapshot yet there are no current values
    // for the other two presets to send alongside the edited one, so there
    // is nothing to open an editor onto.
    if (!this._ptemp) return;
    // Only one of the two "swatch turns into something else in place"
    // interactions can be mid-edit at once; opening this one cancels an
    // in-progress row copy (the reverse -- starting a copy while a preset
    // is being edited -- is not required, per owner direction).
    if (this._copySourceDay !== null) this._cancelCopyMode();
    this._editingPreset = code;
    this._presetEditValue = this._ptemp[code];
    this._presetError = null;
    this._patchPresetStepper();
  }

  _closePresetEditor() {
    if (this._presetSaving) return;
    this._editingPreset = null;
    this._presetEditValue = null;
    this._presetError = null;
    this._patchPresetStepper();
  }

  _stepPreset(delta) {
    if (this._editingPreset === null || this._presetSaving) return;
    const bounds = presetBounds(this._editingPreset, this._ptemp);
    const stepped = Math.round((this._presetEditValue + delta) * 2) / 2;
    this._presetEditValue = Math.min(bounds.max, Math.max(bounds.min, stepped));
    this._patchPresetStepper();
  }

  async _savePreset() {
    if (this._editingPreset === null || this._presetSaving) return;
    const code = this._editingPreset;
    const ptemp = this._ptemp ? this._ptemp.slice() : [null, null, null];
    ptemp[code] = this._presetEditValue;
    if (ptemp.some((value) => value === null || value === undefined)) {
      this._presetError = "No current preset values yet; try again once the heater has reported.";
      this._patchPresetStepper();
      return;
    }
    this._presetSaving = true;
    this._presetError = null;
    this._patchPresetStepper();
    try {
      await this._hass.callService("termoweb_local", "set_preset_temperatures", {
        entity_id: this._config.entity,
        cold: ptemp[0],
        night: ptemp[1],
        day: ptemp[2],
      });
      // Re-read the entity rather than trust `ptemp` blindly: the
      // coordinator's own post-command status request (docs/80-handover.md
      // list C item 10 step 1) may already have landed a real E6 in
      // `this._hass` by the time this call resolves.
      const state = this._hass.states[this._config.entity];
      this._ptemp =
        state && Array.isArray(state.attributes.ptemp) ? state.attributes.ptemp.slice() : ptemp;
      this._editingPreset = null;
      this._presetEditValue = null;
    } catch (err) {
      this._presetError =
        (err && (err.message || (err.body && err.body.message))) ||
        "set_preset_temperatures call failed";
      // Return to the view on failure too: this._ptemp is untouched above,
      // so the view renders the last-known-good value, not the rejected
      // edit, with the failure surfaced in the card's error area instead.
      this._editingPreset = null;
      this._presetEditValue = null;
    } finally {
      this._presetSaving = false;
      this._patchLegend();
      this._patchPresetStepper();
    }
  }

  _patchPresetStepper() {
    if (!this._presetEls) return;
    PRESETS.forEach((preset) => {
      const els = this._presetEls[preset.code];
      if (!els) return;
      const editing = this._editingPreset === preset.code;
      els.swatch.classList.toggle("editing", editing);
      els.view.hidden = editing;
      els.stepper.hidden = !editing;
      if (!editing) return;
      const bounds = presetBounds(preset.code, this._ptemp);
      els.valueEl.textContent = formatTemp(this._presetEditValue) || "--";
      els.minusBtn.disabled = this._presetSaving || this._presetEditValue <= bounds.min;
      els.plusBtn.disabled = this._presetSaving || this._presetEditValue >= bounds.max;
      els.confirmBtn.disabled = this._presetSaving;
      els.cancelBtn.disabled = this._presetSaving;
    });
    if (this._presetErrorEl) {
      this._presetErrorEl.hidden = !this._presetError;
      if (this._presetError) this._presetErrorEl.textContent = this._presetError;
    }
  }

  // Called for every `hass` push once the grid structure already exists
  // and its column count hasn't changed. Per spec: while the user has
  // unsaved edits, nothing in the DOM may change except the stale note --
  // the grid values shown are the user's own, not the (possibly newer)
  // live ones.
  _patchInPlace() {
    this._patchStaleNote();
    if (this._dirty) return;
    const hoursPerSlot = hoursPerSlotFor(this._renderedSlotsPerDay);
    this._syncCellsFromGrid(hoursPerSlot);
    this._patchLegend();
    this._updateActionsState();
    this._updateDirtyDot();
  }

  _legendLabelText(preset) {
    const temp = this._ptemp ? formatTemp(this._ptemp[preset.code]) : null;
    return temp ? `${preset.label} (${temp})` : preset.label;
  }

  _patchLegend() {
    if (arraysEqual(this._ptemp, this._legendPtemp)) return;
    this._legendPtemp = this._ptemp ? this._ptemp.slice() : null;
    PRESETS.forEach((preset) => {
      const label = this._legendLabelEls && this._legendLabelEls[preset.code];
      if (label) label.textContent = this._legendLabelText(preset);
    });
  }

  _patchStaleNote() {
    if (!this._staleNoteEl) return;
    this._staleNoteEl.hidden = !this._staleNote;
    if (this._staleNote) this._staleNoteEl.textContent = STALE_NOTE_TEXT;
  }

  _patchError() {
    if (!this._errorEl) return;
    this._errorEl.hidden = !this._error;
    if (this._error) this._errorEl.textContent = this._error;
  }

  _applyCellVisual(cell, value, hoursPerSlot) {
    const preset = PRESET_BY_CODE.get(value);
    cell.className = preset ? "cell" : "cell unset";
    cell.style.backgroundColor = preset ? preset.color : UNSET_COLOR;
    const day = Number(cell.dataset.day);
    const slot = Number(cell.dataset.slot);
    const timeLabel = formatHourLabel(slot * hoursPerSlot);
    const presetLabel = preset ? preset.label : "Unset";
    cell.setAttribute("aria-label", `${DAY_LABELS[day]} ${timeLabel}, ${presetLabel}`);
  }

  // Diffs this._grid against the last-painted snapshot and patches only the
  // cells that actually changed -- used both for hass-driven syncs (once
  // not dirty) and for local bulk edits (copy-to-all-days, revert).
  _syncCellsFromGrid(hoursPerSlot) {
    if (!this._gridEl || !this._domGrid) return;
    for (let idx = 0; idx < this._grid.length; idx++) {
      const value = this._grid[idx];
      if (this._domGrid[idx] !== value) {
        this._domGrid[idx] = value;
        this._paintCellDom(idx, value, hoursPerSlot);
      }
    }
  }

  _paintCellDom(idx, value, hoursPerSlot) {
    const cell = this._gridEl && this._gridEl.querySelector(`.cell[data-idx="${idx}"]`);
    if (cell) this._applyCellVisual(cell, value, hoursPerSlot);
  }

  _setCellValue(idx, value, hoursPerSlot) {
    if (this._grid[idx] === value) return;
    this._grid[idx] = value;
    this._domGrid[idx] = value;
    this._dirty = true;
    this._paintCellDom(idx, value, hoursPerSlot);
    this._updateActionsState();
    this._updateDirtyDot();
  }

  _cycleCell(idx, hoursPerSlot) {
    const next = nextPresetValue(this._grid[idx]);
    this._setCellValue(idx, next, hoursPerSlot);
    return next;
  }

  _onCellPointerDown(ev, hoursPerSlot) {
    const cell = ev.target.closest(".cell");
    if (!cell) return;
    ev.preventDefault();
    const idx = Number(cell.dataset.idx);
    this._painting = true;
    this._lastPaintedIdx = idx;
    this._paintValue = this._cycleCell(idx, hoursPerSlot);
  }

  _onCellPointerMove(ev, hoursPerSlot) {
    if (!this._painting) return;
    const cell = ev.target.closest(".cell");
    if (!cell) return;
    const idx = Number(cell.dataset.idx);
    if (idx === this._lastPaintedIdx) return;
    this._lastPaintedIdx = idx;
    this._setCellValue(idx, this._paintValue, hoursPerSlot);
  }

  _stopPainting() {
    this._painting = false;
    this._paintValue = null;
    this._lastPaintedIdx = null;
  }

  _onCellKeyDown(ev, hoursPerSlot) {
    if (ev.key === "Escape") {
      // row-actions (the "Copy to" / confirm-cancel / checkbox controls)
      // live inside .grid too, so this one delegated listener already sees
      // Escape from any of them without a separate document-level handler.
      this._cancelCopyMode();
      return;
    }
    if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
    const cell = ev.target.closest(".cell");
    if (!cell) return;
    ev.preventDefault();
    this._cycleCell(Number(cell.dataset.idx), hoursPerSlot);
  }

  _updateActionsState() {
    const hasUnset = this._grid.some((v) => v === null || v === undefined);
    if (this._saveBtnEl) this._saveBtnEl.disabled = this._saving || hasUnset || !this._dirty;
    if (this._revertBtnEl) this._revertBtnEl.disabled = this._saving || !this._dirty;
  }

  _updateSaveButtonLabel() {
    if (this._saveBtnEl) this._saveBtnEl.textContent = this._saving ? "Saving..." : "Save";
  }

  _updateDirtyDot() {
    if (!this._titleEl) return;
    const existing = this._titleEl.querySelector(".dirty-dot");
    if (this._dirty && !existing) {
      const dot = document.createElement("span");
      dot.className = "dirty-dot";
      dot.title = "Unsaved changes";
      this._titleEl.appendChild(dot);
    } else if (!this._dirty && existing) {
      existing.remove();
    }
  }

  // Copy mode: clicking "Copy to" on a row turns that row's button into
  // confirm/cancel and every other row's button into a checkbox. Only one
  // row's controls track state (this._copySourceDay / this._copyTargets);
  // _patchCopyMode() is the only thing that reads it into the DOM, so a
  // hass-driven _patchInPlace() (which never calls it) can't disturb an
  // in-progress copy.
  _startCopyMode(day) {
    this._copySourceDay = day;
    this._copyTargets = new Set();
    this._patchCopyMode();
  }

  _cancelCopyMode() {
    if (this._copySourceDay === null) return;
    this._copySourceDay = null;
    this._copyTargets = new Set();
    this._patchCopyMode();
  }

  _toggleCopyTarget(day, checked) {
    if (this._copySourceDay === null) return;
    if (checked) this._copyTargets.add(day);
    else this._copyTargets.delete(day);
  }

  _confirmCopy(slotsPerDay) {
    if (this._copySourceDay === null) return;
    if (this._copyTargets.size) {
      this._grid = copyDayToDays(this._grid, this._copySourceDay, this._copyTargets, slotsPerDay);
      this._dirty = true;
      this._syncCellsFromGrid(hoursPerSlotFor(slotsPerDay));
      this._updateActionsState();
      this._updateDirtyDot();
    }
    this._cancelCopyMode();
  }

  _patchCopyMode() {
    if (!this._rowEls) return;
    const active = this._copySourceDay !== null;
    for (let day = 0; day < DAYS_PER_WEEK; day++) {
      const els = this._rowEls[day];
      if (!els) continue;
      const isSource = active && this._copySourceDay === day;
      els.copyBtn.hidden = active;
      els.confirmBtn.hidden = !isSource;
      els.cancelBtn.hidden = !isSource;
      els.checkbox.hidden = !active || isSource;
      if (!active) els.checkbox.checked = false;
    }
  }

  async _save() {
    if (this._saving) return;
    this._saving = true;
    this._error = null;
    this._updateActionsState();
    this._updateSaveButtonLabel();
    this._patchError();
    try {
      await this._hass.callService("termoweb_local", "set_schedule", {
        entity_id: this._config.entity,
        prog: this._grid.slice(),
      });
      this._baselineProg = this._grid.slice();
      this._dirty = false;
      this._staleNote = false;
    } catch (err) {
      this._error =
        (err && (err.message || (err.body && err.body.message))) || "set_schedule call failed";
    } finally {
      this._saving = false;
      this._updateActionsState();
      this._updateSaveButtonLabel();
      this._updateDirtyDot();
      this._patchStaleNote();
      this._patchError();
    }
  }
}

// Guard against a second define: the module build and the es5 bundle both
// load on the same page for some browsers during the isModern transition,
// and redefining a custom element name throws.
if (!customElements.get("termoweb-local-schedule-card")) {
  customElements.define("termoweb-local-schedule-card", TermowebLocalScheduleCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "termoweb-local-schedule-card",
  name: "Termoweb Local schedule card",
  description: "Weekly heater schedule editor for the termoweb_local integration.",
  preview: true,
});

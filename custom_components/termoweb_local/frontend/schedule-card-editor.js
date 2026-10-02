/*
 * Visual editor for the termoweb-local schedule card.
 *
 * Standard core-card editor pattern: an ha-form fed a schema, re-emitting
 * its own "value-changed" as a "config-changed" event carrying the whole
 * config (https://developers.home-assistant.io/docs/frontend/custom-ui/
 * custom-card#configuration-editor).
 */

const SCHEMA = [
  {
    name: "entity",
    required: true,
    selector: { entity: { domain: "climate", integration: "termoweb_local" } },
  },
  { name: "title", selector: { text: {} } },
];

const LABELS = { entity: "Heater", title: "Title" };

function computeLabel(schema) {
  return LABELS[schema.name] || schema.name;
}

class TermowebLocalScheduleCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._render();
  }

  _render() {
    // setConfig can run before hass is set (custom-card contract): nothing
    // to render yet, ha-form needs hass to draw the entity picker.
    if (!this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...this._config, ...ev.detail.value };
        this._config = config;
        this.dispatchEvent(
          new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true })
        );
      });
      this.innerHTML = "";
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = SCHEMA;
    this._form.data = this._config;
    this._form.computeLabel = computeLabel;
  }
}

// Guard against a second define: the module build and the es5 bundle both
// load on the same page for some browsers during the isModern transition,
// and redefining a custom element name throws.
if (!customElements.get("termoweb-local-schedule-card-editor")) {
  customElements.define("termoweb-local-schedule-card-editor", TermowebLocalScheduleCardEditor);
}

// FACT-5595: use the full (eval) Alpine build instead of @alpinejs/csp.
// The @alpinejs/csp build cannot evaluate the observability templates' expressions
// (inline x-data with async methods, template literals, optional chaining, window-global
// x-data factories), which left the observability dashboard permanently broken after the
// #4676 CSP migration. The shipped CSP already allows eval (script-src 'unsafe-eval', kept
// for HTMX), so the full build runs within the existing policy. The nonce-based
// script-src-elem remains the primary XSS defense. Upstream fix = convert those templates
// to CSP-safe form; tracked separately.
import Alpine from 'alpinejs';
import { buildTableUrl, syncCheckboxFromUrl } from './utils.js';
import { appRoot } from './components/app-root.js';
import { mainLayout } from './components/main-layout.js';
import { overflowMenu } from './components/overflow-menu.js';
import { teamSelector } from './components/team-selector.js';
import { maintenancePanel } from './components/maintenance-panel.js';

Alpine.data('appRoot', appRoot);
Alpine.data('mainLayout', mainLayout);
Alpine.data('overflowMenu', overflowMenu);
Alpine.data('teamSelector', teamSelector);
Alpine.data('maintenancePanel', maintenancePanel);

Alpine.magic('syncCheckbox', function () {
  return syncCheckboxFromUrl;
});

Alpine.magic('tableHxGet', function (el) {
  return function (tableName, baseUrl, checkboxId, defaultChecked, extraParams) {
    const checkbox = document.getElementById(checkboxId);
    const checked = checkbox !== null ? checkbox.checked : defaultChecked;
    const params = { include_inactive: String(checked) };
    if (extraParams) Object.assign(params, extraParams);
    el.setAttribute('hx-get', buildTableUrl(tableName, baseUrl, params));
  };
});

export default Alpine;

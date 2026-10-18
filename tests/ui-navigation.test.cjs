// Simulate all five navigation taps with the actual switchTab function.
// Runs under plain Node.js with no browser dependency.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('touch navigation activates exactly one page and matching button', () => {
  const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
  const cut = source.indexOf('let lastState=null;');
  assert.ok(cut > 0, 'navigation must be declared before chart state');
  const tabs = ['dashboard', 'connections', 'sync', 'logs', 'security'];

  function node(tab) {
    const classes = new Set(tab === 'dashboard' ? ['active'] : ['hidden']);
    const attributes = new Map();
    return {
      dataset: {panel: tab, tab},
      classList: {
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
        },
        contains(name) { return classes.has(name); },
      },
      setAttribute(name, value) { attributes.set(name, value); },
      removeAttribute(name) { attributes.delete(name); },
      getAttribute(name) { return attributes.get(name); }
    };
  }

  const panels = tabs.map(node);
  const buttons = tabs.map(node);
  const crumb = {textContent: ''};
  let scrollCalls = 0;

  const ctx = {
    window: {
      location: {protocol: 'https:', pathname: '/'},
      matchMedia: () => ({matches: true}),
      scrollTo: () => { scrollCalls++; }
    },
    document: {
      querySelector: selector => selector === '#crumb' ? crumb : null,
      querySelectorAll: selector => {
        if (selector === '.panel') return panels;
        if (selector === '.navitem') return buttons;
        return [];
      }
    },
    console, Intl, Number, String,
  };

  // Execute the genuine implementation, not a duplicate of its logic.
  vm.runInNewContext(source.slice(0, cut) + '\nlet lastState=null;', ctx);

  for (const tab of tabs) {
    vm.runInNewContext('switchTab(' + JSON.stringify(tab) + ')', ctx);
    assert.deepEqual(
      panels.filter(x => !x.classList.contains('hidden')).map(x => x.dataset.panel),
      [tab],
      'visible panel after tapping ' + tab,
    );
    assert.deepEqual(
      buttons.filter(x => x.classList.contains('active')).map(x => x.dataset.tab),
      [tab],
      'selected button after tapping ' + tab,
    );
    assert.equal(
      buttons.find(x => x.dataset.tab === tab).getAttribute('aria-current'),
      'page',
    );
    assert.ok(crumb.textContent.length > 0);
  }
  assert.equal(scrollCalls, 5);
});
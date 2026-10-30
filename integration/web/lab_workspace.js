/* View navigation and read-only DOM mirrors. No API calls or experiment mutations. */
(function () {
  'use strict';
  const byId = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('[data-workspace-tab]')];
  const views = [...document.querySelectorAll('[data-workspace-view]')];
  const modelSelector = byId('experiment-model');
  const modelDescription = byId('model-choice-description');
  function setModel(model) {
    if (modelSelector) modelSelector.value = model;
    const redox = byId('redox-couple');
    if (redox) redox.hidden = model === 'oxidation';
    const destinations = {workbench:'workspace-hmf', validation:'workspace-hmf-validation', analysis:'workspace-hmf-analysis', batch:'workspace-hmf-batch', history:'workspace-hmf-history'};
    for (const tab of tabs) {
      const key = tab.dataset.workspaceTab;
      tab.setAttribute('aria-controls', model === 'oxidation' ? destinations[key] : 'workspace-' + key);
    }
    if (modelDescription) modelDescription.textContent = model === 'oxidation'
      ? '电催化氧化 · HMF 本地数值实验' : '浓差电池 · 云端节点实验条件';
  }
  function selectView(name, focus = false) {
    if (!views.some(view => view.dataset.workspaceView === name)) return;
    for (const tab of tabs) {
      const selected = tab.dataset.workspaceTab === name;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    }
    for (const view of views) {
      const modelMismatch = view.dataset.modelView && view.dataset.modelView !== (modelSelector?.value || 'concentration');
      view.hidden = view.dataset.workspaceView !== name || Boolean(modelMismatch);
    }
    // Existing chart controller redraws to its now-visible container width.
    window.dispatchEvent(new Event('resize'));
  }
  modelSelector?.addEventListener('change', () => { setModel(modelSelector.value); selectView('workbench'); });
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectView(tab.dataset.workspaceTab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectView(tabs[next].dataset.workspaceTab, true);
    });
  });
  const mediaButtons = [...document.querySelectorAll('[data-media-mode]')];
  function selectMedia(name) {
    mediaButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mediaMode === name)));
    document.querySelectorAll('[data-media-panel]').forEach(panel => { panel.hidden = panel.dataset.mediaPanel !== name; });
  }
  mediaButtons.forEach(button => button.addEventListener('click', () => selectMedia(button.dataset.mediaMode)));
  byId('load-video')?.addEventListener('click', () => selectMedia('playback'));
  byId('live-preview-follow')?.addEventListener('click', () => selectMedia('live'));
  // Keep existing controllers' fields authoritative. Mirrors never contain invented results.
  const voltage = byId('voltage'), mirror = byId('workspace-voltage');
  const copyVoltage = () => { if (mirror && voltage) mirror.textContent = voltage.textContent; };
  if (voltage && mirror) {
    new MutationObserver(copyVoltage).observe(voltage, {childList:true, characterData:true, subtree:true});
    copyVoltage();
  }
  const canvas = byId('live-preview-canvas'), empty = byId('workspace-preview-empty');
  const updatePreviewEmpty = () => { if (empty && canvas) empty.hidden = !canvas.hidden; };
  if (canvas && empty) {
    new MutationObserver(updatePreviewEmpty).observe(canvas, {attributes:true, attributeFilter:['hidden']});
    updatePreviewEmpty();
  }
  document.querySelectorAll('[data-open-connection]').forEach(button => button.addEventListener('click', () => {
    byId('connection-settings').open = true;
    byId('endpoint').focus();
  }));
  // Native disclosures keep their state during data polling and tab changes.
  setModel(modelSelector?.value || 'concentration');
  selectView('workbench');
  selectMedia('live');
})();

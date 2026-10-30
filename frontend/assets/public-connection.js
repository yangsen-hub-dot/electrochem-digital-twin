/* Connection configuration only. Experiment controllers retain state ownership. */
'use strict';
(() => {
  const field = document.getElementById('endpoint');
  const token = document.getElementById('video-access-code');
  const media = document.getElementById('video-endpoint');
  const params = new URLSearchParams(location.search);
  let selected = null;
  try {
    const profiles = JSON.parse(localStorage.getItem('electrochemistry-node-profiles-v1') || '[]');
    selected = profiles.find(p => p.id === localStorage.getItem('edt-selected-node')) || null;
    if (selected) {
      field.value = selected.endpoint;
      token.value = sessionStorage.getItem('edt-node-token:' + selected.id) || '';
      if (selected.video_endpoint) media.value = selected.video_endpoint;
      field.dispatchEvent(new Event('input', {bubbles:true}));
    } else if (location.protocol === 'https:') {
      field.value = ''; media.value = '';
    }
  } catch { /* Private browsing can disable storage; manual configuration remains. */ }
  const model = document.getElementById('experiment-model');
  const wanted = params.get('model');
  if (['concentration', 'oxidation'].includes(wanted)) {
    model.value = wanted; model.dispatchEvent(new Event('change', {bubbles:true}));
  }
  const view = params.get('view');
  if (['workbench','validation','analysis','batch','history'].includes(view)) document.querySelector('[data-workspace-tab="' + view + '"]')?.click();
  window.ElectrochemPublicConnection = {
    hmf() {
      let endpoint = field.value.trim().replace(/\/$/, '');
      if (!selected && ['127.0.0.1','localhost'].includes(location.hostname)) endpoint = location.origin;
      if (!endpoint) return null;
      const url = new URL(endpoint);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('节点地址格式无效');
      if (location.protocol === 'https:' && url.protocol !== 'https:') throw new Error('公网页面需要 HTTPS 实验节点');
      return {endpoint: endpoint, token:token.value.trim()};
    }
  };
  let lastEndpoint = field.value;
  field.addEventListener('input', () => {
    if (field.value !== lastEndpoint) { token.value = ''; lastEndpoint = field.value; }
  }, true);
  document.getElementById('node-profile-select').addEventListener('change', event => {
    try {
      const profiles = JSON.parse(localStorage.getItem('electrochemistry-node-profiles-v1') || '[]');
      const profile = profiles.find(p => 'saved:' + p.id === event.target.value);
      token.value = profile ? sessionStorage.getItem('edt-node-token:' + profile.id) || '' : '';
      media.value = profile?.video_endpoint || '';
      if (profile) localStorage.setItem('edt-selected-node', profile.id);
    } catch { token.value = ''; media.value = ''; }
  }, true);
  for (const element of [field, token, document.getElementById('node-profile-select')]) {
    element?.addEventListener('change', () => { selected = {manual:true}; window.dispatchEvent(new Event('edt-nodechange')); });
    element?.addEventListener('input', () => { selected = {manual:true}; window.dispatchEvent(new Event('edt-nodechange')); });
  }
  const connect = document.getElementById('connect');
  connect.addEventListener('click', async event => {
    if (location.protocol === 'https:' && !field.value.trim().startsWith('https://')) {
      event.preventDefault(); event.stopImmediatePropagation();
      document.getElementById('connection').textContent = '请配置 HTTPS 节点；本地 HTTP 节点请配合本地前端使用';
      return;
    }
    if (model.value === 'oxidation') {
      event.preventDefault(); event.stopImmediatePropagation();
      connect.disabled = true;
      try {
        const node = window.ElectrochemPublicConnection.hmf();
        if (!node) throw new Error('请填写 HMF 节点地址');
        const response = await fetch(node.endpoint + '/health', {cache:'no-store', signal:AbortSignal.timeout(8000)});
        if (!response.ok) throw new Error('节点响应 ' + response.status);
        const data = await response.json();
        if (data.mode !== 'hmf_numeric') throw new Error('此节点未声明 HMF 数值能力，请选择匹配节点');
        document.getElementById('connection').textContent = '已连接 HMF 数值节点';
        document.getElementById('hmf-status').textContent = '节点就绪，可按条件运行数值实验';
        document.getElementById('disconnect').disabled = false;
      } catch (error) { document.getElementById('connection').textContent = error.message; }
      finally { connect.disabled = false; }
    }
  }, true);
  document.getElementById('disconnect').addEventListener('click', () => window.dispatchEvent(new Event('edt-nodechange')));
})();

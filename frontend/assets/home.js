'use strict';
(() => {
  const storageKey = 'electrochemistry-node-profiles-v1';
  const dialog = document.getElementById('node-dialog');
  const form = document.getElementById('node-form');
  const message = document.getElementById('node-message');
  const list = document.getElementById('saved-nodes');
  const read = () => { try { const a = JSON.parse(localStorage.getItem(storageKey) || '[]'); return Array.isArray(a) ? a.filter(x => x && x.id && x.name && x.endpoint) : []; } catch { return []; } };
  function address(raw, optional = false) {
    if (!raw && optional) return '';
    const url = new URL(raw);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('请使用没有账号、查询参数和片段的 HTTP/HTTPS 服务地址');
    if (location.protocol === 'https:' && url.protocol !== 'https:') throw new Error('当前网站使用 HTTPS，请配置 HTTPS 节点；本地 HTTP 节点请配合本地前端使用');
    return url.href.replace(/\/$/, '');
  }
  function select(profile) {
    localStorage.setItem('edt-selected-node', profile.id);
    localStorage.setItem('electrochemistry-node-endpoint', profile.endpoint);
    message.textContent = '已选用「' + profile.name + '」，进入工作台后点击连接节点。';
  }
  function render() {
    list.replaceChildren();
    read().forEach(profile => {
      const row = document.createElement('div'); row.className = 'saved-node';
      const details = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = profile.name;
      const endpoint = document.createElement('small'); endpoint.textContent = profile.endpoint;
      details.append(name, endpoint);
      const use = document.createElement('button'); use.type = 'button'; use.textContent = '选用';
      use.addEventListener('click', () => { try { select(profile); } catch { message.textContent = '当前浏览器无法保存节点选择'; } });
      row.append(details, use); list.appendChild(row);
    });
  }
  document.querySelectorAll('[data-open-nodes]').forEach(b => b.addEventListener('click', () => { render(); dialog.showModal(); }));
  document.getElementById('close-nodes').addEventListener('click', () => dialog.close());
  form.addEventListener('submit', event => {
    event.preventDefault();
    try {
      const values = new FormData(form), endpoint = address(String(values.get('endpoint')).trim());
      const profiles = read(), old = profiles.find(p => p.endpoint === endpoint);
      const profile = { id: old?.id || crypto.randomUUID(), name: String(values.get('name')).trim(), endpoint,
        video_endpoint: address(String(values.get('video')).trim(), true), model: values.get('model') };
      if (!profile.name) throw new Error('请填写节点名称');
      localStorage.setItem(storageKey, JSON.stringify([...profiles.filter(p => p.id !== profile.id), profile]));
      const token = String(values.get('token')).trim();
      if (token) sessionStorage.setItem('edt-node-token:' + profile.id, token);
      else sessionStorage.removeItem('edt-node-token:' + profile.id);
      form.elements.token.value = ''; select(profile); render();
    } catch (error) { message.textContent = error.message; }
  });
})();

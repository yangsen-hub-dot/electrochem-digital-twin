/* Local numerical event stream; separate from cloud video and experiment state. */
(function () {
  'use strict';
  const id = name => document.getElementById('hmf-' + name);
  const labels = {load_tip:'装枪头', aspirate:'吸液', dispense:'分液', discard_tip:'弃枪头',
    mix:'混匀', grasp_electrode:'抓取电极', install_electrode:'安装电极',
    connect_electrode:'连接电极', electrolyze:'电解与预测'};
  const value = (number, factor, digits, unit) =>
    Number.isFinite(number) ? (number * factor).toFixed(digits) + ' ' + unit : '—';
  let current = null;
  let connectionController = new AbortController();
  function connection() {
    if (window.ElectrochemPublicConnection) return window.ElectrochemPublicConnection.hmf();
    return location.protocol === 'http:' && location.hostname === '127.0.0.1'
      ? {endpoint:location.origin,token:''} : null;
  }
  async function apiFetch(path, options = {}) {
    const node = connection();
    if (!node) throw new Error('请先在连接设置中填写实验节点地址');
    const headers = new Headers(options.headers || {});
    if (node.token) headers.set('Authorization', 'Bearer ' + node.token);
    const response = await fetch(node.endpoint + path, {...options, headers, signal:connectionController.signal});
    if (connection()?.endpoint !== node.endpoint) throw new Error('节点已切换，请重新读取');
    return response;
  }
  window.addEventListener('edt-nodechange', () => {
    connectionController.abort(); connectionController = new AbortController(); current = null;
    id('history-list')?.replaceChildren(); id('events')?.replaceChildren();
    for (const key of ['revision','volume','concentration','certainty','charge','fdca']) if (id(key)) id(key).textContent = '—';
    if (id('status')) id('status').textContent = '节点已切换，请重新读取或运行';
    if (id('run-id')) id('run-id').textContent = '运行编号：—';
    for (const key of ['validation-result','error-result']) id(key)?.replaceChildren();
    for (const key of ['validation-run','error-run']) if (id(key)) id(key).textContent = '未选择运行';
    if (id('prediction-note')) id('prediction-note').textContent = '等待当前节点的电解步骤；没有 HPLC 读数。';
    if (id('run-conditions')) id('run-conditions').textContent = '本次条件：待当前节点确认';
  });
  function displayRecord(record) {
    const run = record.result || {}, report = record.validation || {}, c = record.conditions || {};
    const label = '运行编号：' + (run.run_id || '—');
    id('validation-run').textContent = label;
    id('error-run').textContent = label;
    const checks = id('validation-result'); checks.replaceChildren();
    for (const check of report.checks || []) {
      const line = document.createElement('p'); line.textContent = '通过：' + check; checks.appendChild(line);
    }
    for (const issue of report.issues || []) {
      const line = document.createElement('p'); line.textContent = '待处理：' + issue.message + ' ' + issue.suggestion; checks.appendChild(line);
      if (issue.kind === 'prediction_unavailable') {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = '填入参考条件（待重新验证）';
        button.addEventListener('click', () => {
          for (const [name, reference] of Object.entries({'input-concentration':'31.25','input-tempo':'5','input-koh':'0.1','input-current':'10','input-bias':'0'})) id(name).value = reference;
          document.getElementById('tab-workbench')?.click();
          id('status').textContent = '已填入参考条件，请重新运行验证';
        });
        checks.appendChild(button);
      }
    }
    if (report.repair_plan?.eligible && run.run_id) {
      const plan = report.repair_plan;
      const info = document.createElement('p');
      info.textContent = `可局部修复：从账本版本 ${plan.checkpoint_revision} 恢复，只重跑前置动作和失败步骤；后续不自动执行。`;
      checks.appendChild(info);
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '执行本地局部修复并复验';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const response = await apiFetch('/api/hmf/repair', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:run.run_id})});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || '修复失败');
          displayRecord(result);
          id('history-status').textContent = '已保存局部修复记录；后续步骤尚未执行。';
        } catch (error) { id('validation-run').textContent = label + ' · ' + error.message; button.disabled = false; }
      });
      checks.appendChild(button);
    }
    if (!(report.checks || []).length && !(report.issues || []).length) checks.textContent = '未保存校验结果。';
    const errors = id('error-result'); errors.replaceChildren();
    const bias = document.createElement('p'); bias.textContent = 'HMF 母液移液偏差情景：' + (c.pipette_bias_percent ?? 0) + '%'; errors.appendChild(bias);
    const aspiration = (run.steps || []).map(row => row.event || {}).find(e => e.action === 'aspirate' && e.source === 'hmf_stock');
    const volume = document.createElement('p');
    volume.textContent = aspiration ? 'HMF 母液：计划 ' + value(aspiration.requested_volume_ul, 1, 3, 'µL') + '，模拟实际吸取 ' + value(aspiration.simulated_volume_ul, 1, 3, 'µL') : '没有 HMF 母液吸液记录。';
    errors.appendChild(volume);
    const reaction = (run.steps || []).map(row => row.event || {}).find(e => e.action === 'electrolyze' && e.prediction);
    const output = document.createElement('p');
    output.textContent = reaction?.prediction?.valid ? 'FDCA 模型预测：' + value(reaction.prediction.fdca_amount_mol, 1000, 4, 'mmol') : 'FDCA 收率预测不可用或尚未执行电解。';
    errors.appendChild(output);
  }
  async function refreshHistory() {
    try {
      const response = await apiFetch('/api/hmf/history', {cache:'no-store'});
      if (!response.ok) throw new Error('读取失败');
      const records = await response.json();
      const list = id('history-list'); list.replaceChildren();
      id('history-status').textContent = records.length ? `共 ${records.length} 条本地 HMF 记录` : '暂无 HMF 记录';
      for (const record of records) {
        const button = document.createElement('button'); button.type = 'button';
        const statusText = ({completed:'数值完成',failed:'失败',completed_prediction_unavailable:'预测不可用',repair_segment_completed:'局部修复待续',repair_verification_failed:'修复待复核'})[record.status] || record.status;
        const lineage = record.source_run_id ? ` · 来源 ${record.source_run_id.slice(0, 12)}` : '';
        button.textContent = `${record.created_at_utc} · ${record.run_id.slice(0, 12)} · HMF ${record.conditions.hmf_mm} mM · ${statusText}${lineage}`;
        button.addEventListener('click', async () => {
          try {
            const response = await apiFetch('/api/hmf/history/' + encodeURIComponent(record.run_id), {cache:'no-store'});
            if (!response.ok) throw new Error('记录不可用');
            displayRecord(await response.json());
            id('history-status').textContent = '已选择 ' + record.run_id + '；可查看校验和误差。';
          } catch (error) { id('history-status').textContent = '读取失败：' + error.message; }
        });
        list.appendChild(button);
      }
    } catch (error) { id('history-status').textContent = '读取失败：' + error.message; }
  }
  function render(message) {
    if (message.run_id) id('run-id').textContent = '运行编号：' + message.run_id;
    if (message.type === 'started') {
      current = {result:{run_id:message.run_id,steps:[]},conditions:message.conditions || {},validation:{}};
      id('status').textContent = '正在计算';
      id('progress').textContent = '步骤：0 / ' + message.total_steps;
      if (message.conditions) {
        const c = message.conditions;
        id('run-conditions').textContent = `本次条件：HMF ${c.hmf_mm} mM · TEMPO ${c.tempo_percent} mol% · KOH ${c.koh_m} M · ${c.current_ma} mA`;
      }
    }
    if (message.reactor) {
      id('revision').textContent = String(message.revision ?? '—');
      id('volume').textContent = value(message.reactor.volume_l, 1000, 3, 'mL');
      const concentration = message.reactor.concentrations_m?.HMF;
      id('concentration').textContent = value(concentration, 1000, 3, 'mM');
      id('certainty').textContent = message.certainty?.status === 'uncertain' ? '电解后未知，上方为电解前库存' : '已记录的数值状态';
    }
    if (message.type === 'step') {
      const e = message.event || {};
      if (current) current.result.steps.push({status:message.step_status,event:e});
      id('progress').textContent = '步骤：' + message.index + ' / ' + message.total_steps;
      const li = document.createElement('li');
      li.textContent = (labels[e.action] || e.action || '步骤') + (e.electrode_id ? ' · ' + e.electrode_id : '') + (message.step_status === 'failed' ? ' · 失败：' + (e.reason || '') : ' · 完成');
      id('events').appendChild(li);
      id('events').scrollTop = id('events').scrollHeight;
      if (e.action === 'electrolyze' && message.step_status === 'completed') {
        id('charge').textContent = value(e.charge_c, 1, 3, 'C');
        const p = e.prediction || {};
        id('fdca').textContent = p.valid ? value(p.fdca_amount_mol, 1000, 4, 'mmol') : '无法预测';
        id('prediction-note').textContent = p.valid ? '论文模型终点估计；不是 HPLC 实测。' : '该条件超出论文模型范围，暂无法预测收率。';
      }
    }
    if (message.type === 'finished') {
      id('status').textContent = message.status === 'completed' ? '数值流程完成' : message.status === 'completed_prediction_unavailable' ? '流程完成，预测不可用' : '流程失败';
      if (message.history_saved === false) id('status').textContent += '；历史保存失败';
      if (current) { current.result.status = message.status; current.validation = message.validation || {}; displayRecord(current); }
    }
    if (message.type === 'error') id('status').textContent = '运行失败：' + message.reason;
  }
  async function start() {
    const fields = [
      ['hmf_mm', 'input-concentration', 1, 100], ['tempo_percent', 'input-tempo', 1, 10],
      ['koh_m', 'input-koh', .05, .2], ['current_ma', 'input-current', 1, 20],
      ['pipette_bias_percent', 'input-bias', -5, 5]
    ];
    const conditions = {};
    for (const [key, name, min, max] of fields) {
      const raw = id(name).value.trim();
      const number = Number(raw);
      if (!raw || !Number.isFinite(number) || number < min || number > max) {
        id('status').textContent = `请填写有效条件：${id(name).parentElement?.textContent?.trim() || key}（${min}–${max}）`;
        id(name).focus(); return;
      }
      conditions[key] = number;
    }
    id('start').disabled = true;
    id('events').replaceChildren();
    for (const key of ['revision','volume','concentration','certainty','charge','fdca']) id(key).textContent = '—';
    id('prediction-note').textContent = '等待电解步骤；没有 HPLC 读数。';
    id('run-id').textContent = '运行编号：—';
    id('run-conditions').textContent = '本次条件：等待服务确认';
    id('status').textContent = '连接所选数值服务';
    let finished = false;
    try {
      const response = await apiFetch('/api/hmf/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({seed:0,conditions})});
      if (!response.ok || !response.body) throw new Error('服务不可用（' + response.status + '）');
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const part = await reader.read();
        buffer += decoder.decode(part.value || new Uint8Array(), {stream:!part.done});
        let pos;
        while ((pos = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, pos); buffer = buffer.slice(pos + 1);
          if (line.trim()) { const message = JSON.parse(line); render(message); if (['finished','error'].includes(message.type)) finished = true; }
        }
        if (part.done) break;
      }
      if (buffer.trim()) throw new Error('事件流不完整');
      if (!finished) throw new Error('连接中断，运行结果未确认');
    } catch (error) { id('status').textContent = '连接失败：' + error.message; }
    finally { id('start').disabled = false; }
  }
  id('start')?.addEventListener('click', start);
  id('history-refresh')?.addEventListener('click', refreshHistory);
  document.getElementById('tab-history')?.addEventListener('click', () => {
    if (document.getElementById('experiment-model')?.value === 'oxidation') refreshHistory();
  });
})();

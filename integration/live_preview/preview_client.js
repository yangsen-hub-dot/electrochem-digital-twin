/* Latest-frame online preview. No video download, POST, or state mutation API. */
(function (root) {
  "use strict";
  const validRun = value => /^[a-f0-9]{32}$/.test(value || "");
  class FrameFollower {
    constructor({fetchPacket, decodeImage, commit, clear, status, now = () => Date.now()}) {
      Object.assign(this, {fetchPacket, decodeImage, commit, clear, status, now});
      this.generation = 0; this.sequence = 0; this.runId = null; this.lastPaint = null; this.terminal = false;
    }
    start(runId) {
      if (!validRun(runId)) throw new Error("运行编号无效");
      this.stop(); this.runId = runId; this.sequence = 0; this.lastPaint = null; this.terminal = false;
      this.clear(); this.status("等待本次运行的首张预览画面");
    }
    stop() { ++this.generation; this.controller?.abort(); this.controller = null; }
    async tick() {
      if (!this.runId || this.controller || this.terminal) return;
      const generation = this.generation, runId = this.runId;
      const controller = new AbortController(); this.controller = controller;
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const packet = await this.fetchPacket(runId, this.sequence, controller.signal);
        if (generation !== this.generation) return;
        if (packet.run_id !== runId) throw new Error("预览运行编号不匹配");
        if (!packet.available) { this.status("等待首帧；当前运行尚无在线预览（旧运行不补造）"); return; }
        if (!packet.unchanged) {
          if (!Number.isInteger(packet.sequence) || packet.sequence <= this.sequence) return;
          if (!packet.frame_state || packet.image_mime !== "image/jpeg" ||
              !Number.isFinite(packet.width) || !Number.isFinite(packet.height) ||
              packet.width <= 0 || packet.height <= 0 || packet.width > 2560 || packet.height > 1440 || packet.width * packet.height > 2560 * 1440 ||
              typeof packet.image_base64 !== "string" || packet.image_base64.length > 4 * 1024 * 1024)
            throw new Error("预览帧格式无效");
          const image = await this.decodeImage(packet);
          try {
            // Decoding may finish after disconnect/reconnect or a change of run.
            if (generation !== this.generation || runId !== this.runId) return;
            this.commit(image, packet); // One JS turn commits both picture and its own data.
            this.sequence = packet.sequence; this.lastPaint = this.now();
          } finally { image.close?.(); }
        }
        const stale = packet.stale || (this.lastPaint != null && this.now() - this.lastPaint > 10000);
        this.terminal = ["ended", "failed"].includes(packet.stream_status);
        this.status(packet.stream_status === "failed" ? "预览结束：运行失败，保留最后一帧"
          : packet.stream_status === "ended" ? "在线预览已结束；完整录像请在云端回放区查看"
          : stale ? "画面暂未更新，当前数据仍属于最后一帧，不是实时终态" : "在线预览 · 画面与同帧数据同步");
      } catch (error) {
        if (generation === this.generation) this.status("预览连接暂不可用；保留最后同帧数据，不影响仿真与云端录像");
      } finally {
        clearTimeout(timeout);
        if (this.controller === controller) this.controller = null;
      }
    }
  }

  function mount(getConnection) {
    const $ = id => document.getElementById(id);
    const canvas = $("live-preview-canvas"), ctx = canvas.getContext("2d");
    const number = (value, digits = 4) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
    const labelIds = ["live-frame-time", "live-frame-step", "live-frame-cells", "live-frame-mixing", "live-frame-voltage", "live-frame-run"];
    const clear = () => { ctx.clearRect(0, 0, canvas.width, canvas.height); canvas.hidden = true; labelIds.forEach(id => { $(id).textContent = "—"; }); };
    const follower = new FrameFollower({
      clear, status: text => { $("live-preview-status").textContent = text; },
      fetchPacket: async (runId, sequence, signal) => {
        const {endpoint, token} = getConnection();
        const base = new URL(endpoint);
        if (!["http:", "https:"].includes(base.protocol) || base.username || base.password) throw new Error("无效节点地址");
        const url = new URL(base.href.replace(/\/$/, "") + "/preview");
        url.search = new URLSearchParams({run_id:runId, after_sequence:String(sequence)});
        const response = await fetch(url, {signal, cache:"no-store", headers:{Authorization:`Bearer ${token}`}});
        if (!response.ok) throw new Error(`预览 HTTP ${response.status}`);
        return response.json();
      },
      decodeImage: async packet => {
        const bytes = Uint8Array.from(atob(packet.image_base64), value => value.charCodeAt(0));
        return createImageBitmap(new Blob([bytes], {type:"image/jpeg"}));
      },
      commit: (image, packet) => {
        const state = packet.frame_state, cells = state.cells || {};
        const cellText = side => `${side === "left" ? "左" : "右"} ${number(cells[side]?.volume_ul == null ? null : cells[side].volume_ul / 1000)} mL · Cu²⁺ ${number(cells[side]?.concentrations_m?.Cu, 6)} M`;
        canvas.width = image.width; canvas.height = image.height;
        ctx.drawImage(image, 0, 0); canvas.hidden = false;
        $("live-frame-run").textContent = packet.run_id;
        $("live-frame-time").textContent = `仿真时间 ${number(state.simulation_time_s, 2)} s · 录像帧 ${state.frame_index} · 预览序号 ${packet.sequence}`;
        $("live-frame-step").textContent = `${state.phase || "—"} · 已确认 ${state.completed_steps ?? "—"}/${state.total_steps ?? "—"} 步 · 状态版本 ${state.state_revision ?? "—"}${state.state_uncertain ? " · 状态不确定" : ""}`;
        $("live-frame-cells").textContent = cellText("left") + "；" + cellText("right");
        const uniformity = side => cells[side]?.mixing_uniformity == null ? "—" : number(cells[side].mixing_uniformity * 100, 1) + "%";
        $("live-frame-mixing").textContent = `同帧温度 ${number(state.temperature_k == null ? null : state.temperature_k - 273.15, 1)} °C · 均匀度：左 ${uniformity("left")}，右 ${uniformity("right")}`;
        $("live-frame-voltage").textContent = state.voltage_valid ? `理想虚拟读数 ${number(state.virtual_meter_mv, 3)} mV · 模拟非实测` : "此帧尚无有效电压结果";
      },
    });
    let timer = null, auto = true, currentRun = null, connectionKey = null;
    const stop = () => { clearInterval(timer); timer = null; follower.stop(); };
    const follow = runId => { stop(); follower.start(runId); follower.tick(); timer = setInterval(() => follower.tick(), 500); };
    $("live-preview-pause").addEventListener("click", () => { stop(); auto = false; $("live-preview-status").textContent = "已暂停观看；仿真和云端完整录像继续，数据冻结在当前画面"; });
    $("live-preview-follow").addEventListener("click", () => { auto = true; if (validRun(currentRun)) follow(currentRun); });
    $("live-preview-select").addEventListener("click", () => {
      const runId = $("live-preview-run-id").value.trim();
      if (!validRun(runId)) { $("live-preview-status").textContent = "请填写 32 位运行编号"; return; }
      auto = false; follow(runId);
    });
    return {
      observe(data) {
        const connection = getConnection(), nextKey = connection.endpoint + "\n" + connection.token;
        const changedConnection = connectionKey !== nextKey;
        connectionKey = nextKey; currentRun = data.run_id;
        if (changedConnection) { stop(); clear(); auto = true; }
        if (auto && !validRun(currentRun)) { stop(); clear(); follower.runId = null; $("live-preview-status").textContent = "节点当前尚无运行；没有使用历史画面代替"; return; }
        if (auto && validRun(currentRun) && (follower.runId !== currentRun || changedConnection || !timer)) follow(currentRun);
      },
      disconnect() { stop(); clear(); currentRun = null; connectionKey = null; follower.runId = null; $("live-preview-status").textContent = "未连接节点"; },
    };
  }
  const api = {FrameFollower, mount};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ElectrochemLivePreview = api;
})(typeof window !== "undefined" ? window : globalThis);

(function () {
  const $ = (id) => document.getElementById(id);
  const apiPrefix = new URLSearchParams(location.search).get('api') || '';
  const demo = buildDemo();
  const state = {
    summary: null, history: demo.history, jobs: demo.jobs, selectedJobId: null, selectedJob: null,
    selectedLog: '', selectedEvents: [], jobFilter: 'all', chartMode: 'trend', activityVisible: true,
    drawerOpen: false, loading: false, error: null, controlState: '空闲', lastRefreshAt: 0, logReq: 0,
  };
  const el = {
    health: $('health-pill'), runtime: $('runtime-pill'), lease: $('lease-pill'), write: $('write-pill'),
    controller: $('controller-label'), controllerMeta: $('controller-meta'), heartbeat: $('heartbeat-label'),
    heartbeatMeta: $('heartbeat-meta'), sync: $('sync-label'), syncMeta: $('sync-meta'),
    q: $('metric-queue'), qm: $('metric-queue-meta'), r: $('metric-running'), rm: $('metric-running-meta'),
    d: $('metric-done'), dm: $('metric-done-meta'), f: $('metric-failed'), fm: $('metric-failed-meta'),
    c: $('metric-cancelled'), cm: $('metric-cancelled-meta'), cq: $('metric-control'), cqm: $('metric-control-meta'),
    legend: $('gpu-legend'), gpuGrid: $('gpu-grid'), chart1: $('history-chart'), chart2: $('gpu-chart'),
    activity: $('activity-feed'), controlState: $('control-state-pill'), controlForm: $('control-form'),
    action: $('control-action'), reason: $('control-reason'), jobIds: $('control-job-ids'),
    signal: $('control-signal'), grace: $('control-grace'), purge: $('control-purge'), cancel: $('control-cancel'),
    activeBody: $('active-jobs-body'), terminalBody: $('terminal-jobs-body'), activeCount: $('active-job-count'),
    terminalCount: $('terminal-job-count'), activeCaption: $('active-job-caption'), terminalCaption: $('terminal-job-caption'),
    filters: $('job-filters'), drawerBg: $('drawer-backdrop'), drawer: $('log-drawer'), drawerTitle: $('drawer-title'),
    drawerMeta: $('drawer-meta'), drawerStatus: $('drawer-status'), drawerJobId: $('drawer-job-id'),
    drawerGpu: $('drawer-gpu'), drawerUpdated: $('drawer-updated'), drawerReason: $('drawer-kill-reason'),
    drawerKill: $('drawer-kill-button'), drawerLogState: $('drawer-log-state'), drawerLog: $('drawer-log'),
    drawerEvents: $('drawer-events'),
  };

  document.addEventListener('click', onClick);
  document.addEventListener('keydown', (e) => e.key === 'Escape' && closeDrawer());
  el.controlForm.addEventListener('submit', submitControl);
  el.drawerBg.addEventListener('click', closeDrawer);
  window.addEventListener('resize', () => requestAnimationFrame(renderCharts));
  refresh(true);
  setInterval(() => refresh(false), 5000);

  async function refresh(force) {
    if (state.loading && !force) return;
    state.loading = true;
    setControlState('同步中');
    try {
      const [s, h, j] = await Promise.allSettled([fetchJSON('/api/summary'), fetchJSON('/api/history?tail=300'), fetchJSON('/api/jobs')]);
      state.summary = s.status === 'fulfilled' ? s.value : demo.summary;
      state.history = normalizeHistory(h.status === 'fulfilled' ? h.value : demo.history);
      state.jobs = normalizeJobs(j.status === 'fulfilled' ? j.value : demo.jobs);
      state.error = [s, h, j].every((r) => r.status !== 'fulfilled') ? new Error('所有接口都失败了') : null;
      state.lastRefreshAt = Date.now();
      if (state.selectedJobId) await loadJob(state.selectedJobId, { silent: true });
    } catch (e) {
      state.summary = demo.summary; state.history = demo.history; state.jobs = demo.jobs; state.error = e;
    } finally {
      state.loading = false;
      render();
    }
  }

  async function fetchJSON(path) {
    const res = await fetch(api(path), { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }

  async function postJSON(path, body) {
    const res = await fetch(api(path), { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body || {}) });
    if (!res.ok) throw new Error(`${path} -> ${res.status} ${await safeText(res)}`);
    return safeJSON(res);
  }

  async function safeJSON(res) { try { return await res.json(); } catch { return {}; } }
  async function safeText(res) { try { return await res.text(); } catch { return ''; } }
  function api(path) { return apiPrefix ? `${apiPrefix.replace(/\/$/, '')}${path}` : path; }

  function onClick(e) {
    const a = e.target.closest('[data-action]');
    if (a) return handleAction(a.dataset.action);
    const p = e.target.closest('[data-control-preset]');
    if (p) return presetControl(p.dataset.controlPreset);
    const f = e.target.closest('[data-filter]');
    if (f) return setFilter(f.dataset.filter);
    const row = e.target.closest('tr[data-job-id]');
    if (!row) return;
    const btn = e.target.closest('[data-job-action]');
    if (btn) return btn.dataset.jobAction === 'kill' ? killJob(btn.dataset.jobId) : openJob(btn.dataset.jobId);
    return openJob(row.dataset.jobId);
  }

  function handleAction(name) {
    if (name === 'refresh') return refresh(true);
    if (name === 'scroll-jobs') return $('jobs-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (name === 'toggle-history') { state.chartMode = state.chartMode === 'trend' ? 'activity' : 'trend'; return renderCharts(); }
    if (name === 'toggle-activity') { state.activityVisible = !state.activityVisible; return render(); }
    if (name === 'close-drawer') return closeDrawer();
    if (name === 'reload-selected-log' && state.selectedJobId) return loadJob(state.selectedJobId, { silent: false });
    if (name === 'kill-selected') return state.selectedJobId && killJob(state.selectedJobId);
    if (name === 'reset-control-form') return resetControlForm();
  }

  function presetControl(action) {
    el.action.value = action;
    el.reason.value = `预设动作: ${action}`;
    el.jobIds.value = '';
    el.signal.value = 'TERM';
    el.grace.value = '15';
    el.purge.checked = action === 'purge_queue' || action === 'retire_controller';
    el.cancel.checked = action !== 'purge_queue';
    setControlState(`预设: ${action}`);
  }

  function resetControlForm() {
    el.action.value = 'cancel_active_job';
    el.reason.value = '人工操作请求';
    el.jobIds.value = '';
    el.signal.value = 'TERM';
    el.grace.value = '15';
    el.purge.checked = false;
    el.cancel.checked = false;
    setControlState('空闲');
  }

  async function submitControl(e) {
    e.preventDefault();
    const body = {
      action: el.action.value,
      reason: el.reason.value.trim() || `人工请求 ${el.action.value}`,
      signal: el.signal.value.trim() || 'TERM',
      grace_seconds: Number(el.grace.value || 0),
      purge_queue: el.purge.checked,
      cancel_active_job: el.cancel.checked,
    };
    const job_ids = el.jobIds.value.split(',').map((s) => s.trim()).filter(Boolean);
    if (job_ids.length) body.job_ids = job_ids;
    setControlState('发送中');
    try {
      await postJSON('/api/control', body);
      setControlState('已提交');
      await refresh(true);
    } catch (err) {
      setControlState('错误');
      alert(`控制请求失败: ${err.message}`);
    }
  }

  function render() {
    const s = state.summary || {};
    const c = s.controller_state || {};
    const l = s.controller_lease || {};
    const counts = s.counts || {};
    const hb = c.updated_at_epoch || latestEpoch(state.history);
    const leaseAge = l.updated_at_epoch || hb;
    const health = healthState(hb, leaseAge);

    el.health.textContent = health.label; el.health.className = `pill ${health.cls}`;
    el.runtime.textContent = `Runtime: ${shortPath(s.runtime_root || c.runtime_root || '-')}`;
    el.lease.textContent = `Lease: ${ageText(leaseAge)}`;
    el.write.textContent = s.allow_write_actions ? '控制权限: 可写' : '控制权限: 只读';
    el.write.className = `pill ${s.allow_write_actions ? 'ok' : 'warn'}`;
    el.controller.textContent = c.pid ? `${c.hostname || 'controller'} | pid ${c.pid}` : 'controller 离线';
    el.controllerMeta.textContent = c.active_job_count != null ? `${c.active_job_count} 个活跃任务 | ${counts.queue ?? c.queue_count ?? 0} 个排队` : '摘要暂不可用';
    el.heartbeat.textContent = hb ? ageText(hb) : '没有心跳';
    el.heartbeatMeta.textContent = hb ? fmtDateTime(hb) : '没有心跳时间';
    el.sync.textContent = state.error ? '降级' : '在线';
    el.syncMeta.textContent = state.error ? state.error.message : `最近更新于 ${state.lastRefreshAt ? ageText(state.lastRefreshAt / 1000) : '-'}`;

    setMetric(el.q, el.qm, counts.queue ?? c.queue_count ?? 0, '排队任务');
    setMetric(el.r, el.rm, counts.running_specs ?? c.active_job_count ?? 0, '活跃任务');
    setMetric(el.d, el.dm, counts.done ?? c.done_count ?? 0, '完成任务');
    setMetric(el.f, el.fm, counts.failed ?? c.failed_count ?? 0, '失败任务');
    setMetric(el.c, el.cm, counts.cancelled ?? c.cancelled_count ?? 0, '取消任务');
    setMetric(el.cq, el.cqm, counts.control_queue ?? c.control_queue_count ?? 0, '待处理请求');

    renderLegend(c.gpu_status || []);
    renderGpuGrid(c);
    renderActivity();
    renderJobs();
    renderDrawer();
    renderCharts();
    syncControls();
  }

  function setMetric(v, m, value, text) { v.textContent = value ?? '-'; m.textContent = text; }

  function renderLegend(gpus) {
    el.legend.innerHTML = '';
    gpus.slice(0, 8).forEach((g) => {
      const s = document.createElement('span');
      s.className = 'legend-pill';
      s.innerHTML = `<i style="background:${gpuColor(Number(g.util_pct || 0), Number(g.temp_c || 0))}"></i> GPU ${esc(g.index)}`;
      el.legend.appendChild(s);
    });
  }

  function renderGpuGrid(controller) {
    const active = Array.isArray(controller.active_jobs) ? controller.active_jobs : [];
    const status = Array.isArray(controller.gpu_status) ? controller.gpu_status : [];
    const procs = Array.isArray(controller.gpu_processes) ? controller.gpu_processes : [];
    const owner = new Map(); active.forEach((j) => (j.allocated_gpu_indices || []).forEach((i) => owner.set(String(i), j.job_id)));
    const pmap = new Map(); procs.forEach((p) => { const k = String(p.gpu_index); (pmap.get(k) || pmap.set(k, []).get(k)).push(p); });
    el.gpuGrid.innerHTML = '';
    status.forEach((g) => {
      const util = clamp(Number(g.util_pct || 0), 0, 100);
      const job = owner.get(String(g.index));
      const count = (pmap.get(String(g.index)) || []).length;
      const card = document.createElement('article');
      card.className = `gpu-card ${job ? 'is-owned' : 'is-free'} ${Number(g.temp_c || 0) >= 82 ? 'is-hot' : ''}`;
      card.innerHTML = `<div class="gpu-head"><div><span class="gpu-index">GPU ${esc(g.index)}</span><strong class="gpu-owner">${esc(job || (count ? '外部进程' : '空闲'))}</strong></div><span class="status-badge ${gpuBadge(util, Number(g.temp_c || 0), job, count)}">${gpuBadgeText(util, Number(g.temp_c || 0), job, count)}</span></div><div class="gpu-metrics"><div class="mini-metric"><span>利用率</span><strong>${util.toFixed(0)}%</strong></div><div class="mini-metric"><span>显存</span><strong>${esc(`${fmtMiB(g.mem_used_mb)} / ${fmtMiB(g.mem_total_mb)}`)}</strong></div><div class="mini-metric"><span>温度</span><strong>${Number(g.temp_c || 0).toFixed(0)} C</strong></div></div><div class="gpu-bar"><i style="width:${util}%"></i></div><p class="gpu-note">${esc(gpuNote(job, count, util))}</p>`;
      card.style.setProperty('--gpu-accent', gpuColor(util, Number(g.temp_c || 0)));
      el.gpuGrid.appendChild(card);
    });
  }

  function renderActivity() {
    el.activity.innerHTML = '';
    if (!state.activityVisible) { el.activity.innerHTML = '<div class="muted-block">活动流已隐藏</div>'; return; }
    const rows = activityRows();
    if (!rows.length) { el.activity.innerHTML = '<div class="muted-block">暂时还没有活动记录</div>'; return; }
    rows.slice(0, 8).forEach((r) => {
      const d = document.createElement('div');
      d.className = 'activity-row';
      d.innerHTML = `<span>${esc(r.when)}</span><div><strong>${esc(r.title)}</strong><small>${esc(r.detail)}</small></div><em>${esc(r.right || '')}</em>`;
      el.activity.appendChild(d);
    });
  }

  function activityRows() {
    const rows = [];
    const events = [...(state.history.controller_events || []).map((x) => ({ ...x, src: 'event' })), ...(state.history.admin_audit || []).map((x) => ({ ...x, src: 'audit' }))];
    events.sort((a, b) => epochOf(b) - epochOf(a)).forEach((x) => rows.push({ when: ageText(epochOf(x)), title: bucketLabel(x.event || x.action || x.status || x.src), detail: x.reason || x.message || x.job_id || 'controller 活动', right: x.job_id || x.request_id || '' }));
    (state.history.heartbeat || []).slice(-3).reverse().forEach((x) => rows.push({ when: ageText(epochOf(x)), title: '心跳', detail: `队列 ${x.queue_count ?? '-'} | 运行中 ${x.active_job_count ?? x.running_count ?? '-'}`, right: fmtDateTime(epochOf(x)) }));
    return rows;
  }

  function renderJobs() {
    const rows = normalizeJobs(state.jobs).filter((j) => state.jobFilter === 'all' || j.bucket === state.jobFilter);
    const active = rows.filter((j) => j.bucket === 'running' || j.bucket === 'queue').sort((a, b) => rank(a.bucket) - rank(b.bucket) || epochJob(b) - epochJob(a));
    const terminal = rows.filter((j) => ['done', 'failed', 'cancelled'].includes(j.bucket)).sort((a, b) => epochJob(b) - epochJob(a));
    el.activeCount.textContent = active.length; el.terminalCount.textContent = terminal.length;
    el.activeCaption.textContent = `${active.length} 个活跃或排队任务`; el.terminalCaption.textContent = `${terminal.length} 个终态任务`;
    fillTable(el.activeBody, active); fillTable(el.terminalBody, terminal);
  }

  function fillTable(tbody, rows) {
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">当前视图下没有任务。</td></tr>';
      return;
    }
    rows.forEach((j) => {
      const tr = document.createElement('tr');
      tr.dataset.jobId = j.job_id;
      tr.className = state.selectedJobId === j.job_id ? 'is-selected' : '';
      tr.innerHTML = `<td><div class="job-name"><strong>${esc(j.job_id)}</strong><small>${esc(j.command_text || j.name || j.bucket || '任务')}</small></div></td><td><span class="status-badge ${statusClass(j.bucket)}">${esc(bucketLabel(j.bucket))}</span></td><td>${esc(j.gpu_text || '-')}</td><td>${esc(ageText(epochJob(j)))}</td><td>${esc(shortPath(j.workdir || '-'))}</td><td><div class="row-actions"><button class="tiny-button" type="button" data-job-action="log" data-job-id="${esc(j.job_id)}">日志</button>${canKill(j) ? `<button class="tiny-button danger" type="button" data-job-action="kill" data-job-id="${esc(j.job_id)}">终止</button>` : ''}</div></td>`;
      tbody.appendChild(tr);
    });
  }

  function canKill(job) {
    return Boolean(state.summary?.allow_write_actions && ['running', 'queue'].includes(job.bucket));
  }

  async function openJob(jobId) {
    state.selectedJobId = jobId;
    openDrawer();
    await loadJob(jobId, { silent: false });
    render();
  }

  async function loadJob(jobId, { silent }) {
    state.selectedJob = lookupJob(jobId) || { job_id: jobId };
    setDrawerMeta(state.selectedJob);
    el.drawerLogState.textContent = '加载中';
    const req = ++state.logReq;
    try {
      const payload = await fetchJSON(`/api/logs/job/${encodeURIComponent(jobId)}?tail=400`);
      if (req !== state.logReq) return;
      state.selectedLog = Array.isArray(payload.log_lines) ? payload.log_lines.join('\n') : String(payload.log_lines || payload.log || '没有返回日志内容。');
      state.selectedEvents = Array.isArray(payload.event_rows) ? payload.event_rows : [];
      el.drawerLog.textContent = state.selectedLog;
      el.drawerLogState.textContent = '就绪';
      renderDrawerEvents();
    } catch (err) {
      if (req !== state.logReq) return;
      state.selectedLog = demo.log;
      state.selectedEvents = [];
      el.drawerLog.textContent = state.selectedLog;
      el.drawerLogState.textContent = '错误';
      renderDrawerEvents();
      if (!silent) alert(`日志加载失败: ${err.message}`);
    }
  }

  function lookupJob(jobId) {
    return normalizeJobs(state.jobs).find((j) => j.job_id === jobId) || normalizeJobs(state.summary?.controller_state?.active_jobs || []).find((j) => j.job_id === jobId) || null;
  }

  function renderDrawer() {
    if (!state.selectedJobId) {
      el.drawerTitle.textContent = '请选择任务';
      el.drawerMeta.textContent = '点击任务行以查看日志尾部和事件载荷。';
      el.drawerStatus.textContent = '-';
      el.drawerJobId.textContent = '-';
      el.drawerGpu.textContent = '-';
      el.drawerUpdated.textContent = '-';
      el.drawerLog.textContent = '请选择任务以查看日志尾部。';
      el.drawerEvents.innerHTML = '<div class="muted-block">任务事件会显示在这里。</div>';
      syncControls();
      return;
    }
    const j = state.selectedJob || lookupJob(state.selectedJobId);
    setDrawerMeta(j);
    el.drawerLog.textContent = state.selectedLog || '没有返回日志内容。';
    renderDrawerEvents();
    syncControls();
  }

  function setDrawerMeta(j) {
    el.drawerTitle.textContent = j?.job_id || state.selectedJobId || '请选择任务';
    el.drawerMeta.textContent = j ? `${bucketLabel(j.bucket || j.status)} | ${shortPath(j.workdir || '-')}` : '任务元信息暂不可用';
    el.drawerStatus.textContent = bucketLabel(j?.bucket || j?.status || '-');
    el.drawerStatus.className = `pill ${statusClass(j?.bucket || j?.status)}`;
    el.drawerJobId.textContent = j?.job_id || '-';
    el.drawerGpu.textContent = j?.gpu_text || '-';
    el.drawerUpdated.textContent = j ? ageText(epochJob(j)) : '-';
  }

  function renderDrawerEvents() {
    el.drawerEvents.innerHTML = '';
    if (!state.selectedEvents.length) {
      el.drawerEvents.innerHTML = '<div class="muted-block">没有返回任务事件。</div>';
      return;
    }
    state.selectedEvents.slice(0, 8).forEach((x) => {
      const n = document.createElement('div');
      n.className = 'drawer-event';
      n.innerHTML = `<span>${esc(ageText(epochOf(x)))}</span><div><strong>${esc(bucketLabel(x.event || x.action || x.status || 'event'))}</strong><small>${esc(x.message || x.reason || x.job_id || '任务事件')}</small></div>`;
      el.drawerEvents.appendChild(n);
    });
  }

  function renderCharts() {
    drawHistoryChart(el.chart1, state.history, state.chartMode);
    drawGpuChart(el.chart2, state.summary?.controller_state || {});
  }

  function openDrawer() {
    state.drawerOpen = true;
    el.drawer.classList.add('is-open');
    el.drawer.setAttribute('aria-hidden', 'false');
    el.drawerBg.hidden = false;
    document.body.classList.add('drawer-open');
  }

  function closeDrawer() {
    state.drawerOpen = false;
    el.drawer.classList.remove('is-open');
    el.drawer.setAttribute('aria-hidden', 'true');
    el.drawerBg.hidden = true;
    document.body.classList.remove('drawer-open');
  }

  function syncControls() {
    const allow = Boolean(state.summary?.allow_write_actions);
    el.drawerKill.disabled = !allow || !state.selectedJobId;
    [el.action, el.reason, el.jobIds, el.signal, el.grace, el.purge, el.cancel].forEach((n) => { n.disabled = !allow; });
    if (!allow) setControlState('只读');
    else if (state.controlState === '只读') setControlState('空闲');
  }

  async function killJob(jobId) {
    if (!state.summary?.allow_write_actions) return alert('当前是只读模式，不能执行写操作。');
    if (!confirm(`确认终止任务 ${jobId} 吗？`)) return;
    setControlState('正在终止');
    try {
      await postJSON(`/api/jobs/${encodeURIComponent(jobId)}/kill`, { reason: el.drawerReason.value.trim() || `管理面板终止 ${jobId}` });
      setControlState('已提交');
      await refresh(true);
      state.selectedJobId = jobId;
      await loadJob(jobId, { silent: true });
      openDrawer();
    } catch (err) {
      setControlState('错误');
      alert(`终止失败: ${err.message}`);
    }
  }

  function drawHistoryChart(canvas, history, mode) {
    const { ctx, w, h } = fitCanvas(canvas);
    clear(ctx, w, h);
    grid(ctx, w, h);
    if (mode === 'activity') {
      const buckets = minuteBuckets([...(history.controller_events || []), ...(history.admin_audit || [])]);
      if (!buckets.length) return empty(ctx, w, h, '还没有活动记录');
      bars(ctx, w, h, buckets.map((b) => b.value), buckets.map((b) => b.label), { fill: gradient(ctx, '#7ef0e0', '#8db8ff'), footer: 'controller 活动' });
      return;
    }
    const hb = history.heartbeat || [];
    if (!hb.length) return empty(ctx, w, h, '还没有历史数据');
    lineSeries(ctx, w, h, hb.map((x) => Number(x.queue_count || 0)), '#7ef0e0');
    lineSeries(ctx, w, h, hb.map((x) => Number(x.active_job_count || x.running_count || 0)), '#8db8ff');
    legend(ctx, w, ['队列', '运行中'], ['#7ef0e0', '#8db8ff']);
    xLabels(ctx, w, h, hb);
  }

  function drawGpuChart(canvas, controller) {
    const { ctx, w, h } = fitCanvas(canvas);
    clear(ctx, w, h);
    grid(ctx, w, h);
    const gpus = controller.gpu_status || [];
    if (!gpus.length) return empty(ctx, w, h, '没有 GPU 数据');
    bars(ctx, w, h, gpus.map((g) => Number(g.util_pct || 0)), gpus.map((g) => `GPU ${g.index}`), { fill: gradient(ctx, '#7ef0e0', '#ffca6b'), footer: '利用率' });
  }

  function fitCanvas(canvas) {
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(320, Math.floor(rect.width || canvas.width));
    const h = Math.max(240, Math.floor(rect.height || canvas.height));
    canvas.width = Math.floor(w * ratio);
    canvas.height = Math.floor(h * ratio);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx, w, h };
  }

  function clear(ctx, w, h) { ctx.clearRect(0, 0, w, h); }
  function grid(ctx, w, h) {
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,.05)';
    for (let i = 1; i <= 5; i += 1) { const y = (h / 6) * i; ctx.beginPath(); ctx.moveTo(42, y); ctx.lineTo(w - 18, y); ctx.stroke(); }
    ctx.restore();
  }
  function empty(ctx, w, h, text) { ctx.save(); ctx.fillStyle = 'rgba(149,171,181,.9)'; ctx.font = '600 15px var(--sans)'; ctx.textAlign = 'center'; ctx.fillText(text, w / 2, h / 2); ctx.restore(); }

  function lineSeries(ctx, w, h, values, color) {
    const left = 54, top = 34, bottom = 40, right = 20, max = Math.max(1, ...values), cw = w - left - right, ch = h - top - bottom;
    const pts = values.map((v, i) => ({ x: left + (values.length <= 1 ? 0 : (cw * i) / (values.length - 1)), y: top + ch - (v / max) * ch }));
    ctx.save();
    ctx.lineWidth = 2.5; ctx.strokeStyle = color; ctx.shadowColor = color; ctx.shadowBlur = 10;
    ctx.beginPath();
    pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.bezierCurveTo(pts[i - 1].x + (p.x - pts[i - 1].x) * .5, pts[i - 1].y, p.x - (p.x - pts[i - 1].x) * .5, p.y, p.x, p.y)));
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.fillStyle = color;
    pts.forEach((p) => { ctx.beginPath(); ctx.arc(p.x, p.y, 3.4, 0, Math.PI * 2); ctx.fill(); });
    ctx.restore();
  }

  function bars(ctx, w, h, values, labels, opt) {
    const left = 54, right = 20, top = 32, bottom = opt.footer ? 44 : 28, max = Math.max(1, ...(opt.max ? [opt.max] : []), ...values), cw = w - left - right, ch = h - top - bottom;
    const gap = Math.max(6, Math.min(14, Math.floor(ch / Math.max(1, values.length * 3))));
    const barH = Math.max(10, (ch - gap * (values.length - 1)) / Math.max(1, values.length));
    ctx.save();
    ctx.textBaseline = 'middle';
    ctx.font = '600 12px var(--mono)';
    values.forEach((v, i) => {
      const y = top + i * (barH + gap), ww = cw * (v / max), label = labels[i] == null ? String(i) : String(labels[i]);
      ctx.fillStyle = 'rgba(149,171,181,.95)'; ctx.textAlign = 'left'; ctx.fillText(label, 8, y + barH / 2);
      roundRect(ctx, left, y, cw, barH, Math.min(12, barH / 2)); ctx.fillStyle = 'rgba(255,255,255,.03)'; ctx.fill();
      roundRect(ctx, left, y, ww, barH, Math.min(12, barH / 2)); ctx.fillStyle = opt.fill; ctx.fill();
      ctx.fillStyle = 'rgba(233,242,246,.95)'; ctx.textAlign = 'right'; ctx.fillText(`${v.toFixed(0)}%`, left + cw - 8, y + barH / 2);
    });
    if (opt.footer) { ctx.fillStyle = 'rgba(149,171,181,.95)'; ctx.textAlign = 'left'; ctx.fillText(opt.footer, 8, h - 18); }
    ctx.restore();
  }

  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function legend(ctx, w, labels, colors) {
    ctx.save();
    ctx.font = '600 12px var(--mono)';
    let x = 54;
    labels.forEach((label, i) => {
      ctx.fillStyle = colors[i];
      ctx.fillRect(x, 18, 12, 12);
      ctx.fillStyle = 'rgba(233,242,246,.88)';
      ctx.fillText(label, x + 18, 28);
      x += 74;
    });
    ctx.restore();
  }

  function xLabels(ctx, w, h, rows) {
    if (!rows.length) return;
    ctx.save();
    ctx.fillStyle = 'rgba(149,171,181,.92)';
    ctx.font = '600 12px var(--mono)';
    ctx.textAlign = 'left';
    ctx.fillText(fmtShort(epochOf(rows[0])), 54, h - 14);
    ctx.textAlign = 'right';
    ctx.fillText(fmtShort(epochOf(rows[rows.length - 1])), w - 22, h - 14);
    ctx.restore();
  }

  function gradient(ctx, a, b) {
    const g = ctx.createLinearGradient(0, 0, 0, 300);
    g.addColorStop(0, a);
    g.addColorStop(1, b);
    return g;
  }

  function setFilter(filter) {
    state.jobFilter = filter;
    [...el.filters.querySelectorAll('[data-filter]')].forEach((b) => b.classList.toggle('is-active', b.dataset.filter === filter));
    renderJobs();
  }

  function openDrawer() {
    state.drawerOpen = true;
    el.drawer.classList.add('is-open');
    el.drawer.setAttribute('aria-hidden', 'false');
    el.drawerBg.hidden = false;
    document.body.classList.add('drawer-open');
  }

  function closeDrawer() {
    state.drawerOpen = false;
    el.drawer.classList.remove('is-open');
    el.drawer.setAttribute('aria-hidden', 'true');
    el.drawerBg.hidden = true;
    document.body.classList.remove('drawer-open');
  }

  function setDrawerMeta(j) {
    el.drawerTitle.textContent = j?.job_id || state.selectedJobId || '请选择任务';
    el.drawerMeta.textContent = j ? `${bucketLabel(j.bucket || j.status)} | ${shortPath(j.workdir || '-')}` : '任务元信息暂不可用';
    el.drawerStatus.textContent = bucketLabel(j?.bucket || j?.status || '-');
    el.drawerStatus.className = `pill ${statusClass(j?.bucket || j?.status)}`;
    el.drawerJobId.textContent = j?.job_id || '-';
    el.drawerGpu.textContent = j?.gpu_text || '-';
    el.drawerUpdated.textContent = j ? ageText(epochJob(j)) : '-';
  }

  function renderDrawerEvents() {
    el.drawerEvents.innerHTML = '';
    if (!state.selectedEvents.length) {
      el.drawerEvents.innerHTML = '<div class="muted-block">没有返回任务事件。</div>';
      return;
    }
    state.selectedEvents.slice(0, 8).forEach((x) => {
      const n = document.createElement('div');
      n.className = 'drawer-event';
      n.innerHTML = `<span>${esc(ageText(epochOf(x)))}</span><div><strong>${esc(bucketLabel(x.event || x.action || x.status || 'event'))}</strong><small>${esc(x.message || x.reason || x.job_id || '任务事件')}</small></div>`;
      el.drawerEvents.appendChild(n);
    });
  }

  function syncControls() {
    const allow = Boolean(state.summary?.allow_write_actions);
    el.drawerKill.disabled = !allow || !state.selectedJobId;
    [el.action, el.reason, el.jobIds, el.signal, el.grace, el.purge, el.cancel].forEach((n) => { n.disabled = !allow; });
    if (!allow) setControlState('只读');
    else if (state.controlState === '只读') setControlState('空闲');
  }

  function setControlState(value) {
    state.controlState = value;
    el.controlState.textContent = value;
    el.controlState.className = `pill ${value === '错误' ? 'bad' : value === '已提交' || value === '正在终止' ? 'ok' : value === '只读' ? 'warn' : 'subtle'}`;
  }

  function healthState(hb, lease) {
    if (!hb) return { label: '离线', cls: 'bad' };
    const ha = ago(hb), la = ago(lease);
    if (ha < 35 && la < 90) return { label: '健康', cls: 'ok' };
    if (ha < 180) return { label: '滞后', cls: 'warn' };
    return { label: '离线', cls: 'bad' };
  }

  function latestEpoch(h) {
    return [...(h.heartbeat || []), ...(h.controller_events || []), ...(h.admin_audit || [])].reduce((m, x) => Math.max(m, epochOf(x)), 0);
  }

  function normalizeHistory(data) {
    return { heartbeat: arr(data?.heartbeat), controller_events: arr(data?.controller_events), admin_audit: arr(data?.admin_audit) };
  }

  function normalizeJobs(data) {
    return arr(data?.jobs || data).map(normalizeJob);
  }

  function normalizeJob(j) {
    const p = j?.payload && typeof j.payload === 'object' ? j.payload : {};
    const bucket = String(j?.bucket || p.status || j?.status || 'queued').toLowerCase();
    const cmd = p.command || j?.command || [];
    const g = p.allocated_gpu_indices || j?.allocated_gpu_indices || p.requested_gpu_indices || j?.requested_gpu_indices || [];
    return { ...p, ...j, payload: p, bucket, status: String(p.status || j?.status || bucket), job_id: String(j?.job_id || p.job_id || j?.name || 'job'), name: String(j?.name || p.name || ''), command_text: Array.isArray(cmd) ? cmd.join(' ') : String(cmd || ''), gpu_text: g.length ? g.join(', ') : '-', allocated_gpu_indices: g, workdir: p.workdir || j?.workdir || '', submitted_at_epoch: p.submitted_at_epoch ?? j?.submitted_at_epoch, started_at_epoch: p.started_at_epoch ?? j?.started_at_epoch, finished_at_epoch: p.finished_at_epoch ?? j?.finished_at_epoch, mtime_epoch: j?.mtime_epoch };
  }

  function arr(v) {
    return Array.isArray(v) ? v : [];
  }

  function rank(bucket) {
    return bucket === 'running' ? 0 : bucket === 'queue' ? 1 : bucket === 'done' ? 2 : bucket === 'failed' ? 3 : bucket === 'cancelled' ? 4 : 5;
  }

  function bucketLabel(bucket) {
    bucket = String(bucket || '').toLowerCase();
    if (bucket === 'running') return '运行中';
    if (bucket === 'queue' || bucket === 'queued') return '排队中';
    if (bucket === 'done') return '已完成';
    if (bucket === 'failed') return '失败';
    if (bucket === 'cancelled') return '已取消';
    if (bucket === 'job_started') return '任务启动';
    if (bucket === 'job_finished') return '任务结束';
    if (bucket === 'job_rejected') return '任务拒绝';
    if (bucket === 'job_launch_failed') return '任务启动失败';
    if (bucket === 'controller_started') return '控制器启动';
    if (bucket === 'controller_stopped') return '控制器停止';
    if (bucket === 'controller_exiting') return '控制器退出';
    if (bucket === 'control_request_completed') return '控制请求完成';
    if (bucket === 'control_request_received') return '收到控制请求';
    if (bucket === 'control_request_failed') return '控制请求失败';
    if (bucket === 'shutdown_requested') return '收到关停请求';
    if (bucket === 'heartbeat') return '心跳';
    if (bucket === 'retire_controller') return 'Retire Controller';
    if (bucket === 'drain_and_stop') return '排空后停止';
    if (bucket === 'cancel_active_job') return '取消活跃任务';
    if (bucket === 'purge_queue') return '清空队列';
    if (bucket === 'event') return '事件';
    return String(bucket || '-');
  }

  function statusClass(bucket) {
    bucket = String(bucket || '').toLowerCase();
    return bucket === 'running' || bucket === 'done' ? 'ok' : bucket === 'queue' ? 'warn' : bucket === 'failed' || bucket === 'cancelled' ? 'bad' : 'subtle';
  }

  function epochOf(x) {
    return Number(x?.updated_at_epoch ?? x?.finished_at_epoch ?? x?.started_at_epoch ?? x?.submitted_at_epoch ?? x?.mtime_epoch ?? 0) || 0;
  }

  function epochJob(x) {
    return Number(x?.started_at_epoch ?? x?.submitted_at_epoch ?? x?.finished_at_epoch ?? x?.mtime_epoch ?? 0) || 0;
  }

  function ago(epoch) {
    return epoch ? Math.max(0, Date.now() / 1000 - Number(epoch)) : Infinity;
  }

  function ageText(epoch) {
    if (!epoch) return '-';
    const d = ago(epoch);
    return d < 10 ? `${d.toFixed(0)}s` : d < 60 ? `${d.toFixed(1)}s` : d < 3600 ? `${(d / 60).toFixed(1)}m` : `${(d / 3600).toFixed(1)}h`;
  }

  function fmtDateTime(epoch) {
    if (!epoch) return '-';
    return new Date(Number(epoch) * 1000).toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function shortPath(path) {
    if (!path || path === '-') return '-';
    const parts = String(path).split(/[\\/]/).filter(Boolean);
    return parts.length <= 3 ? String(path) : `.../${parts.slice(-3).join('/')}`;
  }

  function fmtMiB(v) {
    v = Number(v || 0);
    return Number.isNaN(v) ? '-' : v >= 1024 ? `${(v / 1024).toFixed(1)} GiB` : `${v} MiB`;
  }

  function clamp(v, a, b) {
    return Math.max(a, Math.min(b, v));
  }

  function esc(v) {
    return String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  function gpuColor(util, temp = 0) {
    if (temp >= 82) return '#ff758f';
    if (util >= 85) return '#7ef0e0';
    if (util >= 60) return '#8db8ff';
    if (util >= 30) return '#ffca6b';
    return '#8c98a3';
  }

  function gpuBadge(util, temp, owner, count) {
    return temp >= 82 ? 'bad' : owner ? 'ok' : count ? 'warn' : util >= 60 ? 'warn' : 'neutral';
  }

  function gpuBadgeText(util, temp, owner, count) {
    return temp >= 82 ? '过热' : owner ? '已占用' : count ? '外部进程' : util >= 60 ? '繁忙' : '空闲';
  }

  function gpuNote(owner, count, util) {
    if (owner) return `由任务 ${owner} 管理。`;
    if (count) return `这张卡上有 ${count} 个未归属的进程。`;
    if (util >= 70) return 'GPU 很忙，但没有 controller 归属。';
    if (util >= 20) return 'GPU 有轻度活动，但没有 controller 归属。';
    return '当前卡处于空闲状态。';
  }

  function minuteBuckets(rows) {
    const m = new Map();
    rows.forEach((r) => {
      const min = Math.floor(epochOf(r) / 60) * 60;
      m.set(min, (m.get(min) || 0) + 1);
    });
    return [...m.entries()].sort((a, b) => a[0] - b[0]).map(([epoch, value]) => ({ label: fmtShort(epoch), value }));
  }

  function fmtShort(epoch) {
    return epoch ? new Date(Number(epoch) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-';
  }

  function buildDemo() {
    const now = Math.floor(Date.now() / 1000);
    return {
      summary: {
        runtime_root: '/dev_vepfs/demo/controller/runtime/platform_t-20260322',
        allow_write_actions: true,
        counts: { queue: 4, running_specs: 3, running_meta: 3, done: 25, failed: 2, cancelled: 1, kill: 0, control_queue: 1, control_done: 7, control_failed: 0 },
        controller_state: {
          pid: 2631, hostname: 'demo-controller', updated_at_epoch: now - 18, queue_count: 4, done_count: 25, failed_count: 2, cancelled_count: 1, control_queue_count: 1, active_job_count: 3,
          managed_gpu_indices: ['0', '1', '2', '3', '4', '5', '6', '7'],
          active_jobs: [
            { job_id: 'train_centroid_0123', status: 'running', allocated_gpu_indices: ['0', '1'], started_at_epoch: now - 780, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python train.py --stage centroid'] },
            { job_id: 'train_sparse_4567', status: 'running', allocated_gpu_indices: ['2', '3'], started_at_epoch: now - 620, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python train.py --stage sparse'] },
            { job_id: 'cache_builder', status: 'running', allocated_gpu_indices: ['4'], started_at_epoch: now - 120, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python build_cache.py'] },
          ],
          gpu_status: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => ({ index: i, util_pct: [94, 88, 73, 67, 28, 9, 5, 1][i], mem_used_mb: [46204, 39918, 35130, 33900, 14484, 2200, 2090, 2050][i], mem_total_mb: 81920, temp_c: [72, 69, 67, 65, 41, 28, 27, 26][i] })),
          gpu_processes: [
            { gpu_index: 0, pid: 30121, process_name: 'python', used_memory_mb: 23122, controller_job_id: 'train_centroid_0123' },
            { gpu_index: 1, pid: 30122, process_name: 'python', used_memory_mb: 16642, controller_job_id: 'train_centroid_0123' },
            { gpu_index: 2, pid: 30161, process_name: 'python', used_memory_mb: 18302, controller_job_id: 'train_sparse_4567' },
            { gpu_index: 3, pid: 30162, process_name: 'python', used_memory_mb: 17603, controller_job_id: 'train_sparse_4567' },
            { gpu_index: 4, pid: 30331, process_name: 'python', used_memory_mb: 13120, controller_job_id: 'cache_builder' },
            { gpu_index: 6, pid: 99881, process_name: 'nvidia-smi', used_memory_mb: 30, controller_job_id: null },
          ],
        },
      },
      history: {
        heartbeat: Array.from({ length: 36 }, (_, i) => ({ updated_at_epoch: now - (35 - i) * 8, queue_count: Math.max(0, 6 - Math.floor((35 - i) / 6)), active_job_count: [0, 1, 1, 2, 2, 3, 3][i % 7] })),
        controller_events: [{ updated_at_epoch: now - 520, event: 'job_started', job_id: 'train_centroid_0123' }, { updated_at_epoch: now - 460, event: 'job_started', job_id: 'train_sparse_4567' }, { updated_at_epoch: now - 380, event: 'control_action', reason: 'purge_queue' }],
        admin_audit: [{ updated_at_epoch: now - 240, action: 'retire_controller', reason: '运维演练' }, { updated_at_epoch: now - 80, action: 'control_request', reason: '排空后停止' }],
      },
      jobs: [
        { bucket: 'running', job_id: 'train_centroid_0123', status: 'running', allocated_gpu_indices: ['0', '1'], started_at_epoch: now - 780, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python train.py --stage centroid'] },
        { bucket: 'running', job_id: 'train_sparse_4567', status: 'running', allocated_gpu_indices: ['2', '3'], started_at_epoch: now - 620, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python train.py --stage sparse'] },
        { bucket: 'running', job_id: 'cache_builder', status: 'running', allocated_gpu_indices: ['4'], started_at_epoch: now - 120, workdir: '/dev_vepfs/rc_wu/trellis2_michelangelo_bakeoff', command: ['bash', '-lc', 'python build_cache.py'] },
        { bucket: 'queue', job_id: 'staged_smoke', status: 'queued', requested_gpu_indices: ['5'], submitted_at_epoch: now - 22, workdir: '/dev_vepfs/rc_wu', command: ['bash', '-lc', 'python smoke.py'] },
        { bucket: 'failed', job_id: 'old_failed_worker', status: 'failed', allocated_gpu_indices: ['6', '7'], finished_at_epoch: now - 7400, workdir: '/dev_vepfs/rc_wu', command: ['bash', '-lc', 'python fail.py'] },
      ],
      log: ['[2026-03-22 10:10:11] controller started', '[2026-03-22 10:10:13] job_started train_centroid_0123 gpus=0,1', '[2026-03-22 10:11:01] job_started train_sparse_4567 gpus=2,3', '[2026-03-22 10:12:09] job_started cache_builder gpus=4', '[2026-03-22 10:13:30] heartbeat ok queue=4 running=3', '[2026-03-22 10:13:41] control_action retire_controller accepted', '[2026-03-22 10:13:55] lease renewed'].join('\n'),
    };
  }

})();


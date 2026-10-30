/* restrack profile report — renders window.RESTRACK (a Profiler result) with no dependencies. */
'use strict';
(function () {
  const R = window.RESTRACK;
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nf = new Intl.NumberFormat();
  const fInt = (v) => (v == null ? '–' : nf.format(Math.round(v)));
  function fB(v) {
    if (v == null) return '–';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let n = Math.abs(v); let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${(v < 0 ? '-' : '')}${n.toFixed(i === 0 ? 0 : n < 10 ? 2 : 1)} ${u[i]}`;
  }
  function fS(v) {
    if (v == null) return '–';
    if (v >= 120) return `${Math.floor(v / 60)}m ${(v % 60).toFixed(0)}s`;
    if (v >= 1) return `${v.toFixed(2)} s`;
    if (v >= 0.001) return `${(v * 1000).toFixed(1)} ms`;
    return `${(v * 1e6).toFixed(0)} µs`;
  }
  const fP = (v, d = 1) => (v == null ? '–' : `${v.toFixed(d)}%`);
  const env = R.env || {};
  const memTracing = !!env.memory_tracing;

  // ---------------------------------------------------------------- header
  document.title = `restrack profile — ${R.name}`;
  $('#title').textContent = `restrack profile — ${R.name}`;
  $('#meta').textContent = [new Date(R.started * 1000).toLocaleString(), env.host, env.os, `Python ${env.python}`,
    env.pandas ? `pandas ${env.pandas}` : null, `${env.cpu_logical} logical CPUs`, `${fB(env.memory_total)} RAM`,
    `functions: ${env.functions || 'off'}`, `memory tracing: ${memTracing ? (env.where ? 'on + allocation sites' : 'on') : 'off'}`,
    `CPU counters: ${env.hw_counters ? 'on' : 'off'}`].filter(Boolean).join(' · ');

  const main = $('#main');
  const add = (html) => { main.insertAdjacentHTML('beforeend', html); return main.lastElementChild; };
  function section(title, sub, body) {
    return add(`<div class="section"><div class="sec-head">${esc(title)}${sub ? `<span class="sub">${sub}</span>` : ''}</div><div class="sec-body">${body}</div></div>`);
  }

  // ---------------------------------------------------------------- notes
  if (R.notes && R.notes.length) add(`<ul class="notes">${R.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`);

  // ---------------------------------------------------------------- cards
  const cards = [];
  const card = (k, v, s, extra = '') => cards.push(`<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${s || ''}</div>${extra}</div>`);
  card('Wall time', esc(fS(R.wall)), `${fS(R.cpu)} CPU · ${(R.cores ?? 0).toFixed(2)} cores busy on average`);
  card('Peak memory (RSS)', esc(fB(R.rss_peak)), `start ${fB(R.rss_start)} → end ${fB(R.rss_end)}${env.rss_peak_exact ? ' · exact (kernel)' : ' · sampled'}`);
  if (R.peak_alloc != null) card('Peak Python allocations', esc(fB(R.peak_alloc)), 'traced by tracemalloc (Python, NumPy, pandas buffers)');
  if (R.read_disk != null) {
    const tot = (R.read_disk || 0) + (R.read_cache || 0);
    const hit = R.read_cache != null && tot ? (R.read_cache / tot) * 100 : null;
    const bar = R.read_cache != null && tot ? `<div class="ratio" title="disk vs cache"><i style="width:${100 - hit}%;background:var(--orange)"></i><i style="width:${hit}%;background:var(--blue)"></i></div>` : '';
    card('Reads: disk vs cache', esc(R.read_cache != null ? fP(hit, 0) + ' cached' : fB(R.read_disk)),
      R.read_cache != null ? `${fB(R.read_disk)} from disk · ${fB(R.read_cache)} from OS cache` : 'disk reads (cache split needs Linux)', bar);
    card('Written', esc(fB(R.write_disk)), 'to storage (may still be in the OS write cache)');
  }
  if (R.minflt != null) {
    card('Page faults', esc(fInt(R.minflt + (R.majflt || 0))), R.majflt != null ? `${fInt(R.minflt)} minor (RAM) · <b>${fInt(R.majflt)}</b> major (disk)` : 'minor and major combined (Windows)');
  }
  if (R.hw_ratios) {
    const h = R.hw_ratios;
    const llc = h.llc_miss_pct ?? h.cache_miss_pct;
    card('CPU caches', esc(h.ipc != null ? `IPC ${h.ipc.toFixed(2)}` : '–'),
      [h.l1d_miss_pct != null ? `L1d miss ${fP(h.l1d_miss_pct)}` : null, llc != null ? `LLC miss ${fP(llc)}` : null,
        h.branch_miss_pct != null ? `branch miss ${fP(h.branch_miss_pct, 2)}` : null].filter(Boolean).join(' · '));
  }
  if (R.swap_in || R.swap_out) card('System swap during run', esc(fB((R.swap_in || 0) + (R.swap_out || 0))), `in ${fB(R.swap_in)} · out ${fB(R.swap_out)} (whole machine)`);
  add(`<div class="cards">${cards.join('')}</div>`);

  // ---------------------------------------------------------------- generic sortable table
  function table(cols, rows, { sort = null, rowExtra = null, empty = 'Nothing recorded' } = {}) {
    const id = 't' + Math.random().toString(36).slice(2);
    const st = { k: sort ? sort.k : null, asc: sort ? sort.asc : false };
    const wrap = document.createElement('div');
    function draw() {
      let rs = rows.slice();
      if (st.k) {
        const c = cols.find((x) => x.k === st.k); const g = c.get || ((r) => r[c.k]);
        rs.sort((a, b) => { const va = g(a); const vb = g(b); let d = va == null ? (vb == null ? 0 : 1) : vb == null ? -1 : typeof va === 'string' ? va.localeCompare(vb) : va - vb; return st.asc ? d : -d; });
      }
      let h = `<table id="${id}"><thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}" data-k="${c.k}" title="${esc(c.tip || '')}">${esc(c.label)}${st.k === c.k ? (st.asc ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead><tbody>`;
      if (!rs.length) h += `<tr><td colspan="${cols.length}" class="empty">${esc(empty)}</td></tr>`;
      for (const r of rs) {
        h += `<tr>${cols.map((c) => { const v = c.get ? c.get(r) : r[c.k]; return `<td class="${c.cls || ''}${c.num ? ' num' : ''}">${c.html ? c.html(v, r) : esc(c.fmt ? c.fmt(v, r) : v ?? '')}</td>`; }).join('')}</tr>`;
        if (rowExtra) h += rowExtra(r, cols.length) || '';
      }
      wrap.innerHTML = h + '</tbody></table>';
    }
    wrap.addEventListener('click', (e) => {
      const th = e.target.closest('th[data-k]'); if (!th) return;
      const k = th.dataset.k; st.asc = st.k === k ? !st.asc : false; st.k = k; draw();
    });
    draw();
    wrap.redraw = draw;
    return wrap;
  }
  const pctbar = (v, hot) => `<div class="pctbar${hot ? ' hot' : ''}"><i style="width:${Math.min(100, Math.max(0, v || 0))}%"></i><span>${(v ?? 0).toFixed(1)}%</span></div>`;

  // ---------------------------------------------------------------- timeline
  const S = R.samples || []; const F = R.sample_fields || [];
  const ix = Object.fromEntries(F.map((f, i) => [f, i]));
  if (S.length > 1) {
    const hasTraced = S.some((r) => r[ix.traced] != null);
    const hasIO = S.some((r) => r[ix.read_disk_bps] != null);
    const el = section('Timeline', 'hover for values · bars below show when each section ran', `
      <div class="legend"><span><i class="area" style="background:var(--green)"></i>process memory (RSS)</span>
      ${hasTraced ? '<span><i style="background:var(--purple)"></i>Python allocations (traced)</span>' : ''}
      <span><i style="background:var(--blue)"></i>CPU cores used by the run</span>
      <span><i style="background:#9aa3ad"></i>machine CPU %</span><span><i style="background:#c9a96a"></i>machine memory %</span></div>
      <div class="chart-wrap"><canvas id="cvMain" style="height:220px"></canvas></div>
      ${hasIO ? `<div class="legend"><span><i style="background:var(--orange)"></i>read from disk</span><span><i style="background:var(--blue)"></i>read from OS cache</span></div>
      <div class="chart-wrap"><canvas id="cvIO" style="height:110px"></canvas></div>` : ''}
      <div class="chart-wrap"><canvas id="cvSpans"></canvas></div>`);
    drawTimeline(el, hasTraced, hasIO);
  }

  function setup(cv, h) {
    const dpr = window.devicePixelRatio || 1; const w = cv.clientWidth;
    cv.style.height = h + 'px'; cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); return [ctx, w];
  }
  function drawTimeline(el, hasTraced, hasIO) {
    const T = R.wall || S[S.length - 1][0] || 1;
    const L = 64; const Rp = 46;
    const spans = R.spans || [];
    const depthMax = spans.reduce((m, s) => Math.max(m, s[1]), 0);
    let hover = null;
    function x(t, w) { return L + (t / T) * (w - L - Rp); }
    function draw() {
      // main chart
      const cv = $('#cvMain', el); const H = 220; const [ctx, w] = setup(cv, H);
      const top = 8; const bot = H - 20;
      const memMax = Math.max(...S.map((r) => Math.max(r[ix.rss] || 0, r[ix.traced] || 0))) * 1.08 || 1;
      const coreMax = Math.max(1, Math.ceil(Math.max(...S.map((r) => r[ix.cores] || 0))));
      const y = (v, m) => bot - (v / m) * (bot - top);
      grid(ctx, w, top, bot, T, (i) => fB((memMax * i) / 4), (i) => ((coreMax * i) / 4).toFixed(coreMax <= 4 ? 1 : 0), L, Rp);
      // RSS area
      ctx.beginPath(); ctx.moveTo(x(S[0][0], w), bot);
      for (const r of S) ctx.lineTo(x(r[0], w), y(r[ix.rss], memMax));
      ctx.lineTo(x(S[S.length - 1][0], w), bot); ctx.closePath(); ctx.fillStyle = 'rgba(31,157,31,.18)'; ctx.fill();
      line(ctx, S, (r) => r[ix.rss], (v) => y(v, memMax), w, '#1f9d1f', 1.5);
      if (hasTraced) line(ctx, S, (r) => r[ix.traced], (v) => y(v, memMax), w, '#7b4bb7', 1.5);
      line(ctx, S, (r) => r[ix.sys_cpu], (v) => y(v, 100), w, '#9aa3ad', 1, [3, 3]);
      line(ctx, S, (r) => r[ix.sys_mem], (v) => y(v, 100), w, '#c9a96a', 1, [3, 3]);
      line(ctx, S, (r) => r[ix.cores], (v) => y(v, coreMax), w, '#2c6fd1', 1.5);
      ctx.fillStyle = '#5f6670'; ctx.font = '10px system-ui'; ctx.textAlign = 'left'; ctx.fillText('memory', 4, top + 4);
      ctx.textAlign = 'right'; ctx.fillText('cores', w - 2, top + 4);
      cursor(ctx, w, top, bot);
      // IO chart
      if (hasIO) {
        const cv2 = $('#cvIO', el); const H2 = 110; const [c2, w2] = setup(cv2, H2);
        const t2 = 6; const b2 = H2 - 18;
        const m = Math.max(1, ...S.map((r) => (r[ix.read_disk_bps] || 0) + (r[ix.read_cache_bps] || 0))) * 1.1;
        grid(c2, w2, t2, b2, T, (i) => fB((m * i) / 4) + '/s', null, L, Rp);
        line(c2, S, (r) => r[ix.read_cache_bps], (v) => b2 - (v / m) * (b2 - t2), w2, '#2c6fd1', 1.3);
        line(c2, S, (r) => r[ix.read_disk_bps], (v) => b2 - (v / m) * (b2 - t2), w2, '#e8841a', 1.5);
        cursor(c2, w2, t2, b2);
      }
      // spans (gantt)
      const cv3 = $('#cvSpans', el); const rowH = 16; const H3 = Math.max(24, (depthMax + 1) * rowH + 6); const [c3, w3] = setup(cv3, H3);
      const palette = ['#9cc3f0', '#f6c48f', '#a8dba8', '#d7b8f3', '#f4a9a9', '#b9e3e8', '#e8d99a'];
      const colorOf = {}; let ci = 0;
      for (const sp of spans) {
        const name = sp[0].split('/').pop();
        if (!(name in colorOf)) colorOf[name] = palette[ci++ % palette.length];
        const x0 = x(sp[2], w3); const x1 = Math.max(x0 + 1, x(sp[3], w3)); const yy = 3 + sp[1] * rowH;
        c3.fillStyle = colorOf[name]; c3.fillRect(x0, yy, x1 - x0, rowH - 3);
        if (x1 - x0 > 40) { c3.fillStyle = '#1b1b1b'; c3.font = '10.5px system-ui'; c3.textAlign = 'left'; c3.save(); c3.beginPath(); c3.rect(x0, yy, x1 - x0, rowH); c3.clip(); c3.fillText(name, x0 + 3, yy + rowH - 6); c3.restore(); }
      }
      cursor(c3, w3, 0, H3);
    }
    function grid(ctx, w, top, bot, Tt, leftLbl, rightLbl, l, r) {
      ctx.strokeStyle = '#eceff3'; ctx.lineWidth = 1; ctx.fillStyle = '#6b7380'; ctx.font = '10px system-ui'; ctx.beginPath();
      for (let i = 0; i <= 4; i++) {
        const yy = Math.round(bot - (i / 4) * (bot - top)) + 0.5; ctx.moveTo(l, yy); ctx.lineTo(w - r, yy);
        ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(leftLbl(i), l - 5, yy);
        if (rightLbl) { ctx.textAlign = 'left'; ctx.fillText(rightLbl(i), w - r + 5, yy); }
      }
      const step = niceStep(Tt / 8);
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (let t = 0; t <= Tt + 1e-9; t += step) { const xx = Math.round(x(t, w)) + 0.5; ctx.moveTo(xx, top); ctx.lineTo(xx, bot); ctx.fillText(fS(t).replace(' ', ''), xx, bot + 4); }
      ctx.stroke();
    }
    function niceStep(v) { const e = 10 ** Math.floor(Math.log10(v || 1)); for (const m of [1, 2, 5, 10]) if (m * e >= v) return m * e; return 10 * e; }
    function line(ctx, rows, get, ymap, w, color, lw, dash) {
      ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.setLineDash(dash || []); ctx.beginPath(); let on = false;
      for (const r of rows) { const v = get(r); if (v == null) { on = false; continue; } const xx = x(r[0], w); const yy = ymap(v); if (on) ctx.lineTo(xx, yy); else { ctx.moveTo(xx, yy); on = true; } }
      ctx.stroke(); ctx.setLineDash([]);
    }
    function cursor(ctx, w, top, bot) {
      if (hover == null) return;
      const xx = Math.round(x(hover, w)) + 0.5; ctx.strokeStyle = '#555'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(xx, top); ctx.lineTo(xx, bot); ctx.stroke();
    }
    function onMove(e) {
      const cv = e.target; const r = cv.getBoundingClientRect(); const w = cv.clientWidth;
      const t = ((e.clientX - r.left - L) / (w - L - Rp)) * T;
      if (t < 0 || t > T) { hover = null; $('#tip').hidden = true; draw(); return; }
      hover = t; draw();
      let best = S[0]; for (const s of S) if (Math.abs(s[0] - t) < Math.abs(best[0] - t)) best = s;
      const active = spans.filter((sp) => sp[2] <= t && t <= sp[3]).sort((a, b) => a[1] - b[1]).map((sp) => sp[0]);
      const tip = $('#tip');
      tip.innerHTML = `<b>t = ${esc(fS(best[0]))}</b><br>RSS ${esc(fB(best[ix.rss]))}${best[ix.traced] != null ? ` · traced ${esc(fB(best[ix.traced]))}` : ''}<br>`
        + `${(best[ix.cores] || 0).toFixed(2)} cores · machine CPU ${fP(best[ix.sys_cpu], 0)} · machine RAM ${fP(best[ix.sys_mem], 0)}`
        + (best[ix.read_disk_bps] != null ? `<br>reads: disk ${esc(fB(best[ix.read_disk_bps]))}/s · cache ${esc(fB(best[ix.read_cache_bps]))}/s` : '')
        + (best[ix.children] ? `<br>${best[ix.children]} child process(es)` : '')
        + (active.length ? `<br><span class="muted">in:</span> ${esc(active[active.length - 1])}` : '');
      tip.hidden = false;
      tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
    }
    el.querySelectorAll('canvas').forEach((cv) => { cv.addEventListener('mousemove', onMove); cv.addEventListener('mouseleave', () => { hover = null; $('#tip').hidden = true; draw(); }); });
    window.addEventListener('resize', draw);
    draw();
  }

  // ---------------------------------------------------------------- sections
  const secs = R.sections || [];
  if (secs.length) {
    const hasHW = secs.some((s) => s.hw_ratios && s.hw_ratios.ipc != null);
    const hasRows = secs.some((s) => s.rows_per_s);
    const hasCache = secs.some((s) => s.read_cache != null);
    const memCol = memTracing
      ? { k: 'peak_alloc', label: 'Peak alloc', num: true, fmt: fB, tip: 'Largest Python/NumPy/pandas allocation peak above the section start (tracemalloc)' }
      : { k: 'peak_growth', label: 'Peak RSS growth', num: true, fmt: fB, tip: 'How far process memory rose above its value at the section start (exact on Linux)' };
    const cols = [
      { k: 'path', label: 'Section', cls: 'tree', html: (v, r) => `<span data-path="${esc(r.path)}" style="padding-left:${r.depth * 16}px">${r.sites && r.sites.length ? `<span class="tw">${open.has(r.path) ? '▾' : '▸'}</span>` : '<span class="tw"></span>'}${esc(r.name)}</span>` },
      { k: 'calls', label: 'Calls', num: true, fmt: fInt },
      { k: 'wall', label: 'Wall', num: true, fmt: fS },
      { k: 'pct', label: '% of run', html: (v, r) => pctbar(v, r.hints && r.hints.length) },
      { k: 'self', label: 'Self', num: true, fmt: fS, tip: 'Wall time excluding nested sections' },
      { k: 'cores', label: 'Cores', num: true, fmt: (v) => (v == null ? '–' : v.toFixed(2)), tip: 'CPU time / wall time' },
      memCol,
      { k: 'rss_delta', label: 'Memory kept', num: true, fmt: fB, tip: 'RSS at end minus RSS at start' },
      { k: 'read_disk', label: 'Disk read', num: true, fmt: fB },
      ...(hasCache ? [{ k: 'read_cache', label: 'Cache read', num: true, fmt: fB, tip: 'Read without touching the disk (OS page cache)' }] : []),
      { k: 'majflt', label: 'Hard faults', num: true, fmt: fInt, tip: 'Major page faults: memory that had to come from disk or swap' },
      ...(hasRows ? [{ k: 'rows_per_s', label: 'Rows/s', num: true, fmt: fInt }] : []),
      ...(hasHW ? [
        { k: 'ipc', label: 'IPC', num: true, get: (r) => r.hw_ratios && r.hw_ratios.ipc, fmt: (v) => (v == null ? '–' : v.toFixed(2)), tip: 'Instructions per cycle (higher is better; < 1 usually means waiting on memory)' },
        { k: 'l1', label: 'L1d miss', num: true, get: (r) => r.hw_ratios && r.hw_ratios.l1d_miss_pct, fmt: (v) => fP(v) },
        { k: 'llc', label: 'LLC miss', num: true, get: (r) => r.hw_ratios && (r.hw_ratios.llc_miss_pct ?? r.hw_ratios.cache_miss_pct), fmt: (v) => fP(v) },
      ] : []),
    ];
    const open = new Set();
    const tbl = table(cols, secs, {
      sort: null,
      rowExtra: (r, n) => {
        let h = '';
        if (r.hints && r.hints.length) h += r.hints.map((t) => `<tr><td colspan="${n}" class="hint" style="padding-left:${30 + r.depth * 16}px">${esc(t)}</td></tr>`).join('');
        if (r.sites && r.sites.length && open.has(r.path)) {
          h += `<tr class="sites"><td colspan="${n}"><div class="pad"><b>Memory held at the section's ${r.sites_kind === 'peak' ? 'peak' : 'end'}, by line of your code</b>`
            + `<table><thead><tr><th class="num">Size</th><th class="num">Blocks</th><th>Where</th><th>Code</th><th>Allocated inside</th></tr></thead><tbody>`
            + r.sites.map((s) => `<tr><td class="num">${esc(fB(s.size))}</td><td class="num">${fInt(s.count)}</td><td>${esc(s.file)}:${s.line}</td><td class="code">${esc(s.code)}</td><td class="callee">${esc(s.via || '')}</td></tr>`).join('')
            + '</tbody></table></div></td></tr>';
        }
        return h;
      },
    });
    const el = section('Sections', 'time and resources of each marked block · ⚑ = observation · click ▸ for allocation sites', '');
    el.querySelector('.sec-body').appendChild(tbl);
    tbl.addEventListener('click', (e) => {
      const span = e.target.closest('td.tree [data-path]'); if (!span) return;
      const row = secs.find((x) => x.path === span.dataset.path);
      if (!row || !row.sites || !row.sites.length) return;
      if (open.has(row.path)) open.delete(row.path); else open.add(row.path);
      tbl.redraw();
    });
  } else {
    section('Sections', '', '<div class="empty">No sections were marked. Wrap code in <code>with prof.section("name"):</code>, decorate functions with <code>@restrack.track</code>, or use <code>restrack profile --track-module mymodule</code>.</div>');
  }

  // ---------------------------------------------------------------- hot lines
  const lines = R.lines || [];
  if (lines.length) {
    const el = section('Hot lines in your code', `sampled every ${fS(env.sample_interval)} · ${fInt(R.stack_samples)} samples · time includes the library calls each line makes`, '');
    el.querySelector('.sec-body').appendChild(table([
      { k: 'time', label: 'Time', num: true, fmt: fS },
      { k: 'pct', label: '% of run', html: (v) => pctbar(v, v > 20) },
      { k: 'where', label: 'Where', get: (r) => `${r.file}:${r.line}`, html: (v, r) => `${esc(v)} <span class="muted">${esc(r.func)}</span>` },
      { k: 'code', label: 'Code', cls: 'code' },
      { k: 'callee', label: 'Spends its time in', get: (r) => (r.calls_into[0] ? r.calls_into[0].callee : ''), html: (v, r) => r.calls_into.map((c) => `<span class="callee">${esc(c.callee)}</span> <span class="muted">${esc(fS(c.time))}</span>`).join('<br>') },
      { k: 'section', label: 'Section' },
    ], lines, { sort: { k: 'time', asc: false } }));
  }

  // ---------------------------------------------------------------- functions
  const fn = R.functions;
  if (fn && (fn.user_cum.length || fn.edges.length)) {
    const views = [['Your functions (total time)', fn.user_cum, [{ k: 'label', label: 'Function' }, { k: 'cum', label: 'Total', num: true, fmt: fS }, ...(fn.mode === 'cprofile' ? [{ k: 'self', label: 'Self', num: true, fmt: fS }, { k: 'calls', label: 'Calls', num: true, fmt: fInt }] : [])], 'cum']];
    if (fn.mode === 'cprofile' && fn.user_self.length) views.push(['Your functions (self time)', fn.user_self, [{ k: 'label', label: 'Function' }, { k: 'self', label: 'Self', num: true, fmt: fS }, { k: 'cum', label: 'Total', num: true, fmt: fS }, { k: 'calls', label: 'Calls', num: true, fmt: fInt }], 'self']);
    if (fn.edges.length) views.push(['Library calls made by your code', fn.edges, [{ k: 'caller', label: 'Your function' }, { k: 'callee', label: 'Library call', html: (v) => `<span class="callee">${esc(v)}</span>` }, { k: 'cum', label: 'Time', num: true, fmt: fS }, ...(fn.mode === 'cprofile' ? [{ k: 'calls', label: 'Calls', num: true, fmt: fInt }] : [])], 'cum']);
    if (fn.mode === 'cprofile' && fn.lib_self.length) views.push(['Library internals (self time)', fn.lib_self, [{ k: 'label', label: 'Function' }, { k: 'self', label: 'Self', num: true, fmt: fS }, { k: 'calls', label: 'Calls', num: true, fmt: fInt }], 'self']);
    const el = section('Functions', fn.mode === 'cprofile' ? 'exact (cProfile)' : 'sampled', `<div class="tabs">${views.map((v, i) => `<button data-i="${i}" class="${i ? '' : 'active'}">${esc(v[0])}</button>`).join('')}</div><div class="fn-body"></div>`);
    const body = el.querySelector('.fn-body');
    const show = (i) => { body.innerHTML = ''; const v = views[i]; body.appendChild(table(v[2], v[1], { sort: { k: v[3], asc: false } })); el.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', +b.dataset.i === i)); };
    el.querySelector('.tabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) show(+b.dataset.i); });
    show(0);
  }

  // ---------------------------------------------------------------- caches, counters, gauges
  const caches = Object.entries(R.caches || {}); const rates = Object.entries(R.hit_rates || {});
  const counters = Object.entries(R.counters || {}); const gauges = Object.entries(R.gauges || {});
  if (caches.length || rates.length || counters.length || gauges.length) {
    let h = '<div class="grid2">';
    const crow = [];
    for (const [n, c] of caches) crow.push({ name: n, kind: 'lru_cache', hits: c.hits, misses: c.misses, hit_rate: c.hit_rate, size: c.size != null ? `${fInt(c.size)}${c.maxsize ? ' / ' + fInt(c.maxsize) : ''}` : '' });
    for (const [n, c] of rates) crow.push({ name: n, kind: 'counters', hits: c.hits, misses: c.misses, hit_rate: c.hit_rate, size: '' });
    h += '<div class="c-caches"></div><div class="c-counters"></div></div><div class="c-gauges"></div>';
    const el = section('Caches, counters & gauges', 'from watch_cache(), count() and gauge()', h);
    if (crow.length) el.querySelector('.c-caches').appendChild(table([{ k: 'name', label: 'Cache' }, { k: 'kind', label: 'Source' }, { k: 'hits', label: 'Hits', num: true, fmt: fInt }, { k: 'misses', label: 'Misses', num: true, fmt: fInt }, { k: 'hit_rate', label: 'Hit rate', html: (v) => (v == null ? '–' : pctbar(v, v < 50)) }, { k: 'size', label: 'Size' }], crow, { sort: { k: 'hits', asc: false } }));
    if (counters.length) el.querySelector('.c-counters').appendChild(table([{ k: 'name', label: 'Counter' }, { k: 'value', label: 'Value', num: true, fmt: (v) => (Number.isInteger(v) ? fInt(v) : v.toPrecision(6)) }], counters.map(([name, value]) => ({ name, value })), { sort: { k: 'name', asc: true } }));
    if (gauges.length) {
      el.querySelector('.c-gauges').appendChild(table([{ k: 'name', label: 'Gauge' }, { k: 'n', label: 'Points', num: true }, { k: 'last', label: 'Last', num: true, fmt: (v) => (v == null ? '–' : String(+v.toPrecision(6))) },
        { k: 'min', label: 'Min', num: true, fmt: (v) => String(+v.toPrecision(6)) }, { k: 'max', label: 'Max', num: true, fmt: (v) => String(+v.toPrecision(6)) },
        { k: 'spark', label: 'Over time', html: (v) => v }],
      gauges.map(([name, pts]) => { const vs = pts.map((p) => p[1]); return { name, n: pts.length, last: vs[vs.length - 1], min: Math.min(...vs), max: Math.max(...vs), spark: spark(pts) }; })));
    }
  }
  function spark(pts) {
    if (pts.length < 2) return '';
    const w = 160; const h = 26; const vs = pts.map((p) => p[1]); const lo = Math.min(...vs); const hi = Math.max(...vs) || lo + 1; const T = R.wall || pts[pts.length - 1][0] || 1;
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${((p[0] / T) * w).toFixed(1)},${(h - 2 - ((p[1] - lo) / (hi - lo || 1)) * (h - 4)).toFixed(1)}`).join('');
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="#2c6fd1" stroke-width="1.4"/></svg>`;
  }

  // ---------------------------------------------------------------- DataFrames (memory + numeric precision)
  for (const df of R.dataframes || []) {
    const el = section(`DataFrame: ${df.name}`, `${fInt(df.rows)} rows × ${fInt(df.columns_count)} columns · ${fB(df.total)} (index ${fB(df.index)}) · could be ${fB(df.est_total)}, saving ${fB(df.saving)} (${df.saving_pct}%) · inspected at ${fS(df.t)}`, '');
    el.querySelector('.sec-body').appendChild(table([
      { k: 'column', label: 'Column' }, { k: 'dtype', label: 'dtype' },
      { k: 'memory', label: 'Memory', num: true, fmt: fB }, { k: 'pct', label: '% of frame', html: (v) => pctbar(v, v > 30) },
      { k: 'nulls', label: 'Nulls', num: true, fmt: fInt }, { k: 'unique_ratio', label: 'Unique %', num: true, fmt: (v) => (v == null ? '' : (v * 100).toFixed(1) + '%') },
      { k: 'suggested', label: 'Suggested dtype', html: (v) => (v ? `<b>${esc(v)}</b>` : '<span class="muted">keep</span>') },
      { k: 'saving', label: 'Saving', num: true, fmt: (v) => (v ? fB(v) : '') },
      { k: 'note', label: 'Why / precision', cls: 'wrap' },
    ], df.columns, { sort: { k: 'memory', asc: false } }));
  }

  add(`<footer>Generated by restrack · ${new Date().toLocaleString()} · pid ${esc(env.pid)} · ${esc((env.argv || []).join(' '))}</footer>`);
}());

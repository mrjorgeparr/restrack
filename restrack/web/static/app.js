/* restrack — Resource Monitor-style frontend (vanilla JS, no build step). */
'use strict';

// ------------------------------------------------------------------ state
const HIST = 60;                  // seconds shown in the live graphs
const $ = (s, r = document) => r.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem('restrack.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('restrack.' + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } },
};
const S = {
  info: null, snap: null, points: [], tick: 0,
  selected: new Set(), handles: {},
  sort: store.get('sort', {}), collapsed: store.get('collapsed', {}),
  tab: store.get('tab', 'overview'), viewsCollapsed: store.get('views', false),
  paused: false, conn: 'connecting', graphs: [], gpuCount: 0,
};

// ------------------------------------------------------------------ formatting
const nf = new Intl.NumberFormat();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fInt = (v) => (v == null ? '' : nf.format(Math.round(v)));
const fKB = (v) => (v == null ? '' : nf.format(Math.round(v / 1024)));
const fMB = (v) => (v == null ? '' : nf.format(Math.round(v / 1048576)));
const fPct = (v, d = 0) => (v == null ? '–' : `${v.toFixed(d)}%`);
function fBytes(v, suffix = 'B') {
  if (v == null) return '–';
  const u = ['', 'K', 'M', 'G', 'T', 'P'];
  let i = 0; let n = Math.abs(v);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${(v < 0 ? -n : n).toFixed(i === 0 ? 0 : n < 10 ? 1 : 0)} ${u[i]}${suffix}`;
}
function fBits(bytesPerSec) {
  if (bytesPerSec == null) return '–';
  const u = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps'];
  let n = bytesPerSec * 8; let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(i === 0 ? 0 : n < 10 ? 1 : 0)} ${u[i]}`;
}
function fDur(sec) {
  if (!sec) return '–';
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400); const h = Math.floor((sec % 86400) / 3600); const m = Math.floor((sec % 3600) / 60);
  return (d ? `${d}d ` : '') + `${h}h ${String(m).padStart(2, '0')}m`;
}
const fStatus = (s) => (s === 'stopped' ? 'Suspended' : s ? s[0].toUpperCase() + s.slice(1) : '');
function niceMax(v) {
  if (!(v > 0)) return 1;
  const e = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * e >= v) return m * e;
  return 10 * e;
}

// ------------------------------------------------------------------ derived graph points
function toPoint(s) {
  const pl = s.processes && s.processes.list;
  let selCpu = null; let selDisk = null;
  if (pl && S.selected.size) {
    selCpu = 0; selDisk = 0;
    for (const p of pl) if (S.selected.has(p.pid)) { selCpu += p.cpu; selDisk += p.read_bps + p.write_bps; }
  }
  const disks = {}; for (const d of s.disk.disks) disks[d.name] = { bps: d.read_bps + d.write_bps, busy: d.busy_percent };
  const nics = {}; for (const n of s.network.interfaces) nics[n.name] = n.sent_bps + n.recv_bps;
  const conns = Object.values(s.network.connections_by_status || {}).reduce((a, b) => a + b, 0);
  return {
    ts: s.ts, cpu: s.cpu.percent, freq: s.cpu.freq_percent, per: s.cpu.per_cpu,
    mem: s.memory.percent, swap: s.memory.swap_percent, faults: s.memory.hard_faults_s,
    disk: s.disk.read_bps + s.disk.write_bps, diskBusy: s.disk.busy_percent, disks,
    net: s.network.sent_bps + s.network.recv_bps, netUtil: s.network.utilization_percent, nics,
    tcp: s.network.connections_available === false ? null : conns,
    selCpu, selDisk, gpu: (s.gpu || []).map((g) => g.util_percent),
  };
}

// ------------------------------------------------------------------ live graph (black, scrolling grid)
const COLORS = { green: '#39e639', blue: '#3a8bff', orange: '#ff9b26' };
class Graph {
  constructor(o) {
    this.o = Object.assign({ height: 92, max: 100, auto: false, fmt: (v) => `${Math.round(v)}%`, value: null, small: false }, o);
    const el = document.createElement('div');
    el.className = 'graph' + (this.o.small ? ' small' : '');
    el.innerHTML = '<div class="g-head"><b></b><span class="gmax"></span></div><canvas></canvas>'
      + '<div class="g-foot"><span>60 Seconds</span><span>0</span></div>';
    this.el = el; this.cv = $('canvas', el); this.head = $('b', el); this.maxEl = $('.gmax', el);
    this.cv.style.height = this.o.height + 'px';
  }
  update(points) {
    const { o, cv } = this;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth; const h = o.height;
    if (!w) return;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h);

    const pts = points.slice(-HIST);
    let max = o.max;
    if (o.auto) {
      let m = 0;
      for (const p of pts) for (const s of o.series) if (!s.pct) { const v = s.get(p); if (v > m) m = v; }
      max = niceMax(Math.max(m * 1.1, o.minAuto || 1));
    }
    // grid: 10 rows; vertical lines every 6 samples, scrolling with time
    ctx.strokeStyle = '#0d5a0d'; ctx.lineWidth = 1; ctx.beginPath();
    for (let i = 1; i < 10; i++) { const y = Math.round((h * i) / 10) + 0.5; ctx.moveTo(0, y); ctx.lineTo(w, y); }
    const step = w / (HIST - 1);
    const off = (S.tick % 6) * step;
    for (let x = w - off; x > 0; x -= step * 6) { const xx = Math.round(x) + 0.5; ctx.moveTo(xx, 0); ctx.lineTo(xx, h); }
    ctx.stroke();

    for (const s of o.series) {
      const scale = s.pct ? 100 : max;
      ctx.strokeStyle = COLORS[s.color] || s.color; ctx.lineWidth = 1.5; ctx.beginPath();
      let started = false;
      pts.forEach((p, i) => {
        const v = s.get(p);
        if (v == null || Number.isNaN(v)) { started = false; return; }
        const x = w - (pts.length - 1 - i) * step;
        const y = h - 1 - Math.min(1, Math.max(0, v / scale)) * (h - 2);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    const last = pts[pts.length - 1];
    const cur = last && o.value ? ` — ${o.value(last)}` : '';
    this.head.textContent = o.title + cur;
    this.maxEl.textContent = o.auto ? o.fmt(max) : o.maxLabel || '100%';
  }
}

// ------------------------------------------------------------------ tables
function procRows(list, { filter = null, selectedOnly = false } = {}) {
  let rows = list || [];
  if (selectedOnly && S.selected.size) rows = rows.filter((r) => S.selected.has(r.pid));
  else if (filter) rows = rows.filter(filter);
  return rows;
}
function renderTable(id, cols, rows, { checkable = false, defSort = null, empty = 'No data', rowKey = null } = {}) {
  const st = S.sort[id] || defSort || { k: cols[0].k, asc: true };
  const col = cols.find((c) => c.k === st.k) || cols[0];
  const get = col.get || ((r) => r[col.k]);
  const sorted = rows.slice().sort((a, b) => {
    const va = get(a); const vb = get(b);
    let c;
    if (va == null && vb == null) c = 0; else if (va == null) c = 1; else if (vb == null) c = -1;
    else c = typeof va === 'string' ? va.localeCompare(vb, undefined, { sensitivity: 'base' }) : va - vb;
    return st.asc ? c : -c;
  });
  let ordered = sorted;
  if (checkable && S.selected.size) {   // like Resource Monitor: checked processes float to the top
    const top = sorted.filter((r) => S.selected.has(r.pid));
    ordered = top.concat(sorted.filter((r) => !S.selected.has(r.pid)));
  }
  const allChecked = checkable && rows.length > 0 && rows.every((r) => S.selected.has(r.pid));
  let h = `<table class="grid" data-table="${id}"><thead><tr>`;
  if (checkable) h += `<th class="chk"><input type="checkbox" data-all="1" ${allChecked ? 'checked' : ''} title="Select all"></th>`;
  for (const c of cols) {
    const cls = [c.num ? 'num' : '', c.k === col.k ? 'sorted' + (st.asc ? ' asc' : '') : ''].join(' ');
    h += `<th class="${cls}" data-k="${c.k}" title="${esc(c.tip || '')}">${esc(c.label)}</th>`;
  }
  h += '</tr></thead><tbody>';
  if (!ordered.length) h += `<tr><td class="empty" colspan="${cols.length + (checkable ? 1 : 0)}">${esc(empty)}</td></tr>`;
  const nSel = checkable ? ordered.filter((r) => S.selected.has(r.pid)).length : 0;
  ordered.forEach((r, i) => {
    const checked = checkable && S.selected.has(r.pid);
    const cls = [checked ? 'checked' : '', checked && i === nSel - 1 ? 'sep' : '', r.status === 'stopped' ? 'suspended' : ''].join(' ');
    const key = rowKey ? rowKey(r) : r.pid;
    h += `<tr class="${cls}"${r.pid != null ? ` data-pid="${r.pid}"` : ''} data-key="${esc(key)}">`;
    if (checkable) h += `<td class="chk"><input type="checkbox" data-pid="${r.pid}" ${checked ? 'checked' : ''}></td>`;
    for (const c of cols) {
      const v = c.get ? c.get(r) : r[c.k];
      const txt = c.fmt ? c.fmt(v, r) : v ?? '';
      h += `<td class="${c.num ? 'num' : ''}" title="${esc(c.title ? c.title(r) : '')}">${esc(txt)}</td>`;
    }
    h += '</tr>';
  });
  return h + '</tbody></table>';
}

const C = {
  image: { k: 'name', label: 'Image', title: (r) => `${r.name} (PID ${r.pid})${r.user ? ' — ' + r.user : ''}` },
  pid: { k: 'pid', label: 'PID', num: true },
  user: { k: 'user', label: 'User' },
  status: { k: 'status', label: 'Status', fmt: fStatus },
  threads: { k: 'threads', label: 'Threads', num: true },
  handles: { k: 'handles', label: 'Handles', num: true, tip: 'Open handles (Windows) / file descriptors (POSIX)', fmt: fInt },
  cpu: { k: 'cpu', label: 'CPU', num: true, fmt: (v) => Math.round(v), tip: '% of total CPU capacity' },
  cpuAvg: { k: 'cpu_avg', label: 'Average CPU', num: true, fmt: (v) => v.toFixed(2), tip: '60-second exponential average' },
  faults: { k: 'faults_s', label: 'Hard Faults/sec', num: true, fmt: fInt, tip: 'Linux: major faults; Windows: all page faults' },
  commit: { k: 'vms', label: 'Commit (KB)', num: true, fmt: fKB, tip: 'Virtual memory size' },
  ws: { k: 'rss', label: 'Working Set (KB)', num: true, fmt: fKB, tip: 'Resident set size' },
  shareable: { k: 'shared', label: 'Shareable (KB)', num: true, fmt: fKB },
  priv: { k: 'private', label: 'Private (KB)', num: true, fmt: fKB },
  read: { k: 'read_bps', label: 'Read (B/sec)', num: true, fmt: fInt },
  write: { k: 'write_bps', label: 'Write (B/sec)', num: true, fmt: fInt },
  total: { k: 'io_total', label: 'Total (B/sec)', num: true, get: (r) => r.read_bps + r.write_bps, fmt: fInt },
  cacheRead: { k: 'read_cache_bps', label: 'Cache Read (B/sec)', num: true, fmt: fInt, tip: 'Bytes read without touching the disk: OS page cache (plus pipes/sockets). Linux only' },
  tcp: { k: 'tcp', label: 'TCP Connections', num: true },
  listen: { k: 'listening', label: 'Listening Ports', num: true },
};
const CPU_COLS = [C.image, C.pid, C.user, C.status, C.threads, C.cpu, C.cpuAvg];
const MEM_COLS = [C.image, C.pid, C.faults, C.commit, C.ws, C.shareable, C.priv];
const DISK_COLS = [C.image, C.pid, C.read, C.write, C.total];
const NETP_COLS = [C.image, C.pid, C.tcp, C.listen];

// ------------------------------------------------------------------ layout helpers
function section(id, title, { raw = false, tall = false } = {}) {
  const col = S.collapsed[id] ? ' collapsed' : '';
  const body = raw ? `<div id="body-${id}"></div>` : `<div class="tbl-wrap${tall ? ' tall' : ''}" id="body-${id}"></div>`;
  return `<div class="section${col}" data-sec="${id}"><div class="sec-head"><span class="sec-title">${esc(title)}</span>`
    + `<span id="sum-${id}" class="sec-sums"></span><span class="chev">${S.collapsed[id] ? '&#9660;' : '&#9650;'}</span></div>`
    + `<div class="sec-body">${body}<div class="note" id="note-${id}" hidden></div></div></div>`;
}
const sum = (...items) => items.filter(Boolean).map(([c, t]) => `<span class="sec-sum"><i class="sq ${c}"></i>${esc(t)}</span>`).join('');
function setHTML(id, html) { const el = document.getElementById(id); if (el && el.innerHTML !== html) el.innerHTML = html; }
function setNote(id, text) { const el = document.getElementById('note-' + id); if (!el) return; el.hidden = !text; if (text) el.textContent = text; }
function isOpen(id) { return !S.collapsed[id] && document.getElementById('body-' + id); }

// ------------------------------------------------------------------ tabs
const TABS = {
  overview: {
    html: () => section('ov-cpu', 'CPU') + section('ov-disk', 'Disk') + section('ov-net', 'Network')
      + section('ov-mem', 'Memory') + section('ov-gpu', 'GPU'),
    graphs: () => [
      new Graph({ title: 'CPU', value: (p) => fPct(p.cpu), series: [
        { get: (p) => p.freq, color: 'blue', pct: true }, { get: (p) => p.cpu, color: 'green' }, { get: (p) => p.selCpu, color: 'orange' }] }),
      new Graph({ title: 'Disk', auto: true, minAuto: 1048576, fmt: (v) => fBytes(v) + '/s', value: (p) => fBytes(p.disk) + '/s',
        series: [{ get: (p) => p.diskBusy, color: 'blue', pct: true }, { get: (p) => p.disk, color: 'green' }, { get: (p) => p.selDisk, color: 'orange' }] }),
      new Graph({ title: 'Network', auto: true, minAuto: 125000, fmt: (v) => fBits(v), value: (p) => fBits(p.net),
        series: [{ get: (p) => p.netUtil, color: 'blue', pct: true }, { get: (p) => p.net, color: 'green' }] }),
      new Graph({ title: 'Memory', value: (p) => fPct(p.mem) + ' used', series: [
        { get: (p) => p.swap, color: 'blue', pct: true }, { get: (p) => p.mem, color: 'green' }] }),
      ...gpuGraphs(),
    ],
    render(s) {
      const pl = s.processes ? s.processes.list : [];
      setHTML('sum-ov-cpu', sum(['green', `${fPct(s.cpu.percent)} CPU Usage`],
        s.cpu.freq_percent != null && ['blue', `${fPct(s.cpu.freq_percent)} Maximum Frequency`]));
      if (isOpen('ov-cpu')) setHTML('body-ov-cpu', renderTable('ov-cpu', CPU_COLS, pl, { checkable: true, defSort: { k: 'cpu', asc: false } }));
      setHTML('sum-ov-disk', sum(['green', `${fBytes(s.disk.read_bps + s.disk.write_bps)}/sec Disk I/O`],
        ['blue', `${fPct(s.disk.busy_percent)} Highest Active Time`]));
      if (isOpen('ov-disk')) setHTML('body-ov-disk', renderTable('ov-disk', DISK_COLS,
        procRows(pl, { selectedOnly: true, filter: (r) => r.read_bps + r.write_bps > 0 }),
        { checkable: true, defSort: { k: 'io_total', asc: false }, empty: 'No processes with disk activity' }));
      setHTML('sum-ov-net', sum(['green', `${fBits(s.network.sent_bps + s.network.recv_bps)} Network I/O`],
        s.network.utilization_percent != null && ['blue', `${fPct(s.network.utilization_percent)} Network Utilization`]));
      if (isOpen('ov-net')) {
        setHTML('body-ov-net', renderTable('ov-net', NETP_COLS, procRows(pl, { selectedOnly: true, filter: (r) => r.tcp + r.listening > 0 }),
          { checkable: true, defSort: { k: 'tcp', asc: false }, empty: 'No processes with network activity' }));
        setNote('ov-net', s.network.connections_available === false ? 'Connection details need elevated privileges on this OS.' : '');
      }
      const m = s.memory;
      setHTML('sum-ov-mem', sum(m.hard_faults_s != null ? ['green', `${fInt(m.hard_faults_s)} Hard Faults/sec`] : ['green', `${fBytes(m.swap_in_bps + m.swap_out_bps)}/sec Swap I/O`],
        ['blue', `${fPct(m.percent)} Used Physical Memory`]));
      if (isOpen('ov-mem')) setHTML('body-ov-mem', renderTable('ov-mem', MEM_COLS, procRows(pl, { selectedOnly: true }),
        { checkable: true, defSort: { k: 'rss', asc: false } }));
      const gsec = document.querySelector('[data-sec="ov-gpu"]');
      if (gsec) gsec.hidden = !(s.gpu && s.gpu.length);
      if (s.gpu && s.gpu.length) {
        setHTML('sum-ov-gpu', sum(['green', `${fPct(Math.max(...s.gpu.map((g) => g.util_percent || 0)))} GPU Usage`]));
        if (isOpen('ov-gpu')) setHTML('body-ov-gpu', renderTable('ov-gpu', [
          { k: 'name', label: 'GPU' }, { k: 'util_percent', label: 'Utilization', num: true, fmt: (v) => fPct(v) },
          { k: 'mem_used', label: 'Memory Used (MB)', num: true, fmt: fMB }, { k: 'mem_total', label: 'Memory Total (MB)', num: true, fmt: fMB },
          { k: 'temperature', label: 'Temp (°C)', num: true }, { k: 'power_w', label: 'Power (W)', num: true }], s.gpu, { rowKey: (r) => r.index }));
      }
    },
  },

  cpu: {
    html: () => section('cpu-procs', 'Processes', { tall: true }) + section('cpu-handles', 'Associated Handles')
      + section('cpu-sched', 'Scheduler & Load', { raw: true }) + section('cpu-sensors', 'Sensors'),
    graphs: () => {
      const n = (S.snap && S.snap.cpu.per_cpu.length) || (S.info && S.info.cpu_logical) || 1;
      const g = [new Graph({ title: 'CPU - Total', value: (p) => fPct(p.cpu), series: [
        { get: (p) => p.freq, color: 'blue', pct: true }, { get: (p) => p.cpu, color: 'green' }, { get: (p) => p.selCpu, color: 'orange' }] })];
      for (let i = 0; i < n; i++) g.push(new Graph({ title: `CPU ${i}`, small: true, height: 54, value: (p) => fPct(p.per && p.per[i]), series: [{ get: (p) => p.per && p.per[i], color: 'green' }], group: 'percore' }));
      return g;
    },
    render(s) {
      const pl = s.processes ? s.processes.list : [];
      setHTML('sum-cpu-procs', sum(['green', `${fPct(s.cpu.percent)} CPU Usage`],
        s.cpu.freq_percent != null && ['blue', `${fPct(s.cpu.freq_percent)} Maximum Frequency`]));
      if (isOpen('cpu-procs')) setHTML('body-cpu-procs', renderTable('cpu-procs', [C.image, C.pid, C.user, C.status, C.threads, C.handles, C.cpu, C.cpuAvg],
        pl, { checkable: true, defSort: { k: 'cpu', asc: false } }));
      // associated handles (open files + sockets) for checked processes
      const sel = [...S.selected].slice(0, 8);
      setHTML('sum-cpu-handles', S.selected.size ? sum(['gray', `${S.selected.size} selected`]) : '');
      if (isOpen('cpu-handles')) {
        if (S.tick % 3 === 0) refreshHandles(sel);
        const rows = [];
        for (const pid of sel) {
          const d = S.handles[pid];
          if (!d) continue;
          if (d.error) { rows.push({ pid, name: '', type: '—', handle: d.error, _k: pid + 'err' }); continue; }
          d.open_files.forEach((f, i) => rows.push({ pid, name: d.name, type: 'File', handle: f.path, _k: `${pid}f${i}` }));
          d.connections.forEach((c, i) => rows.push({ pid, name: d.name, type: c.type, handle: `${c.laddr}${c.raddr ? ' → ' + c.raddr : ''}${c.status && c.status !== 'NONE' ? ' (' + c.status + ')' : ''}`, _k: `${pid}c${i}` }));
        }
        setHTML('body-cpu-handles', renderTable('cpu-handles', [{ k: 'name', label: 'Image' }, C.pid, { k: 'type', label: 'Handle Type' }, { k: 'handle', label: 'Handle Name', title: (r) => r.handle }],
          rows, { rowKey: (r) => r._k, empty: S.selected.size ? 'No open files or sockets visible (may need elevated privileges)' : 'Check a process above to see its open files and sockets' }));
      }
      if (isOpen('cpu-sched')) {
        const c = s.cpu; const p = s.processes || {};
        const kv = [
          ['Utilization', fPct(c.percent, 1)], ['Frequency', c.freq_mhz ? `${fInt(c.freq_mhz)} / ${fInt(c.freq_max_mhz)} MHz` : '–'],
          ['Load average (1/5/15)', c.load_avg ? c.load_avg.map((x) => x.toFixed(2)).join(' / ') : '–'],
          ['Context switches/sec', fInt(c.ctx_switches_s)], ['Interrupts/sec', fInt(c.interrupts_s)],
          ['System calls/sec', c.syscalls_s == null ? '–' : fInt(c.syscalls_s)],
          ['Processes', fInt(p.count)], ['Threads', fInt(p.threads)], ['Handles', fInt(p.handles)],
          ['Logical / physical cores', `${S.info ? S.info.cpu_logical : '?'} / ${S.info ? S.info.cpu_physical ?? '?' : '?'}`],
          ['Up time', fDur(s.uptime)],
          ...Object.entries(c.times_percent || {}).map(([k, v]) => [`Time: ${k}`, fPct(v, 1)]),
        ];
        setHTML('body-cpu-sched', `<div class="kv">${kv.map(([k, v]) => `<div><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('')}</div>`);
      }
      const sen = s.sensors || {};
      const srows = [
        ...(sen.temperatures || []).map((t, i) => ({ _k: 't' + i, kind: 'Temperature', label: `${t.chip} / ${t.label}`, value: `${t.current?.toFixed(1)} °C`, high: t.high, crit: t.critical })),
        ...(sen.fans || []).map((f, i) => ({ _k: 'f' + i, kind: 'Fan', label: `${f.chip} / ${f.label}`, value: `${fInt(f.rpm)} RPM` })),
      ];
      if (sen.battery) srows.push({ _k: 'b', kind: 'Battery', label: sen.battery.plugged ? 'Plugged in' : 'On battery', value: fPct(sen.battery.percent), high: sen.battery.secs_left != null ? fDur(sen.battery.secs_left) + ' left' : null });
      const hot = (sen.temperatures || []).reduce((m, t) => Math.max(m, t.current || 0), 0);
      setHTML('sum-cpu-sensors', hot ? sum(['orange', `${hot.toFixed(0)} °C Hottest`]) : '');
      if (isOpen('cpu-sensors')) setHTML('body-cpu-sensors', renderTable('cpu-sensors', [{ k: 'kind', label: 'Type' }, { k: 'label', label: 'Sensor' },
        { k: 'value', label: 'Current' }, { k: 'high', label: 'High', num: true }, { k: 'crit', label: 'Critical', num: true }], srows,
      { rowKey: (r) => r._k, empty: 'No sensors exposed by this OS/hardware (psutil supports them on Linux/FreeBSD; battery on most laptops)' }));
    },
  },

  memory: {
    html: () => section('mem-procs', 'Processes', { tall: true }) + section('mem-phys', 'Physical Memory', { raw: true }),
    graphs: () => [
      new Graph({ title: 'Used Physical Memory', value: (p) => fPct(p.mem), series: [{ get: (p) => p.mem, color: 'green' }] }),
      new Graph({ title: 'Swap Usage', value: (p) => fPct(p.swap), series: [{ get: (p) => p.swap, color: 'blue' }] }),
      new Graph({ title: 'Hard Faults/sec', auto: true, minAuto: 10, fmt: (v) => fInt(v), value: (p) => fInt(p.faults), series: [{ get: (p) => p.faults, color: 'green' }] }),
    ],
    render(s) {
      const m = s.memory; const pl = s.processes ? s.processes.list : [];
      setHTML('sum-mem-procs', sum(['green', `${fPct(m.percent)} Used Physical Memory`]));
      if (isOpen('mem-procs')) setHTML('body-mem-procs', renderTable('mem-procs', MEM_COLS, pl, { checkable: true, defSort: { k: 'rss', asc: false } }));
      const inUse = m.total - m.available;
      const standby = Math.max(0, m.available - m.free);
      const free = Math.max(0, m.total - inUse - standby);
      setHTML('sum-mem-phys', sum(['green', `${fMB(inUse)} MB In Use`], ['blue', `${fMB(m.available)} MB Available`]));
      if (isOpen('mem-phys')) {
        const seg = (v, c) => `<div style="width:${(v / m.total) * 100}%;background:${c}"></div>`;
        const item = (c, label, v) => `<div class="item"><i class="sq ${c}"></i><div><div class="lbl">${label}</div><div class="val">${fMB(v)} MB</div></div></div>`;
        const stats = [['Available', `${fMB(m.available)} MB`], ['Cached', `${fMB((m.cached || 0) + (m.buffers || 0) || standby)} MB`],
          ['Total', `${fMB(m.total)} MB`], ['Swap', m.swap_total ? `${fMB(m.swap_used)} / ${fMB(m.swap_total)} MB` : 'none'],
          ['Swap in / out', `${fBytes(m.swap_in_bps)}/s · ${fBytes(m.swap_out_bps)}/s`],
          ...(m.dirty != null ? [['Dirty (not yet written)', `${fMB(m.dirty)} MB`], ['Being written back', `${fMB(m.writeback)} MB`]] : []),
          ...(s.disk.cache_hit_percent != null ? [['Reads served from cache', fPct(s.disk.cache_hit_percent, 1)]] : [])];
        setHTML('body-mem-phys', `<div class="membar">${seg(inUse, 'var(--green)')}${seg(standby, 'var(--blue)')}${seg(free, '#8ec5f3')}</div>`
          + `<div class="memlegend">${item('green', 'In Use', inUse)}${item('blue', 'Standby / Cache', standby)}${item('lblue', 'Free', free)}</div>`
          + `<div class="memstats">${stats.map(([k, v]) => `<span>${k}</span><span>${esc(v)}</span>`).join('')}</div>`);
      }
    },
  },

  disk: {
    html: () => section('disk-procs', 'Processes with Disk Activity') + section('disk-act', 'Disk Activity') + section('disk-storage', 'Storage'),
    graphs: () => {
      const g = [new Graph({ title: 'Disk', auto: true, minAuto: 1048576, fmt: (v) => fBytes(v) + '/s', value: (p) => fBytes(p.disk) + '/s',
        series: [{ get: (p) => p.diskBusy, color: 'blue', pct: true }, { get: (p) => p.disk, color: 'green' }, { get: (p) => p.selDisk, color: 'orange' }] })];
      for (const d of (S.snap ? S.snap.disk.disks : [])) {
        g.push(new Graph({ title: `${d.name} Active Time`, small: true, height: 54, value: (p) => fPct(p.disks[d.name] && p.disks[d.name].busy), series: [{ get: (p) => p.disks[d.name] && p.disks[d.name].busy, color: 'green' }] }));
      }
      return g;
    },
    render(s) {
      const pl = s.processes ? s.processes.list : [];
      const ch = s.disk.cache_hit_percent;
      setHTML('sum-disk-procs', sum(['gray', `${pl.filter((r) => r.read_bps + r.write_bps > 0).length} Active Processes`],
        ch != null && ['lblue', `${fPct(ch)} of reads served from cache`]));
      if (isOpen('disk-procs')) {
        const dcols = s.disk.cache_hit_percent !== undefined && s.processes && s.processes.read_logical_bps != null
          ? [C.image, C.pid, C.read, C.cacheRead, C.write, C.total] : DISK_COLS;
        setHTML('body-disk-procs', renderTable('disk-procs', dcols, procRows(pl, { selectedOnly: true, filter: (r) => r.read_bps + r.write_bps + (r.read_cache_bps || 0) > 0 }),
          { checkable: true, defSort: { k: 'io_total', asc: false }, empty: 'No processes with disk activity' }));
        setNote('disk-procs', pl.some((r) => !r.io_available) ? 'Per-process I/O is not available for some processes (needs privileges, or unsupported on macOS).' : '');
      }
      setHTML('sum-disk-act', sum(['green', `${fBytes(s.disk.read_bps + s.disk.write_bps)}/sec Disk I/O`], ['blue', `${fPct(s.disk.busy_percent)} Highest Active Time`]));
      if (isOpen('disk-act')) setHTML('body-disk-act', renderTable('disk-act', [{ k: 'name', label: 'Disk' },
        { k: 'read_bps', label: 'Read (B/sec)', num: true, fmt: fInt }, { k: 'write_bps', label: 'Write (B/sec)', num: true, fmt: fInt },
        { k: 'tot', label: 'Total (B/sec)', num: true, get: (r) => r.read_bps + r.write_bps, fmt: fInt },
        { k: 'read_iops', label: 'Reads/sec', num: true, fmt: (v) => v.toFixed(1) }, { k: 'write_iops', label: 'Writes/sec', num: true, fmt: (v) => v.toFixed(1) },
        { k: 'busy_percent', label: 'Active Time (%)', num: true, fmt: (v) => v.toFixed(1) }], s.disk.disks, { rowKey: (r) => r.name, empty: 'No disk counters (common in containers)' }));
      const lowest = s.disk.partitions.reduce((m, p) => Math.min(m, 100 - p.percent), 100);
      setHTML('sum-disk-storage', sum(['blue', `${s.disk.partitions.length} Logical Disks`], ['green', `${lowest.toFixed(0)}% Lowest Free Space`]));
      if (isOpen('disk-storage')) setHTML('body-disk-storage', renderTable('disk-storage', [{ k: 'mountpoint', label: 'Logical Disk' }, { k: 'device', label: 'Device' },
        { k: 'fstype', label: 'File System' }, { k: 'free', label: 'Available Space (MB)', num: true, fmt: fMB }, { k: 'total', label: 'Total Space (MB)', num: true, fmt: fMB },
        { k: 'pfree', label: '% Free', num: true, get: (r) => 100 - r.percent, fmt: (v) => v.toFixed(0) }], s.disk.partitions, { rowKey: (r) => r.mountpoint }));
    },
  },

  network: {
    html: () => section('net-procs', 'Processes with Network Activity') + section('net-act', 'Network Activity')
      + section('net-tcp', 'TCP Connections') + section('net-listen', 'Listening Ports'),
    graphs: () => {
      const g = [new Graph({ title: 'Network', auto: true, minAuto: 125000, fmt: (v) => fBits(v), value: (p) => fBits(p.net),
        series: [{ get: (p) => p.netUtil, color: 'blue', pct: true }, { get: (p) => p.net, color: 'green' }] })];
      for (const n of (S.snap ? S.snap.network.interfaces : []).filter((x) => !x.loopback && x.is_up)) {
        g.push(new Graph({ title: n.name, small: true, height: 54, auto: true, minAuto: 12500, fmt: (v) => fBits(v), value: (p) => fBits(p.nics[n.name]), series: [{ get: (p) => p.nics[n.name], color: 'green' }] }));
      }
      g.push(new Graph({ title: 'TCP Connections', auto: true, minAuto: 10, height: 64, fmt: (v) => fInt(v), value: (p) => (p.tcp == null ? 'n/a' : fInt(p.tcp)), series: [{ get: (p) => p.tcp, color: 'green' }] }));
      return g;
    },
    render(s) {
      const pl = s.processes ? s.processes.list : []; const n = s.network;
      const active = pl.filter((r) => r.tcp + r.listening > 0);
      setHTML('sum-net-procs', sum(['gray', `${active.length} Processes`]));
      if (isOpen('net-procs')) {
        setHTML('body-net-procs', renderTable('net-procs', NETP_COLS, procRows(pl, { selectedOnly: true, filter: (r) => r.tcp + r.listening > 0 }),
          { checkable: true, defSort: { k: 'tcp', asc: false }, empty: 'No processes with network activity' }));
        setNote('net-procs', n.connections_available === false ? 'Connection details need elevated privileges on this OS (e.g. sudo on macOS).'
          : 'psutil cannot attribute bytes/sec to individual processes, so this shows socket counts.');
      }
      setHTML('sum-net-act', sum(['green', `${fBits(n.sent_bps + n.recv_bps)} Network I/O`], n.utilization_percent != null && ['blue', `${fPct(n.utilization_percent)} Network Utilization`]));
      if (isOpen('net-act')) setHTML('body-net-act', renderTable('net-act', [{ k: 'name', label: 'Adapter' },
        { k: 'is_up', label: 'Status', fmt: (v) => (v ? 'Up' : v === false ? 'Down' : '?') }, { k: 'ipv4', label: 'IPv4' },
        { k: 'speed_mbps', label: 'Link Speed (Mbps)', num: true, fmt: fInt },
        { k: 'sent_bps', label: 'Send (B/sec)', num: true, fmt: fInt }, { k: 'recv_bps', label: 'Receive (B/sec)', num: true, fmt: fInt },
        { k: 'tot', label: 'Total (B/sec)', num: true, get: (r) => r.sent_bps + r.recv_bps, fmt: fInt },
        { k: 'errors', label: 'Errors', num: true, fmt: fInt }, { k: 'drops', label: 'Drops', num: true, fmt: fInt }], n.interfaces, { rowKey: (r) => r.name }));
      const tcp = S.selected.size ? (n.tcp || []).filter((c) => S.selected.has(c.pid)) : n.tcp || [];
      setHTML('sum-net-tcp', sum(['gray', `${(n.tcp || []).length} Active TCP Connections`]));
      if (isOpen('net-tcp')) setHTML('body-net-tcp', renderTable('net-tcp', [{ k: 'name', label: 'Image' }, C.pid, { k: 'laddr', label: 'Local Address' },
        { k: 'lport', label: 'Local Port', num: true }, { k: 'raddr', label: 'Remote Address' }, { k: 'rport', label: 'Remote Port', num: true },
        { k: 'status', label: 'State' }], tcp, { rowKey: (r) => `${r.laddr}:${r.lport}-${r.raddr}:${r.rport}`, empty: n.connections_available === false ? 'Needs elevated privileges' : S.selected.size ? 'No TCP connections for the checked processes' : 'No TCP connections' }));
      const lst = S.selected.size ? (n.listening || []).filter((c) => S.selected.has(c.pid)) : n.listening || [];
      setHTML('sum-net-listen', sum(['gray', `${(n.listening || []).length} Listening Ports`]));
      if (isOpen('net-listen')) setHTML('body-net-listen', renderTable('net-listen', [{ k: 'name', label: 'Image' }, C.pid, { k: 'address', label: 'Address' },
        { k: 'port', label: 'Port', num: true }, { k: 'protocol', label: 'Protocol' }, { k: 'family', label: 'Family' }], lst,
      { rowKey: (r) => `${r.protocol}${r.address}:${r.port}`, defSort: { k: 'port', asc: true }, empty: n.connections_available === false ? 'Needs elevated privileges' : S.selected.size ? 'No listening ports for the checked processes' : 'No listening ports' }));
    },
  },

  history: { html: () => History.html(), graphs: () => [], render() {} },
};

function gpuGraphs() {
  const gpus = (S.snap && S.snap.gpu) || [];
  return gpus.map((g, i) => new Graph({ title: `GPU ${i}`, value: (p) => fPct(p.gpu[i]), series: [{ get: (p) => p.gpu && p.gpu[i], color: 'green' }] }));
}

let handlesBusy = false;
async function refreshHandles(pids) {
  if (handlesBusy || !pids.length) return;
  handlesBusy = true;
  try {
    const res = await Promise.all(pids.map((pid) => fetch(`/api/process/${pid}`).then((r) => r.json().then((j) => [pid, r.ok ? j : { error: j.error }])).catch(() => [pid, { error: 'unreachable' }])));
    S.handles = Object.fromEntries(res);
  } finally { handlesBusy = false; }
}

// ------------------------------------------------------------------ tab build + render
function buildTab() {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === S.tab));
  const tab = TABS[S.tab] || TABS.overview;
  $('#content').innerHTML = tab.html();
  const g = $('#graphs'); g.innerHTML = '';
  S.graphs = tab.graphs();
  let per = null;
  for (const gr of S.graphs) {
    if (gr.o.group === 'percore') {
      if (!per) { per = document.createElement('div'); per.className = 'percore'; g.appendChild(per); }
      per.appendChild(gr.el);
    } else g.appendChild(gr.el);
  }
  g.classList.toggle('grid2', S.graphs.filter((x) => x.o.group === 'percore').length > 1);
  $('#views').hidden = S.tab === 'history';
  $('#views').classList.toggle('collapsed', !!S.viewsCollapsed);
  S.gpuCount = (S.snap && S.snap.gpu || []).length;
  if (S.tab === 'history') History.enter(); else History.leave();
  render();
}
function render() {
  const s = S.snap;
  if (s && TABS[S.tab]) TABS[S.tab].render(s);
  for (const g of S.graphs) g.update(S.points);
  status();
}
function status() {
  const st = $('#stConn');
  st.className = 'conn ' + (S.paused ? 'paused' : S.conn === 'live' ? 'live' : S.conn === 'down' ? 'down' : '');
  st.textContent = S.paused ? 'Monitoring stopped' : S.conn === 'live' ? `Live · every ${S.info ? S.info.interval : 1}s` : S.conn === 'down' ? 'Disconnected — retrying…' : 'Connecting…';
  if (S.info) {
    $('#stHost').textContent = `${S.info.hostname} · ${S.info.os} ${S.info.os_release} · ${S.info.cpu_model}`;
    $('#titleHost').textContent = `— ${S.info.hostname}`;
  }
  if (S.snap) {
    $('#stUptime').textContent = `Up ${fDur(S.snap.uptime)}`;
    const p = S.snap.processes;
    $('#stProcs').textContent = p ? `${p.count} processes · ${fInt(p.threads)} threads` : '';
  }
  $('#stStore').textContent = S.info && S.info.storage ? 'Logging to disk' : 'Not logging to disk';
}

// ------------------------------------------------------------------ data intake
function onSnapshot(s) {
  if (S.paused) return;
  S.snap = s; S.tick++;
  S.points.push(toPoint(s));
  if (S.points.length > HIST + 5) S.points.splice(0, S.points.length - HIST - 5);
  for (const pid of [...S.selected]) if (s.processes && !s.processes.list.some((p) => p.pid === pid)) S.selected.delete(pid);
  const gpuN = (s.gpu || []).length;
  if (gpuN !== S.gpuCount && S.tab === 'overview') { buildTab(); return; }
  if (S.tab !== 'history') render();
}
function connect() {
  const es = new EventSource('/api/stream');
  es.addEventListener('snapshot', (e) => { S.conn = 'live'; try { onSnapshot(JSON.parse(e.data)); } catch (err) { console.error(err); } });
  es.onopen = () => { S.conn = 'live'; status(); };
  es.onerror = () => { S.conn = 'down'; status(); };
}
async function init() {
  try {
    S.info = await (await fetch('/api/info')).json();
    const hist = await (await fetch(`/api/history?n=${HIST}`)).json();
    S.points = hist.map(toPoint);
  } catch (e) { console.error(e); }
  buildTab();
  connect();
}

// ------------------------------------------------------------------ interaction
$('#tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]'); if (!b) return;
  S.tab = b.dataset.tab; store.set('tab', S.tab); buildTab();
});
$('#viewsToggle').addEventListener('click', () => {
  S.viewsCollapsed = !S.viewsCollapsed; store.set('views', S.viewsCollapsed);
  $('#views').classList.toggle('collapsed', S.viewsCollapsed);
  $('#viewsToggle').innerHTML = S.viewsCollapsed ? '&#9664;' : '&#9654;';
  render();
});
const content = $('#content');
content.addEventListener('click', (e) => {
  const th = e.target.closest('th[data-k]');
  if (th) {
    const id = th.closest('table').dataset.table; const k = th.dataset.k;
    const cur = S.sort[id];
    const numeric = th.classList.contains('num');
    S.sort[id] = cur && cur.k === k ? { k, asc: !cur.asc } : { k, asc: !numeric };
    store.set('sort', S.sort); render(); return;
  }
  const head = e.target.closest('.sec-head');
  if (head) {
    const sec = head.parentElement; const id = sec.dataset.sec;
    S.collapsed[id] = !S.collapsed[id]; store.set('collapsed', S.collapsed);
    sec.classList.toggle('collapsed', S.collapsed[id]);
    $('.chev', sec).innerHTML = S.collapsed[id] ? '&#9660;' : '&#9650;';
    render();
  }
});
content.addEventListener('change', (e) => {
  const cb = e.target;
  if (cb.type !== 'checkbox') return;
  if (cb.dataset.all) {
    const pids = [...cb.closest('table').querySelectorAll('tbody input[data-pid]')].map((x) => +x.dataset.pid);
    pids.forEach((p) => (cb.checked ? S.selected.add(p) : S.selected.delete(p)));
  } else if (cb.dataset.pid) {
    const pid = +cb.dataset.pid;
    if (cb.checked) S.selected.add(pid); else S.selected.delete(pid);
  }
  S.handles = {};
  if (S.tab === 'cpu') refreshHandles([...S.selected].slice(0, 8)).then(render);
  render();
});
content.addEventListener('contextmenu', (e) => {
  const tr = e.target.closest('tr[data-pid]'); if (!tr) return;
  e.preventDefault(); showProcMenu(+tr.dataset.pid, e.clientX, e.clientY);
});
content.addEventListener('dblclick', (e) => {
  const tr = e.target.closest('tr[data-pid]'); if (tr) showProperties(+tr.dataset.pid);
});

// context menu ------------------------------------------------------
const ctx = $('#ctxmenu');
function showProcMenu(pid, x, y) {
  const p = S.snap && S.snap.processes && S.snap.processes.list.find((r) => r.pid === pid);
  const can = S.info && S.info.allow_control;
  const dis = can ? '' : ' disabled title="Start with --allow-control (or allow_control = true) to enable"';
  ctx.innerHTML = `<button data-a="terminate"${dis}>End Process</button><button data-a="kill"${dis}>End Process (Force)</button>`
    + `<button data-a="suspend"${dis}>Suspend Process</button><button data-a="resume"${dis}>Resume Process</button><hr>`
    + `<button data-a="select">${S.selected.has(pid) ? 'Uncheck' : 'Check'} Process</button><button data-a="props">Properties…</button>`;
  ctx.dataset.pid = pid; ctx.dataset.name = p ? p.name : '';
  ctx.hidden = false;
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(x, innerWidth - r.width - 4) + 'px';
  ctx.style.top = Math.min(y, innerHeight - r.height - 4) + 'px';
}
ctx.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-a]'); if (!b || b.disabled) return;
  const pid = +ctx.dataset.pid; const name = ctx.dataset.name; const a = b.dataset.a;
  ctx.hidden = true;
  if (a === 'props') return showProperties(pid);
  if (a === 'select') { if (S.selected.has(pid)) S.selected.delete(pid); else S.selected.add(pid); return render(); }
  if ((a === 'terminate' || a === 'kill') && !confirm(`End ${name} (PID ${pid})?\n\nUnsaved data in the process will be lost.`)) return;
  try {
    const r = await fetch(`/api/process/${pid}/${a}`, { method: 'POST', headers: { 'X-Restrack-Control': '1' } });
    const j = await r.json();
    if (!r.ok) alert(`Could not ${a} ${name} (PID ${pid}): ${j.error}`);
  } catch (err) { alert('Request failed: ' + err); }
});
document.addEventListener('click', (e) => {
  if (!ctx.contains(e.target)) ctx.hidden = true;
  const m = e.target.closest('.menu');
  document.querySelectorAll('.menu.open').forEach((x) => { if (x !== m) x.classList.remove('open'); });
  if (m && !e.target.closest('.dropdown')) m.classList.toggle('open');
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { ctx.hidden = true; closeModal(); } });
window.addEventListener('blur', () => { ctx.hidden = true; });

// modal ------------------------------------------------------------
const modal = $('#modal');
function openModal(title, html) { $('#modalTitle').textContent = title; $('#modalBody').innerHTML = html; modal.hidden = false; }
function closeModal() { modal.hidden = true; }
$('#modalClose').addEventListener('click', closeModal);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

async function showProperties(pid) {
  openModal(`Process ${pid}`, 'Loading…');
  try {
    const r = await fetch(`/api/process/${pid}`); const d = await r.json();
    if (!r.ok) { $('#modalBody').textContent = d.error; return; }
    const kv = [['Name', d.name], ['PID / Parent', `${d.pid} / ${d.ppid ?? '?'} (${d.parent_name ?? '?'})`], ['User', d.user], ['Status', fStatus(d.status)],
      ['Started', d.created ? new Date(d.created * 1000).toLocaleString() : '?'], ['Threads', d.threads], ['Children', d.children], ['Nice / priority', d.nice],
      ['Unique memory (USS)', d.uss != null ? fBytes(d.uss) : 'n/a'],
      ['Context switches', d.ctx_switches ? `${fInt(d.ctx_switches.voluntary)} vol / ${fInt(d.ctx_switches.involuntary)} invol` : 'n/a'],
      ['CPU affinity', d.cpu_affinity ? d.cpu_affinity.join(', ') : 'n/a']];
    $('#modalTitle').textContent = `${d.name} (PID ${pid}) — Properties`;
    $('#modalBody').innerHTML = `<div class="kv">${kv.map(([k, v]) => `<div><span>${esc(k)}</span><span>${esc(v ?? '')}</span></div>`).join('')}</div>`
      + `<h4>Executable</h4><pre>${esc(d.exe || 'n/a (access denied)')}</pre><h4>Command line</h4><pre>${esc((d.cmdline || []).join(' ') || 'n/a')}</pre>`
      + `<h4>Working directory</h4><pre>${esc(d.cwd || 'n/a')}</pre>`
      + `<h4>Open files (${d.open_files.length})</h4><pre>${esc(d.open_files.map((f) => f.path).join('\n') || '—')}</pre>`
      + `<h4>Sockets (${d.connections.length})</h4><pre>${esc(d.connections.map((c) => `${c.type} ${c.laddr}${c.raddr ? ' → ' + c.raddr : ''} ${c.status}`).join('\n') || '—')}</pre>`;
  } catch (e) { $('#modalBody').textContent = String(e); }
}

// menubar ----------------------------------------------------------
$('#menubar').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  document.querySelectorAll('.menu.open').forEach((x) => x.classList.remove('open'));
  const a = b.dataset.act;
  if (a === 'stop') { S.paused = true; status(); }
  if (a === 'start') { S.paused = false; status(); }
  if (a === 'export-live' && S.snap) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(S.snap, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `restrack-snapshot-${Math.floor(S.snap.ts)}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  if (a === 'about') {
    const i = S.info || {};
    openModal('About restrack', `<div class="kv">${[['Host', i.hostname], ['OS', i.platform], ['CPU', i.cpu_model], ['Python / psutil', `${i.python} / ${i.psutil}`],
      ['Sample interval', `${i.interval}s`], ['Config file', i.config_source || '(defaults)'], ['Disk logging', i.storage ? 'on' : 'off'],
      ['Process control', i.allow_control ? 'enabled' : 'disabled']].map(([k, v]) => `<div><span>${esc(k)}</span><span>${esc(v ?? '')}</span></div>`).join('')}</div>`
      + '<p>restrack mimics Windows Resource Monitor in the browser using psutil. Check processes to filter the other tables and draw their usage in orange; right-click a process for actions; the History tab shows the long-term log.</p>');
  }
});
document.querySelectorAll('.menu').forEach((m) => {
  if (m.dataset.menu === 'monitor') {
    const upd = () => { $('[data-act="stop"]', m).disabled = S.paused; $('[data-act="start"]', m).disabled = !S.paused; };
    m.addEventListener('click', upd);
  }
});
window.addEventListener('resize', () => { render(); if (S.tab === 'history') History.draw(); });

// ------------------------------------------------------------------ History tab (long-term log)
const RANGES = { '15m': 900, '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 };
const HCHARTS = [
  { id: 'cpu', title: 'CPU', unit: '%', max: 100, series: [{ k: 'cpu', label: 'average', color: '#1f9d1f', band: 'cpu_max' }, { k: 'gpu', label: 'GPU', color: '#8e44ad' }] },
  { id: 'mem', title: 'Memory', unit: '%', max: 100, series: [{ k: 'mem', label: 'physical used', color: '#1f9d1f', band: 'mem_max' }, { k: 'swap', label: 'swap used', color: '#2c6fd1' }] },
  { id: 'disk', title: 'Disk I/O', unit: 'B/s', series: [{ k: 'disk_read', label: 'read', color: '#1f9d1f', band: 'disk_read_max' }, { k: 'disk_write', label: 'write', color: '#2c6fd1', band: 'disk_write_max' }] },
  { id: 'busy', title: 'Disk Active Time & Cache Hits', unit: '%', max: 100, series: [{ k: 'disk_busy', label: 'highest disk active', color: '#1f9d1f', band: 'disk_busy_max' }, { k: 'cache_hit', label: 'reads served from cache', color: '#2c6fd1' }] },
  { id: 'net', title: 'Network', unit: 'bps', series: [{ k: 'net_recv', label: 'receive', color: '#1f9d1f', band: 'net_recv_max' }, { k: 'net_sent', label: 'send', color: '#2c6fd1', band: 'net_sent_max' }] },
  { id: 'load', title: 'Load & Temperature', unit: '', series: [{ k: 'load1', label: 'load (1 min)', color: '#1f9d1f' }, { k: 'temp_max', label: 'hottest sensor °C', color: '#e8841a', band: 'temp_max_max' }] },
];
const History = {
  range: store.get('hrange', '1h'), start: 0, end: 0, live: true, data: null, events: [], bounds: null,
  hoverTs: null, drag: null, timer: null, charts: [], top: null, topTs: null,
  html() {
    if (!(S.info && S.info.storage)) {
      return '<div class="section"><div class="sec-head"><span class="sec-title">History</span></div><div class="hist-empty">'
        + 'Disk logging is off. Enable <code>[storage] enabled = true</code> in your config (create one with <code>restrack config init</code>) and restart <code>restrack web</code>.</div></div>';
    }
    const seg = Object.keys(RANGES).map((k) => `<button data-r="${k}" class="${this.range === k ? 'active' : ''}">${k}</button>`).join('');
    return `<div class="section"><div class="hist-toolbar"><span class="seg" id="hRanges">${seg}</span>`
      + '<span class="seg"><button id="hBack" title="Earlier">&#9664;</button><button id="hFwd" title="Later">&#9654;</button><button id="hNow" title="Jump to now and follow live">Now</button></span>'
      + '<span class="muted" id="hLabel"></span><span class="spacer"></span><span class="muted" id="hInfo"></span><a class="btn" id="hExport" href="#">Export CSV</a></div>'
      + '<div class="hint">Drag across a chart to zoom in · hover for values · click a moment to see which processes were busy. Shaded bands show the peak within each point.</div>'
      + HCHARTS.map((c) => `<div class="hchart" data-c="${c.id}"><div class="hc-head"><b>${c.title}</b>${c.series.filter((s) => s.k !== 'gpu' || (S.snap && S.snap.gpu && S.snap.gpu.length)).map((s) => `<span class="legend-i"><i style="background:${s.color}"></i>${s.label}</span>`).join('')}`
        + `${c.series.some((s) => s.band) ? '<span class="legend-i"><i class="band" style="background:#1f9d1f"></i>peak</span>' : ''}</div><canvas></canvas></div>`).join('')
      + '</div>' + section('h-top', 'Top Processes at Selected Time') + section('h-events', 'Events & Alerts');
  },
  enter() {
    if (!(S.info && S.info.storage)) return;
    this.charts = HCHARTS.map((c) => ({ def: c, el: document.querySelector(`.hchart[data-c="${c.id}"]`) }))
      .map((c) => Object.assign(c, { cv: $('canvas', c.el) }));
    for (const c of this.charts) this.bindChart(c);
    $('#hRanges').addEventListener('click', (e) => { const b = e.target.closest('button[data-r]'); if (b) this.setRange(b.dataset.r); });
    $('#hBack').addEventListener('click', () => this.shift(-1));
    $('#hFwd').addEventListener('click', () => this.shift(1));
    $('#hNow').addEventListener('click', () => this.setRange(RANGES[this.range] ? this.range : '1h'));
    if (this.live || !this.end) this.setWindow();
    this.renderTables();
    this.load();
    clearInterval(this.timer);
    this.timer = setInterval(() => { if (this.live) { this.setWindow(); this.load(); } }, 15000);
  },
  leave() { clearInterval(this.timer); this.timer = null; },
  setWindow() { const span = RANGES[this.range] || (this.end - this.start) || 3600; this.end = Date.now() / 1000; this.start = this.end - span; },
  setRange(r) {
    this.range = r; this.live = true; store.set('hrange', r);
    document.querySelectorAll('#hRanges button').forEach((b) => b.classList.toggle('active', b.dataset.r === r));
    this.setWindow(); this.load();
  },
  shift(dir) {
    const span = this.end - this.start; this.start += dir * span * 0.75; this.end += dir * span * 0.75;
    const now = Date.now() / 1000;
    if (this.end >= now) { this.end = now; this.start = now - span; this.live = true; } else this.live = false;
    this.load();
  },
  zoom(t0, t1) {
    this.start = Math.min(t0, t1); this.end = Math.max(t0, t1); this.live = false; this.range = 'custom';
    document.querySelectorAll('#hRanges button').forEach((b) => b.classList.remove('active'));
    this.load();
  },
  async load() {
    const q = `start=${this.start}&end=${this.end}`;
    const width = Math.max(200, Math.min(1500, (this.charts[0] && this.charts[0].cv.clientWidth) || 800));
    try {
      const [data, events, bounds] = await Promise.all([
        fetch(`/api/range?${q}&points=${Math.round(width / 2)}`).then((r) => r.json()),
        fetch(`/api/events?${q}`).then((r) => r.json()),
        fetch('/api/storage').then((r) => r.json()),
      ]);
      this.data = data; this.events = events; this.bounds = bounds;
    } catch (e) { console.error(e); return; }
    if (S.tab !== 'history') return;
    $('#hExport').href = `/api/export.csv?${q}`;
    const fmt = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    $('#hLabel').textContent = `${fmt(this.start)} → ${this.live ? 'now (live)' : fmt(this.end)} · ${this.data.series.length} points of ${humanSpan(this.data.bucket)}`;
    const b = this.bounds;
    $('#hInfo').textContent = b.rows ? `${fInt(b.rows)} rows since ${new Date(b.first * 1000).toLocaleDateString()} · ${fBytes(b.bytes)} · keeps ${b.retention_days || '∞'} d` : 'No rows yet — the first is written within a few seconds';
    this.draw(); this.renderTables();
  },
  draw() {
    if (!this.data) return;
    for (const c of this.charts) drawHChart(c, this);
  },
  bindChart(c) {
    const cv = c.cv;
    const tsAt = (x) => { const L = HPAD.l; const w = cv.clientWidth - L - HPAD.r; return this.start + ((x - L) / w) * (this.end - this.start); };
    cv.addEventListener('mousemove', (e) => {
      const x = e.offsetX;
      this.hoverTs = tsAt(x);
      if (this.drag) this.drag.x1 = x;
      this.draw();
      this.tooltip(c, e);
    });
    cv.addEventListener('mouseleave', () => { this.hoverTs = null; this.draw(); $('#tooltip').hidden = true; });
    c.tsAt = tsAt;
    cv.addEventListener('mousedown', (e) => { e.preventDefault(); this.drag = { chart: c, x0: e.offsetX, x1: e.offsetX }; });
  },
  endDrag(e) {
    if (!this.drag) return;
    const { chart, x0, x1 } = this.drag; this.drag = null;
    if (Math.abs(x1 - x0) > 6) this.zoom(chart.tsAt(x0), chart.tsAt(x1));
    else if (e.target === chart.cv) this.loadTop(chart.tsAt(x0));
    this.draw();
  },
  nearest(ts) {
    const s = this.data && this.data.series; if (!s || !s.length) return null;
    let best = null; let bd = Infinity;
    for (const r of s) { const d = Math.abs(r.ts - ts); if (d < bd) { bd = d; best = r; } }
    return bd <= Math.max(this.data.bucket * 1.5, 30) ? best : null;
  },
  tooltip(c, e) {
    const tt = $('#tooltip'); const r = this.nearest(this.hoverTs);
    if (!r) { tt.hidden = true; return; }
    const f = unitFmt(c.def.unit);
    tt.innerHTML = `<b>${new Date(r.ts * 1000).toLocaleString()}</b><br>` + c.def.series.filter((s) => r[s.k] != null).map((s) => `${s.label}: <b>${r[s.k] == null ? '–' : f(r[s.k])}</b>${s.band && r[s.band] != null ? ` (peak ${f(r[s.band])})` : ''}`).join('<br>');
    tt.hidden = false;
    tt.style.left = Math.min(e.clientX + 14, innerWidth - tt.offsetWidth - 6) + 'px';
    tt.style.top = (e.clientY + 14) + 'px';
  },
  async loadTop(ts) {
    const win = Math.max(60, (this.data ? this.data.bucket : 60) * 2);
    this.topTs = ts;
    try { this.top = await (await fetch(`/api/top?ts=${ts}&window=${win}&n=15`)).json(); } catch (e) { this.top = []; }
    this.renderTables();
  },
  renderTables() {
    if (!document.getElementById('body-h-top')) return;
    setHTML('sum-h-top', this.topTs ? sum(['gray', new Date(this.topTs * 1000).toLocaleString()]) : '');
    setHTML('body-h-top', renderTable('h-top', [{ k: 'name', label: 'Image' }, C.pid, { k: 'user', label: 'User' },
      { k: 'cpu', label: 'Average CPU', num: true, fmt: (v) => v.toFixed(1) }, { k: 'cpu_max', label: 'Peak CPU', num: true, fmt: (v) => v.toFixed(1) },
      { k: 'rss', label: 'Working Set (KB)', num: true, fmt: fKB }, { k: 'read_bps', label: 'Read (B/sec)', num: true, fmt: fInt },
      { k: 'write_bps', label: 'Write (B/sec)', num: true, fmt: fInt }], this.top || [],
    { rowKey: (r) => `${r.pid}${r.name}`, defSort: { k: 'cpu', asc: false }, empty: 'Click a point on any chart to see the busiest processes at that moment' }));
    const ev = this.events || [];
    const warns = ev.filter((x) => x.level === 'WARNING').length;
    setHTML('sum-h-events', sum(['orange', `${warns} Alerts`], ['gray', `${ev.length} Events`]));
    setHTML('body-h-events', renderTable('h-events', [{ k: 'ts', label: 'Time', fmt: (v) => new Date(v * 1000).toLocaleString() },
      { k: 'level', label: 'Level' }, { k: 'message', label: 'Message', title: (r) => r.message }], ev,
    { rowKey: (r) => `${r.ts}${r.kind}`, defSort: { k: 'ts', asc: false }, empty: 'No events in this range' }));
  },
};

window.addEventListener('mouseup', (e) => History.endDrag(e));

const HPAD = { l: 52, r: 10, t: 8, b: 20 };
function humanSpan(sec) { return sec < 90 ? `${Math.round(sec)} s` : sec < 5400 ? `${Math.round(sec / 60)} min` : `${(sec / 3600).toFixed(1)} h`; }
function unitFmt(u) {
  if (u === '%') return (v) => `${v.toFixed(1)}%`;
  if (u === 'B/s') return (v) => `${fBytes(v)}/s`;
  if (u === 'bps') return (v) => fBits(v);
  return (v) => (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));
}
function timeTicks(start, end, n) {
  const span = end - start;
  const steps = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const step = steps.find((s) => span / s <= n) || 604800;
  const tz = new Date().getTimezoneOffset() * 60;
  const first = Math.ceil((start - tz) / step) * step + tz;
  const out = []; for (let t = first; t <= end; t += step) out.push(t);
  return { ticks: out, step };
}
function drawHChart(c, H) {
  const cv = c.cv; const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth; const h = cv.clientHeight; if (!w) return;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h);
  const L = HPAD.l; const R = w - HPAD.r; const T = HPAD.t; const B = h - HPAD.b;
  const rows = H.data.series; const def = c.def;
  let max = def.max;
  if (!max) {
    let m = 0;
    for (const r of rows) for (const s of def.series) { const v = r[s.band] ?? r[s.k]; if (v > m) m = v; }
    max = niceMax(m * 1.08 || 1);
  }
  const X = (t) => L + ((t - H.start) / (H.end - H.start)) * (R - L);
  const Y = (v) => B - Math.min(1, Math.max(0, v / max)) * (B - T);
  const f = unitFmt(def.unit);
  // grid + labels
  ctx.font = '10px "Segoe UI", system-ui, sans-serif'; ctx.textBaseline = 'middle';
  ctx.strokeStyle = '#eceff3'; ctx.fillStyle = '#666'; ctx.lineWidth = 1; ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4; const y = Math.round(Y(v)) + 0.5;
    ctx.moveTo(L, y); ctx.lineTo(R, y);
    ctx.textAlign = 'right'; ctx.fillText(def.unit === '%' ? `${Math.round(v)}%` : f(v), L - 5, y);
  }
  const { ticks, step } = timeTicks(H.start, H.end, Math.max(2, Math.floor((R - L) / 90)));
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const t of ticks) {
    const x = Math.round(X(t)) + 0.5; ctx.moveTo(x, T); ctx.lineTo(x, B);
    const d = new Date(t * 1000);
    const lbl = step >= 86400 ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
      : (d.getHours() === 0 && d.getMinutes() === 0 ? d.toLocaleDateString([], { month: 'short', day: 'numeric' }) : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    ctx.fillText(lbl, x, B + 4);
  }
  ctx.stroke();
  ctx.strokeStyle = '#c5ccd6'; ctx.strokeRect(L + 0.5, T + 0.5, R - L - 1, B - T - 1);
  if (!rows.length) {
    ctx.fillStyle = '#888'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('No data recorded in this range', (L + R) / 2, (T + B) / 2); return;
  }
  const gap = Math.max(H.data.bucket * 3, 60);
  ctx.save(); ctx.beginPath(); ctx.rect(L, T, R - L, B - T); ctx.clip();
  for (const s of def.series) {
    if (!rows.some((r) => r[s.k] != null)) continue;
    // peak band (between average and bucket max)
    if (s.band) {
      ctx.fillStyle = s.color + '30';
      let seg = [];
      const flush = () => {
        if (seg.length > 1) {
          ctx.beginPath();
          seg.forEach((r, i) => (i ? ctx.lineTo(X(r.ts), Y(r[s.band])) : ctx.moveTo(X(r.ts), Y(r[s.band]))));
          for (let i = seg.length - 1; i >= 0; i--) ctx.lineTo(X(seg[i].ts), Y(seg[i][s.k]));
          ctx.closePath(); ctx.fill();
        }
        seg = [];
      };
      let prev = null;
      for (const r of rows) {
        if (r[s.k] == null || r[s.band] == null || (prev && r.ts - prev.ts > gap)) flush();
        if (r[s.k] != null && r[s.band] != null) seg.push(r);
        prev = r;
      }
      flush();
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.beginPath();
    let prev = null;
    for (const r of rows) {
      const v = r[s.k];
      if (v == null) { prev = null; continue; }
      if (prev && r.ts - prev.ts <= gap) ctx.lineTo(X(r.ts), Y(v)); else ctx.moveTo(X(r.ts), Y(v));
      prev = r;
    }
    ctx.stroke();
  }
  ctx.restore();
  // alert markers on the CPU chart
  if (def.id === 'cpu') {
    for (const e of H.events) {
      if (e.ts < H.start || e.ts > H.end) continue;
      const x = X(e.ts);
      ctx.fillStyle = e.level === 'WARNING' ? '#e8841a' : '#9aa7b8';
      ctx.beginPath(); ctx.moveTo(x - 4, T); ctx.lineTo(x + 4, T); ctx.lineTo(x, T + 6); ctx.closePath(); ctx.fill();
    }
  }
  // selected moment
  if (H.topTs && H.topTs >= H.start && H.topTs <= H.end) {
    ctx.strokeStyle = '#e8841a'; ctx.setLineDash([3, 3]); ctx.beginPath();
    ctx.moveTo(Math.round(X(H.topTs)) + 0.5, T); ctx.lineTo(Math.round(X(H.topTs)) + 0.5, B); ctx.stroke(); ctx.setLineDash([]);
  }
  // hover crosshair (synced across charts)
  if (H.hoverTs != null) {
    const r = H.nearest(H.hoverTs);
    if (r) {
      const x = Math.round(X(r.ts)) + 0.5;
      ctx.strokeStyle = '#888'; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, B); ctx.stroke();
      for (const s of def.series) if (r[s.k] != null) { ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, Y(r[s.k]), 3, 0, 7); ctx.fill(); }
    }
  }
  // drag selection
  if (H.drag && H.drag.chart === c) {
    ctx.fillStyle = 'rgba(44,111,209,.15)';
    const x0 = Math.min(H.drag.x0, H.drag.x1); ctx.fillRect(x0, T, Math.abs(H.drag.x1 - H.drag.x0), B - T);
  }
}

init();

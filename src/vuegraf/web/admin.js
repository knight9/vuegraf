'use strict';
const el = id => document.getElementById(id);
let lastActivity = Date.now(), timer, inFlight = false, catalogSignature = '', lastChecked = null;
const activeWindow = 5 * 60 * 1000;
const active = () => Date.now() - lastActivity < activeWindow;
function activity() { lastActivity = Date.now(); if (!timer && !inFlight) poll(); }
document.addEventListener('pointerdown', activity);
document.addEventListener('keydown', activity);
el('check').addEventListener('click', () => { lastActivity = Date.now(); clearTimeout(timer); timer = null; poll(); });
function format(value) { return value ? new Date(value).toLocaleString() : '—'; }
function duration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds === 0) return '0 seconds';
  const units = [[31536000,'year'],[86400,'day'],[3600,'hour'],[60,'minute']];
  const [size,name] = units.find(([size]) => seconds >= size) || [1,'second'];
  const count = Math.round(seconds / size);
  return `${count} ${name}${count === 1 ? '' : 's'}`;
}
function addRow(target, values) {
  const row = document.createElement('tr');
  values.forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
  target.append(row);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Vuegraf-Request':'1'}, body:JSON.stringify(body)});
  if (!response.ok) { const error = await response.json(); throw new Error(error.error || `HTTP ${response.status}`); }
  return response;
}
async function poll() {
  if (inFlight) return;
  clearTimeout(timer);
  timer = null;
  if (!active()) { el('polling').textContent = `Status polling paused after five minutes of inactivity. Last checked: ${format(lastChecked)}. Collection continues.`; return; }
  inFlight = true;
  try {
    const status = await (await api('/api/status')).json(); lastChecked = Date.now();
    el('polling').textContent = `Status polling active · every second · last checked ${format(lastChecked)}`;
    el('active').textContent = status.active ? `Running ${status.active.kind} (${status.active.source}) · ${status.active.phase || 'collection'} · ${status.active.progress.completed}/${status.active.progress.total ?? '?'} batches` : status.ready ? 'Collector idle' : `Collector unavailable: ${status.error || 'initializing'}`;
    el('legacy').textContent = `Legacy energy_usage recording: ${status.legacy_energy_enabled ? 'enabled' : 'disabled'}`;
    el('collect').disabled = !status.ready || Boolean(status.active);
    el('jobs').replaceChildren();
    Object.entries(status.jobs).forEach(([kind, job]) => {
      const row = document.createElement('tr');
      [kind, job.state, job.last_result || 'never run', format(job.last_completion), format(job.last_success), format(job.next_run)].forEach(value => {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }); el('jobs').append(row);
    });
    const signature = JSON.stringify(status.circuits);
    if (signature !== catalogSignature) {
      const selected = new Set(Array.from(el('circuits').selectedOptions, option => option.value));
      el('circuits').replaceChildren();
      status.circuits.forEach(circuit => { const option = document.createElement('option'); option.value = circuit.id; option.textContent = `${circuit.name} (${circuit.device}/${circuit.channel})`; option.selected = selected.has(circuit.id); el('circuits').append(option); });
      catalogSignature = signature;
    }
    const recovery = status.recovery || {};
    el('recovery-summary').textContent = `${recovery.streams || 0} tracked streams · ${recovery.deferred_intervals || 0} deferred intervals · ${recovery.permanently_unavailable_intervals || 0} unavailable intervals (${duration(recovery.permanently_unavailable_stream_seconds || 0)}) · ${duration(recovery.expired_stream_seconds || 0)} expired`;
    el('recovery').replaceChildren();
    Object.entries(recovery.remaining || {}).forEach(([resolution, gaps]) => addRow(el('recovery'), [resolution, gaps.streams_with_gaps || 0, duration(gaps.missing_stream_seconds || 0), format(gaps.oldest_gap)]));
    const disk = status.storage.filesystem;
    el('storage-summary').textContent = disk && Number.isFinite(disk.free) ? `Filesystem: ${(disk.free/2**30).toFixed(1)} GiB free of ${(disk.total/2**30).toFixed(1)} GiB. No verified hard quota.` : 'Filesystem usage unavailable; see mount configuration below.';
    el('storage-summary').textContent += Number.isFinite(status.storage.database_bytes) ? ` InfluxDB: ${(status.storage.database_bytes/2**20).toFixed(1)} MiB${status.storage.database_scan_complete ? '' : ' (partial scan)'}. Measured ${format(status.storage.checked_at)}.` : ' InfluxDB size unavailable.';
    el('storage-warning').textContent = status.storage.warning ? 'WARNING: less than 15% disk space remains.' : '';
    el('storage-alerts').textContent = `Alerts: ${(status.storage.alerts && status.storage.alerts.delivery) || 'not configured'}. Quota: ${(status.storage.quota && status.storage.quota.status) || 'unknown'}.`;
    el('retention').replaceChildren();
    (status.storage.buckets || []).forEach(bucket => {
      const rules = bucket.retention_seconds;
      const retention = bucket.unlimited ? 'Unlimited' : Array.isArray(rules) && rules.length ? rules.map(duration).join(', ') : 'Unknown';
      addRow(el('retention'), [bucket.name, (bucket.resolutions || []).join(', ') || 'legacy only', retention, bucket.contains_legacy ? 'Yes' : 'No']);
    });
  } catch (error) { el('polling').textContent = `Status check failed: ${error.message}. Last checked: ${format(lastChecked)}`; }
  finally { inFlight = false; timer = setTimeout(poll, 1000); }
}
el('collect').addEventListener('click', async () => {
  el('collect').disabled = true;
  try { const job = await (await api('/api/collect/second', {lookback_seconds:Number(el('lookback').value),circuits:Array.from(el('circuits').selectedOptions, option=>option.value)})).json(); el('message').textContent = `Admitted second-data job ${job.id}.`; }
  catch(error) { el('message').textContent = error.message; }
  finally { poll(); }
});
const now = new Date(); el('stop').value = now.toISOString(); el('start').value = new Date(now.getTime()-3600000).toISOString();
el('export').addEventListener('click', async () => {
  el('export').disabled = true;
  try {
    const query = Object.fromEntries(['start','stop','resolution','field','aggregate','window'].map(key=>[key,el(key).value]));
    const response = await api('/api/export', query); const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = 'vuegraf.csv'; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    el('message').textContent = 'CSV downloaded.';
  } catch(error) { el('message').textContent = error.message; }
  finally { el('export').disabled = false; }
});
poll();

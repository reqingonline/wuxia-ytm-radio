/**
 * 天刀·帮派 24h 云音坊控制台 — app.js
 * Native ES Module, zero build.
 *
 * XSS 安全：所有来自后端/API/OCR 的文本全部通过 textContent 或 DOM 节点构造，
 * 严禁使用未转义的 innerHTML 拼接外部文本。
 */

// ─── Utilities ────────────────────────────────────────────────────────────────

/**
 * 安全创建 DOM 元素，仅允许通过 textContent 设置文本内容。
 * @param {string} tag
 * @param {object} attrs
 * @param {string} [text]  textContent (XSS-safe)
 * @returns {HTMLElement}
 */
function el(tag, attrs = {}, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'className') e.className = v;
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else e.setAttribute(k, v);
  }
  if (text !== undefined) e.textContent = text; // XSS-safe
  return e;
}

/** 格式化秒数为 m:ss */
function fmtSec(s) {
  if (!Number.isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

/** 显示 Toast 通知（XSS-safe：只用 textContent） */
function toast(msg, type = 'ok', durationMs = 3500) {
  const c = document.getElementById('toast-container');
  const t = el('div', { className: `toast ${type}` });
  t.textContent = msg; // XSS-safe
  c.appendChild(t);
  setTimeout(() => t.remove(), durationMs);
}

/** 获取元素（简写） */
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

// ─── API helpers ──────────────────────────────────────────────────────────────

const BASE = '';

async function apiFetch(path, opts = {}) {
  const r = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(`HTTP ${r.status}: ${txt}`);
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('application/json') ? r.json() : r;
}

function apiGet(path) { return apiFetch(path); }
function apiPost(path, body) {
  return apiFetch(path, { method: 'POST', body: JSON.stringify(body) });
}

// ─── Tabs (tabs guide applied) ────────────────────────────────────────────────
// tabs guide: selection follows focus (default) since there is no fetch cost per tab.
// tabs guide: focusgroupstart moves to selected tab; nomemory used in HTML.

function initTabs() {
  const tablist = $('[focusgroup]');
  const tabs = $$('.tab', tablist);
  const panels = $$('.tab-panel');

  function selectTab(tab) {
    // tabs guide: set aria-selected + move focusgroupstart
    tabs.forEach(t => {
      const selected = t === tab;
      t.setAttribute('aria-selected', String(selected));
      t.toggleAttribute('focusgroupstart', selected);
    });
    panels.forEach(p => {
      const ctl = tab.getAttribute('aria-controls');
      if (p.id === ctl) {
        // Fix #5: Use hidden="until-found" for search compatibility (progressive enhancement)
        // tabs guide: "hidden='until-found'" keeps panels searchable by browser find-in-page
        p.removeAttribute('hidden');
        p.setAttribute('tabindex', '0');
      } else {
        // Use "until-found" on inactive panels so browser find-in-page can still locate content
        try {
          p.hidden = 'until-found'; // modern browsers
        } catch {
          p.setAttribute('hidden', ''); // fallback boolean hidden
        }
        p.removeAttribute('tabindex');
      }
    });
  }

  tabs.forEach(tab => {
    tab.addEventListener('click', () => selectTab(tab));
    // tabs guide: selection follows focus → also select on focus
    tab.addEventListener('focus', () => selectTab(tab));
  });

  // Scroll selected tab into view on selection change
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
  });

  // Fix #5: beforematch event – when find-in-page activates a hidden panel, switch tab
  panels.forEach(panel => {
    panel.addEventListener('beforematch', () => {
      const tab = tabs.find(t => t.getAttribute('aria-controls') === panel.id);
      if (tab) selectTab(tab);
    });
  });
}

// ─── Sliders (slider guide applied) ──────────────────────────────────────────
// slider guide: sync --control-value, --attr-min, --attr-max from JS for fill gradient

function syncSlider(slider, outputEl, fmtFn) {
  function sync() {
    slider.style.setProperty('--attr-min', slider.min || 0);
    slider.style.setProperty('--attr-max', slider.max || 100);
    slider.style.setProperty('--control-value', slider.value);
    if (outputEl) {
      outputEl.textContent = fmtFn ? fmtFn(Number(slider.value)) : slider.value;
    }
  }
  sync();
  slider.addEventListener('input', sync);
}

function initSliders() {
  // Seek slider
  const seekSlider = $('#seek-slider');
  syncSlider(seekSlider, null, null);

  // Audio form sliders
  syncSlider($('#sl-broadcast-vol'), $('#out-broadcast-vol'), v => `${Math.round(v * 100)}%`);
  syncSlider($('#sl-monitor-vol'), $('#out-monitor-vol'), v => `${Math.round(v * 100)}%`);
  syncSlider($('#sl-mic-vol'), $('#out-mic-vol'), v => `${v.toFixed(2)}×`);
  syncSlider($('#sl-duck-thresh'), $('#out-duck-thresh'), v => `${v} dBFS`);
  syncSlider($('#sl-duck-depth'), $('#out-duck-depth'), v => `${v} dB`);

  // OCR sliders
  syncSlider($('#sl-interval'), $('#out-interval'), v => `${v} s`);
  syncSlider($('#sl-cooldown'), $('#out-cooldown'), v => `${v} s`);
}

// ─── WebSocket & Real-time State ──────────────────────────────────────────────

let wsReconnectTimer = null;
let wsInstance = null;
let currentScheduleState = null;

function initWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws`;
  if (wsInstance) { try { wsInstance.close(); } catch (_) {} }
  wsInstance = new WebSocket(url);

  wsInstance.addEventListener('open', () => {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  });

  wsInstance.addEventListener('message', ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'meter') handleMeterUpdate(msg.data);
    else if (msg.type === 'state') handleStateUpdate(msg.data);
    else if (msg.type === 'event') handleWsEvent(msg);
  });

  wsInstance.addEventListener('close', () => {
    wsReconnectTimer = setTimeout(initWS, 3000);
  });
  wsInstance.addEventListener('error', () => {
    wsInstance.close();
  });
}

// ─── Meter update (real-time, from WS 'meter' frames) ─────────────────────────

function handleMeterUpdate(data) {
  if (!data) return;

  // broadcast (virtual mic)
  const bVal = typeof data.peak_db === 'number' ? data.peak_db : -60;
  updateMeterBar('meter-broadcast', 'meter-broadcast-val', bVal);

  // mic
  const mVal = typeof data.mic_rms_db === 'number' ? data.mic_rms_db : -60;
  updateMeterBar('meter-mic', 'meter-mic-val', mVal);

  // ducking indicator
  const ind = $('#ducking-indicator');
  if (data.ducking_active) ind.classList.add('active');
  else ind.classList.remove('active');

  // player position
  if (typeof data.position_sec === 'number') {
    updateSeekPosition(data.position_sec, data.duration_sec);
  }

  // play/pause icon
  if (data.state) updatePlayPauseIcon(data.state);
}

function updateMeterBar(barId, valId, dbVal) {
  const bar = $(`#${barId}`);
  const valEl = $(`#${valId}`);
  if (!bar) return;
  // Map -60 dBFS..0 dBFS to 0..100%
  const pct = Math.max(0, Math.min(100, (dbVal + 60) / 60 * 100));
  bar.style.width = `${pct}%`;
  bar.setAttribute('aria-valuenow', dbVal.toFixed(1));
  if (valEl) valEl.textContent = dbVal > -60 ? dbVal.toFixed(1) : '—';
}

function updateSeekPosition(pos, dur) {
  const sl = $('#seek-slider');
  const posEl = $('#pos-time');
  const durEl = $('#dur-time');
  if (!sl._dragging) {
    const max = Number.isFinite(dur) && dur > 0 ? dur : 100;
    sl.max = max;
    sl.value = pos;
    sl.style.setProperty('--attr-max', max);
    sl.style.setProperty('--control-value', pos);
  }
  if (posEl) posEl.textContent = fmtSec(pos);
  if (durEl) durEl.textContent = fmtSec(dur);
}

function updatePlayPauseIcon(state) {
  const icon = $('#playpause-icon');
  if (!icon) return;
  // Fix #3: track current player state globally for play/pause/stopped logic
  window._playerState = state;
  if (state === 'playing') {
    // Pause icon (two bars)
    icon.setAttribute('d', 'M6 19h4V5H6v14zm8-14v14h4V5h-4z');
    $('#btn-playpause').setAttribute('aria-label', '暂停');
  } else {
    // Play icon (triangle) – covers both 'paused' and 'stopped'
    icon.setAttribute('d', 'M8 5v14l11-7z');
    $('#btn-playpause').setAttribute('aria-label', state === 'paused' ? '继续播放' : '播放');
  }
}

// ─── State update (from WS 'state' frames or REST poll) ───────────────────────

function handleStateUpdate(data) {
  if (!data) return;

  // Scheduled broadcast state (must run before updatePlayerUI so standby copy is ready)
  if (data.schedule) updateScheduleUI(data.schedule);

  // Player / track info
  if (data.player) updatePlayerUI(data.player);

  // Auto-DJ button
  if (typeof data.auto_dj_enabled === 'boolean') {
    const btn = $('#btn-autodj');
    btn.setAttribute('aria-pressed', String(data.auto_dj_enabled));
  }

  // VB-CABLE banner
  if (data.player && typeof data.player.vbcable_installed === 'boolean') {
    const installed = data.player.vbcable_installed;
    const bangBanner = $('#vbcable-banner');
    const okBanner = $('#vbcable-ok-banner');
    if (bangBanner) bangBanner.hidden = installed;
    if (okBanner) okBanner.hidden = !installed;
  }

  // Queue
  if (Array.isArray(data.queue)) renderQueue(data.queue);

  // History
  if (Array.isArray(data.history)) renderHistory(data.history);

  // Adaptive OCR schedule status badge (XSS-safe textContent)
  if (data.ocr) updateOcrScheduleBadge(data.ocr);

  // Fix #4: Back-fill form controls from persisted config
  backfillForms(data);
}

function updateOcrScheduleBadge(ocr) {
  const badge = $('#ocr-schedule-badge');
  if (!badge) return;
  const sched = ocr.peak_schedule;
  const baseIv = ocr.interval_sec ?? 120;
  const effIv = ocr.effective_interval_sec ?? baseIv;
  if (ocr.standby_paused) {
    badge.textContent = `非播音时段已自动休眠（0 占用），到点自动唤醒（${sched?.active_interval_sec ?? 8} s 高频扫描）`;
    return;
  }
  if (sched && sched.enabled) {
    const dayMap = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const daysText = Array.isArray(sched.days) && sched.days.length
      ? sched.days.map(d => dayMap[d] || '').filter(Boolean).join('、')
      : '周四、周日';
    const modeLabel = ocr.peak_mode_active ? `活动高峰档 (${effIv} s)` : `平时省资源档 (${baseIv} s)`;
    badge.textContent = `定时活跃提速：每${daysText} ${sched.start || '19:30'}–${sched.end || '20:20'} 切换为 ${sched.active_interval_sec ?? 8} s（当前生效：${modeLabel}）`;
  } else {
    badge.textContent = `当前扫描间隔：固定 ${baseIv} s`;
  }
}

function fmtCountdown(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const mins = Math.ceil((s % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${mins} 分钟`;
  return `${Math.max(1, mins)} 分钟`;
}

function updateScheduleUI(sched) {
  if (!sched) return;
  currentScheduleState = sched;
  const banner = $('#schedule-banner');
  const statusEl = $('#schedule-status-text');
  const btn = $('#btn-schedule-override');

  if (banner) {
    banner.classList.remove('schedule-banner--standby', 'schedule-banner--live', 'schedule-banner--override');
    if (sched.mode === 'live_scheduled' || sched.mode === 'always_on') {
      banner.classList.add('schedule-banner--live');
    } else if (sched.mode === 'live_override') {
      banner.classList.add('schedule-banner--override');
    } else {
      banner.classList.add('schedule-banner--standby');
    }
  }

  if (statusEl) {
    if (sched.mode === 'always_on') {
      statusEl.textContent = '📻 定时限制已关闭（全天候随时可播）';
    } else if (sched.mode === 'live_scheduled') {
      const rem = fmtCountdown(sched.seconds_until_end);
      statusEl.textContent = `🎙 定时播音时段直播中（${sched.days_label} ${sched.start}–${sched.end} · 约 ${rem} 后自动静音待机）`;
    } else if (sched.mode === 'live_override') {
      statusEl.textContent = `▶ 临时试播中（定时时段：${sched.days_label} ${sched.start}–${sched.end} · 可随时结束恢复待机）`;
    } else {
      const countdown = sched.seconds_until_next_start > 0 ? `（约 ${fmtCountdown(sched.seconds_until_next_start)} 后）` : '';
      statusEl.textContent = `⏸ 非播音时段·已静音待机（0 播放 / 0 OCR） | 下次开播：${sched.next_start_label}${countdown}`;
    }
  }

  if (btn) {
    if (sched.mode === 'standby') {
      btn.hidden = false;
      btn.textContent = '▶ 临时试播';
      btn.dataset.targetActive = 'true';
    } else if (sched.mode === 'live_override') {
      btn.hidden = false;
      btn.textContent = '⏹ 结束试播恢复待机';
      btn.dataset.targetActive = 'false';
    } else {
      btn.hidden = true;
    }
  }
}

function updatePlayerUI(player) {
  const track = player.track;

  // Track title / artist (XSS-safe: textContent)
  const titleEl = $('#track-title');
  const artistEl = $('#track-artist');
  const sourceEl = $('#track-source');
  const coverEl = $('#cover-img');

  if (track) {
    if (titleEl) titleEl.textContent = track.title || '（未知歌曲）';
    if (artistEl) artistEl.textContent = track.artist || '—';

    // Source badge
    let srcLabel = '';
    if (track.source === 'ocr') srcLabel = '[帮派OCR]';
    else if (track.source === 'web') srcLabel = '[网页点歌]';
    else srcLabel = '[帮派电台]';
    if (sourceEl) sourceEl.textContent = srcLabel; // XSS-safe

    // Cover
    if (coverEl && track.thumbnail_url) {
      coverEl.src = track.thumbnail_url;
      coverEl.alt = track.title ? `${track.title} 封面` : '封面'; // XSS-safe (alt attr)
    }
  } else if (currentScheduleState && currentScheduleState.mode === 'standby' && player.state === 'stopped') {
    if (titleEl) titleEl.textContent = '（非时段静音待机）';
    if (artistEl) artistEl.textContent = `下次开播：${currentScheduleState.next_start_label || '周四/周日 19:30'}`;
    if (sourceEl) sourceEl.textContent = '[定时待机]';
  } else {
    if (titleEl) titleEl.textContent = '（待播）';
    if (artistEl) artistEl.textContent = '—';
    if (sourceEl) sourceEl.textContent = '';
  }

  if (typeof player.position_sec === 'number') {
    updateSeekPosition(player.position_sec, player.duration_sec);
  }
  if (player.state) updatePlayPauseIcon(player.state);
}

function handleWsEvent(msg) {
  const level = msg.level === 'error' ? 'err' : msg.level === 'warn' ? 'warn' : 'ok';
  toast(msg.message || '', level);
}

// Fix #4 (refined): Only back-fill forms once on first load, not on every poll
// to avoid overwriting values the user is currently editing.
let _formsInitialized = false;

// Fix #4: Back-fill form controls from API /api/state response
function backfillForms(data) {
  if (_formsInitialized) return; // Only run once; avoid overwriting in-progress edits
  _formsInitialized = true;
  // Audio routing config (from data.player.routing or data.audio_config)
  const routing = (data.player && data.player.routing) || data.audio_config;
  if (routing) {
    _setInputVal('#sl-broadcast-vol', routing.broadcast_volume, v => `${Math.round(v * 100)}%`, '#out-broadcast-vol');
    _setInputVal('#sl-monitor-vol', routing.monitor_volume, v => `${Math.round(v * 100)}%`, '#out-monitor-vol');
    _setInputVal('#sl-mic-vol', routing.mic_volume, v => `${Number(v).toFixed(2)}×`, '#out-mic-vol');
    _setInputVal('#sl-duck-thresh', routing.duck_threshold_db, v => `${v} dBFS`, '#out-duck-thresh');
    _setInputVal('#sl-duck-depth', routing.ducking_db, v => `${v} dB`, '#out-duck-depth');
    _setCheckbox('#sw-monitor', routing.monitor_enabled);
    _setCheckbox('#sw-mic', routing.mic_bridge_enabled);
    _setCheckbox('#sw-ducking', routing.ducking_enabled);
    // Device selects (IDs may not be in select yet; set when devices are loaded)
    if (routing.virtual_out_device_id !== undefined && routing.virtual_out_device_id !== null) {
      setSelectVal('#sel-virtual-out', String(routing.virtual_out_device_id));
    }
    if (routing.monitor_out_device_id !== undefined && routing.monitor_out_device_id !== null) {
      setSelectVal('#sel-monitor-out', String(routing.monitor_out_device_id));
    }
    if (routing.mic_in_device_id !== undefined && routing.mic_in_device_id !== null) {
      setSelectVal('#sel-mic-in', String(routing.mic_in_device_id));
    }
  }

  // Broadcast schedule config
  const sched = data.schedule || data.config?.broadcast_schedule;
  if (sched) {
    _setCheckbox('#sw-schedule-enabled', sched.enabled);
    _setCheckbox('#sw-sched-clear-end', sched.clear_queue_on_end);
    const startInp = $('#inp-sched-start');
    const endInp = $('#inp-sched-end');
    if (startInp && sched.start) startInp.value = sched.start;
    if (endInp && sched.end) endInp.value = sched.end;
    if (Array.isArray(sched.days)) {
      const daySet = new Set(sched.days.map(Number));
      $$('input[name="sched_day"]').forEach(cb => {
        cb.checked = daySet.has(Number(cb.value));
      });
    }
  }

  // OCR config
  const ocr = data.ocr;
  if (ocr) {
    _setCheckbox('#sw-ocr-enabled', ocr.enabled);
    _setInputVal('#sl-interval', ocr.interval_sec, v => `${v} s`, '#out-interval');
    _setInputVal('#sl-cooldown', ocr.cooldown_sec, v => `${v} s`, '#out-cooldown');
    _setCheckbox('#sw-allow-skip', ocr.allow_skip_cmd);
    if (ocr.trigger_mode) setSelectVal('#sel-trigger-mode', ocr.trigger_mode);
    if (ocr.roi) {
      canvasState.roi = ocr.roi;
      updateRoiInfo();
    }
  }

  // YTMusic / fallback config
  const ytm = data.ytmusic || data.fallback;
  if (ytm) {
    if (ytm.proxy) { const inp = $('#inp-proxy'); if (inp) inp.value = ytm.proxy; }

    // Fix: only fill #inp-fallback-src when the source is a search: prefix or URL,
    // NOT when it looks like a playlist ID that belongs in #sel-playlists.
    // This prevents the custom input from shadowing the playlist dropdown selection.
    if (ytm.fallback_source) {
      const src = ytm.fallback_source;
      const isSearchOrUrl = src.startsWith('search:') || src.startsWith('http') || src.startsWith('PL') === false;
      const inp = $('#inp-fallback-src');
      // Try to match to a playlist select option first
      const sel = $('#sel-playlists');
      let matchedInSelect = false;
      if (sel) {
        for (const opt of sel.options) {
          if (opt.value === src) {
            sel.value = src;
            matchedInSelect = true;
            break;
          }
        }
      }
      // Only put into custom input if not in the select, or if it's a search:/URL type
      if (!matchedInSelect && inp) {
        inp.value = src;
      }
      // Show current fallback source as status hint
      const statusEl = $('#fallback-status');
      if (statusEl && !statusEl.textContent) {
        statusEl.textContent = `当前兜底源: ${src}`;
        statusEl.className = 'status-text';
      }
    }

    if (typeof ytm.shuffle === 'boolean') _setCheckbox('#sw-shuffle', ytm.shuffle);
    else if (typeof ytm.fallback_shuffle === 'boolean') _setCheckbox('#sw-shuffle', ytm.fallback_shuffle);

    // Auth status display
    const authSt = $('#ytm-auth-status');
    if (authSt) {
      if (ytm.authenticated) {
        authSt.textContent = `✅ 已认证 (${ytm.mode || 'authenticated'})`;
        authSt.className = 'status-text ok';
      } else {
        authSt.textContent = '匿名模式（未绑定 Cookie）';
        authSt.className = 'status-text';
      }
    }
  }
}

/** Helper: set input value + sync slider visual + update output label */
function _setInputVal(inputSel, val, fmtFn, outputSel) {
  if (val === undefined || val === null) return;
  const inp = $(inputSel);
  if (!inp) return;
  inp.value = val;
  inp.style.setProperty('--attr-min', inp.min || 0);
  inp.style.setProperty('--attr-max', inp.max || 100);
  inp.style.setProperty('--control-value', val);
  if (outputSel) {
    const out = $(outputSel);
    if (out) out.textContent = fmtFn ? fmtFn(Number(val)) : val;
  }
}

/** Helper: set checkbox checked state */
function _setCheckbox(sel, val) {
  if (val === undefined || val === null) return;
  const cb = $(sel);
  if (cb) cb.checked = Boolean(val);
}

// ─── Queue rendering (XSS-safe DOM construction) ──────────────────────────────

function renderQueue(queue) {
  const list = $('#queue-list');
  if (!list) return;
  list.textContent = ''; // clear safely

  if (queue.length === 0) {
    const empty = el('li', { className: 'queue-item' }, '队列为空，快来点歌吧～');
    empty.style.color = 'var(--text-muted)';
    list.appendChild(empty);
    return;
  }

  queue.forEach((item, idx) => {
    const li = el('li', { className: 'queue-item' });

    const idxEl = el('span', { className: 'queue-item-idx' }, String(idx + 1));

    const info = el('div', { className: 'queue-item-info' });
    const titleEl = el('div', { className: 'queue-item-title' },
      item.track?.title || '（未知）');
    const artistEl = el('div', { className: 'queue-item-artist' },
      item.track?.artist || '—');
    const reqEl = el('div', { className: 'queue-item-requester' },
      `♟ ${item.requester || '少侠'} · ${item.source === 'ocr' ? '[帮派OCR]' : '[网页点歌]'}`);
    info.appendChild(titleEl);
    info.appendChild(artistEl);
    info.appendChild(reqEl);

    const actions = el('div', { className: 'queue-item-actions' });
    if (idx > 0) {
      const pinBtn = el('button', { type: 'button', className: 'btn btn-sm', title: '置顶' }, '⬆');
      pinBtn.addEventListener('click', () => pinQueueItem(item.item_id));
      actions.appendChild(pinBtn);
    }
    const removeBtn = el('button', { type: 'button', className: 'btn btn-sm btn-danger-soft', title: '移除' }, '✕');
    removeBtn.addEventListener('click', () => removeQueueItem(item.item_id));
    actions.appendChild(removeBtn);

    li.appendChild(idxEl);
    li.appendChild(info);
    li.appendChild(actions);
    list.appendChild(li);
  });
}

function renderHistory(history) {
  const list = $('#history-list');
  if (!list) return;
  list.textContent = '';
  history.slice(0, 20).forEach(item => {
    const li = el('li', { className: 'history-item' });
    const titleEl = el('span', { className: 'history-item-title' },
      item.track?.title || '（未知）');
    const sep = el('span', {}, ' — ');
    const artistEl = el('span', {}, item.track?.artist || '—');
    li.appendChild(titleEl);
    li.appendChild(sep);
    li.appendChild(artistEl);
    list.appendChild(li);
  });
}

// ─── Queue operations ─────────────────────────────────────────────────────────

async function addSong(pinTop = false) {
  const q = $('#song-input').value.trim();
  const req = $('#requester-input').value.trim() || '少侠';
  if (!q) { toast('请输入歌名或关键字', 'warn'); return; }

  try {
    await apiPost('/api/queue/add', { query: q, requester: req, pin_top: pinTop });
    $('#song-input').value = '';
    toast(`「${q}」已加入队列`, 'ok');
  } catch (err) {
    toast(`点歌失败: ${err.message}`, 'err');
  }
}

async function pinQueueItem(itemId) {
  try {
    await apiPost('/api/queue/pin', { item_id: itemId });
  } catch (err) {
    toast(`置顶失败: ${err.message}`, 'err');
  }
}

async function removeQueueItem(itemId) {
  try {
    await apiPost('/api/queue/remove', { item_id: itemId });
  } catch (err) {
    toast(`移除失败: ${err.message}`, 'err');
  }
}

async function clearQueue() {
  try {
    await apiPost('/api/queue/clear', {});
    toast('队列已清空', 'ok');
  } catch (err) {
    toast(`清空失败: ${err.message}`, 'err');
  }
}

// ─── YTMusic search ───────────────────────────────────────────────────────────

let ytmSearchDebounce = null;

function initYtmSearch() {
  const inp = $('#ytm-search-input');
  const btn = $('#btn-ytm-search');
  const resultsEl = $('#ytm-results');

  async function doSearch() {
    const q = inp.value.trim();
    if (!q) return;
    resultsEl.textContent = '';
    const loading = el('li', {}, '搜索中…');
    loading.style.color = 'var(--text-muted)';
    resultsEl.appendChild(loading);
    try {
      const results = await apiGet(`/api/ytmusic/search?q=${encodeURIComponent(q)}&limit=8`);
      resultsEl.textContent = '';
      if (!results.length) {
        resultsEl.appendChild(el('li', {}, '未找到结果'));
        return;
      }
      results.forEach(r => {
        const card = renderYtmCard(r);
        resultsEl.appendChild(card);
      });
    } catch (err) {
      resultsEl.textContent = '';
      resultsEl.appendChild(el('li', {}, `搜索失败: ${err.message}`));
    }
  }

  btn.addEventListener('click', doSearch);
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
  inp.addEventListener('input', () => {
    clearTimeout(ytmSearchDebounce);
    ytmSearchDebounce = setTimeout(doSearch, 600);
  });
}

function renderYtmCard(r) {
  const li = el('li', { className: 'ytm-result-card' });

  const titleEl = el('div', { className: 'ytm-result-title' }, r.title || '（未知）');
  const artistEl = el('div', { className: 'ytm-result-artist' }, r.artist || '—');

  const metaEl = el('div', { className: 'ytm-result-meta' });
  const dur = r.duration_sec ? fmtSec(r.duration_sec) : '—';
  metaEl.textContent = `时长 ${dur}`;

  const actions = el('div', { className: 'ytm-result-actions' });

  const addBtn = el('button', { type: 'button', className: 'btn btn-sm btn-primary' }, '+ 加入队列');
  addBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await apiPost('/api/queue/add', {
        video_id: r.video_id,
        title: r.title,
        artist: r.artist,
        requester: '网页点歌',
        pin_top: false,
      });
      toast(`「${r.title}」已加入队列`, 'ok');
    } catch (err) {
      toast(`入队失败: ${err.message}`, 'err');
    }
  });

  const topBtn = el('button', { type: 'button', className: 'btn btn-sm btn-secondary' }, '⬆ 置顶');
  topBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await apiPost('/api/queue/add', {
        video_id: r.video_id,
        title: r.title,
        artist: r.artist,
        requester: '网页点歌',
        pin_top: true,
      });
      toast(`「${r.title}」已置顶插播`, 'ok');
    } catch (err) {
      toast(`置顶失败: ${err.message}`, 'err');
    }
  });

  actions.appendChild(addBtn);
  actions.appendChild(topBtn);

  li.appendChild(titleEl);
  li.appendChild(artistEl);
  li.appendChild(metaEl);
  li.appendChild(actions);

  return li;
}

// ─── Player controls ──────────────────────────────────────────────────────────

function initPlayerControls() {
  // Fix #3: stopped state must send 'play' (not 'resume') to trigger auto-dj start
  $('#btn-playpause').addEventListener('click', async () => {
    // Determine current state from play/pause icon path
    const icon = $('#playpause-icon');
    const isPauseIcon = icon.getAttribute('d').startsWith('M6 19'); // pause icon starts with M6 19
    // If we show the pause icon, the player is currently playing → pause it
    // If we show the play icon, player may be paused OR stopped
    let action;
    if (isPauseIcon) {
      action = 'pause';
    } else {
      // Check actual state: 'paused' → 'resume'; 'stopped' or unknown → 'play'
      action = (window._playerState === 'paused') ? 'resume' : 'play';
    }
    try {
      await apiPost('/api/player/control', { action });
    } catch (err) {
      toast(`操作失败: ${err.message}`, 'err');
    }
  });

  $('#btn-skip').addEventListener('click', async () => {
    try {
      await apiPost('/api/player/control', { action: 'skip' });
      toast('已切歌', 'ok');
    } catch (err) {
      toast(`切歌失败: ${err.message}`, 'err');
    }
  });

  $('#btn-autodj').addEventListener('click', async () => {
    const btn = $('#btn-autodj');
    const enabled = btn.getAttribute('aria-pressed') !== 'true';
    try {
      await apiPost('/api/player/control', { action: 'toggle_autodj', enabled });
      btn.setAttribute('aria-pressed', String(enabled));
      toast(`Auto-DJ 已${enabled ? '开启' : '关闭'}`, 'ok');
    } catch (err) {
      toast(`切换失败: ${err.message}`, 'err');
    }
  });

  // dialog guide: showModal() for confirmation dialog
  const dialog = $('#testtone-confirm');
  $('#btn-testtone').addEventListener('click', () => {
    dialog.showModal();
  });
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue === 'confirm') {
      try {
        await apiPost('/api/player/control', { action: 'test_tone' });
        toast('正在发送 440 Hz 测试音 (3s)…', 'warn');
      } catch (err) {
        toast(`发送失败: ${err.message}`, 'err');
      }
    }
    // dialog guide: reset returnValue after each close
    dialog.returnValue = '';
  });

  // Seek drag
  const seekSlider = $('#seek-slider');
  seekSlider.addEventListener('mousedown', () => { seekSlider._dragging = true; });
  seekSlider.addEventListener('touchstart', () => { seekSlider._dragging = true; }, { passive: true });
  seekSlider.addEventListener('change', async () => {
    seekSlider._dragging = false;
    const sec = Number(seekSlider.value);
    try {
      await apiPost('/api/player/control', { action: 'play', seek_sec: sec });
    } catch (err) {
      // non-critical, ignore
    }
  });
}

// ─── Audio config ─────────────────────────────────────────────────────────────

async function loadAudioDevices() {
  try {
    const data = await apiGet('/api/audio/devices');
    populateDeviceSelect('#sel-virtual-out', data.outputs || [], true);
    populateDeviceSelect('#sel-monitor-out', data.outputs || [], false);
    populateDeviceSelect('#sel-mic-in', data.inputs || [], false);

    // VB-CABLE banner
    const installed = data.vbcable_installed;
    const bangBanner = $('#vbcable-banner');
    const okBanner = $('#vbcable-ok-banner');
    if (bangBanner) bangBanner.hidden = installed;
    if (okBanner) okBanner.hidden = !installed;

    // Pre-select recommended devices
    if (data.recommended_virtual_out_id !== null) {
      setSelectVal('#sel-virtual-out', String(data.recommended_virtual_out_id));
    }
    if (data.recommended_monitor_out_id !== null) {
      setSelectVal('#sel-monitor-out', String(data.recommended_monitor_out_id));
    }
    if (data.recommended_mic_in_id !== null) {
      setSelectVal('#sel-mic-in', String(data.recommended_mic_in_id));
    }
  } catch (err) {
    toast(`加载声卡列表失败: ${err.message}`, 'err');
  }
}

function populateDeviceSelect(selector, devices, outputMode) {
  const sel = $(selector);
  if (!sel) return;
  sel.textContent = '';
  devices.forEach(d => {
    const opt = el('option', { value: String(d.id) });
    // XSS-safe: textContent for device name
    let label = d.name;
    if (d.is_vbcable) label += ' ✓CABLE';
    if (d.is_default) label += ' (默认)';
    opt.textContent = label;
    sel.appendChild(opt);
  });
}

function setSelectVal(selector, val) {
  const sel = $(selector);
  if (!sel) return;
  for (const opt of sel.options) {
    if (opt.value === val) { sel.value = val; break; }
  }
}

function initAudioForm() {
  const form = $('#audio-config-form');

  $('#btn-refresh-devices').addEventListener('click', () => loadAudioDevices());

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    // Fix #2: Use empty-string check not falsy check; device ID 0 is valid
    const parseDeviceId = key => {
      const v = fd.get(key);
      return (v === null || v === '') ? null : Number(v);
    };
    const cfg = {
      virtual_out_device_id: parseDeviceId('virtual_out_device_id'),
      broadcast_volume: Number(fd.get('broadcast_volume')),
      monitor_out_device_id: parseDeviceId('monitor_out_device_id'),
      monitor_enabled: $('#sw-monitor').checked,
      monitor_volume: Number(fd.get('monitor_volume')),
      mic_bridge_enabled: $('#sw-mic').checked,
      mic_in_device_id: parseDeviceId('mic_in_device_id'),
      mic_volume: Number(fd.get('mic_volume')),
      ducking_enabled: $('#sw-ducking').checked,
      duck_threshold_db: Number(fd.get('duck_threshold_db')),
      ducking_db: Number(fd.get('ducking_db')),
    };
    try {
      await apiPost('/api/audio/config', cfg);
      toast('路由配置已保存', 'ok');
    } catch (err) {
      toast(`保存失败: ${err.message}`, 'err');
    }
  });
}

// ─── OCR Panel ────────────────────────────────────────────────────────────────

// Canvas ROI drawing
let canvasState = {
  origW: 0, origH: 0, scaledW: 0, scaledH: 0,
  monLeft: 0, monTop: 0,
  roi: null,
  drawing: false, startX: 0, startY: 0, curX: 0, curY: 0,
  img: null,
};

function initOcrPanel() {
  $('#btn-scan-windows').addEventListener('click', scanGameWindows);
  $('#btn-use-window').addEventListener('click', useWindowCoords);
  $('#btn-refresh-screenshot').addEventListener('click', refreshScreenshot);
  $('#btn-roi-preview').addEventListener('click', loadRoiPreview);
  $('#btn-ocr-test').addEventListener('click', runOcrTest);

  const canvas = $('#screenshot-canvas');
  canvas.addEventListener('mousedown', canvasMouseDown);
  canvas.addEventListener('mousemove', canvasMouseMove);
  canvas.addEventListener('mouseup', canvasMouseUp);

  const ocrForm = $('#ocr-config-form');
  ocrForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(ocrForm);
    const cfg = {
      enabled: $('#sw-ocr-enabled').checked,
      interval_sec: Number(fd.get('interval_sec')),
      trigger_mode: fd.get('trigger_mode'),
      cooldown_sec: Number(fd.get('cooldown_sec')),
      allow_skip_cmd: $('#sw-allow-skip').checked,
      roi: canvasState.roi || null,
    };
    try {
      await apiPost('/api/ocr/config', cfg);
      toast('OCR 配置已保存', 'ok');
    } catch (err) {
      toast(`保存失败: ${err.message}`, 'err');
    }
  });
}

async function scanGameWindows() {
  try {
    const wins = await apiGet('/api/ocr/windows');
    const sel = $('#sel-game-window');
    sel.textContent = '';
    if (!wins.length) {
      sel.appendChild(el('option', { value: '' }, '未检测到游戏窗口'));
      return;
    }
    wins.forEach(w => {
      const opt = el('option', { value: String(w.hwnd) });
      // XSS-safe: textContent for window title
      opt.textContent = `${w.is_game_candidate ? '⭐ ' : ''}${w.title} (${w.width}×${w.height})`;
      opt.dataset.left = w.left;
      opt.dataset.top = w.top;
      opt.dataset.width = w.width;
      opt.dataset.height = w.height;
      sel.appendChild(opt);
    });
    toast(`检测到 ${wins.length} 个窗口`, 'ok');
  } catch (err) {
    toast(`扫描失败: ${err.message}`, 'err');
  }
}

function useWindowCoords() {
  const sel = $('#sel-game-window');
  const opt = sel.selectedOptions[0];
  if (!opt || !opt.value) return;
  // Use bottom-left quarter as default chat area
  const w = Number(opt.dataset.width);
  const h = Number(opt.dataset.height);
  const left = Number(opt.dataset.left);
  const top = Number(opt.dataset.top);
  canvasState.roi = {
    left: left,
    top: top + Math.round(h * 0.65),
    width: Math.round(w * 0.5),
    height: Math.round(h * 0.35),
  };
  updateRoiInfo();
  toast('已使用窗口左下角建议聊天区域', 'ok');
}

async function refreshScreenshot() {
  const placeholder = $('#canvas-placeholder');
  if (placeholder) placeholder.textContent = '加载中…';
  try {
    const r = await fetch('/api/ocr/screenshot?monitor=1');
    if (!r.ok) throw new Error(r.statusText);

    canvasState.origW = Number(r.headers.get('X-Orig-Width') || 1920);
    canvasState.origH = Number(r.headers.get('X-Orig-Height') || 1080);
    canvasState.monLeft = Number(r.headers.get('X-Mon-Left') || 0);
    canvasState.monTop = Number(r.headers.get('X-Mon-Top') || 0);

    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      const canvas = $('#screenshot-canvas');
      canvasState.scaledW = img.naturalWidth;
      canvasState.scaledH = img.naturalHeight;
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvasState.img = img;
      drawCanvas();
      URL.revokeObjectURL(url);
      if (placeholder) placeholder.style.display = 'none';
    };
    img.src = url;
  } catch (err) {
    toast(`截图失败: ${err.message}`, 'err');
    if (placeholder) placeholder.textContent = '截图加载失败';
  }
}

function drawCanvas() {
  const canvas = $('#screenshot-canvas');
  const ctx = canvas.getContext('2d');
  if (canvasState.img) {
    ctx.drawImage(canvasState.img, 0, 0);
  } else {
    ctx.fillStyle = '#0d1117';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  // Draw ROI box
  if (canvasState.roi) {
    const { left, top, width, height } = canvasRoiToCanvas(canvasState.roi);
    ctx.strokeStyle = '#00ff66';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(left, top, width, height);
    ctx.setLineDash([]);
  }

  // Draw in-progress selection
  if (canvasState.drawing) {
    const x = Math.min(canvasState.startX, canvasState.curX);
    const y = Math.min(canvasState.startY, canvasState.curY);
    const w = Math.abs(canvasState.curX - canvasState.startX);
    const h = Math.abs(canvasState.curY - canvasState.startY);
    ctx.strokeStyle = '#00ff66';
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 2]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(0,255,102,0.08)';
    ctx.fillRect(x, y, w, h);
  }
}

function canvasToScreen(cx, cy) {
  const canvas = $('#screenshot-canvas');
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvasState.scaledW / rect.width;
  const scaleY = canvasState.scaledH / rect.height;
  const imgX = cx * scaleX;
  const imgY = cy * scaleY;
  // convert from scaled to original
  const oScaleX = canvasState.origW / canvasState.scaledW;
  const oScaleY = canvasState.origH / canvasState.scaledH;
  return {
    x: Math.round(imgX * oScaleX) + canvasState.monLeft,
    y: Math.round(imgY * oScaleY) + canvasState.monTop,
  };
}

function canvasRoiToCanvas(roi) {
  // Map original screen coords → canvas pixels
  const oScaleX = canvasState.scaledW / canvasState.origW;
  const oScaleY = canvasState.scaledH / canvasState.origH;
  return {
    left: Math.round((roi.left - canvasState.monLeft) * oScaleX),
    top: Math.round((roi.top - canvasState.monTop) * oScaleY),
    width: Math.round(roi.width * oScaleX),
    height: Math.round(roi.height * oScaleY),
  };
}

function getCanvasXY(e) {
  const canvas = $('#screenshot-canvas');
  const rect = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left) / rect.width * canvas.width,
    y: (e.clientY - rect.top) / rect.height * canvas.height,
  };
}

function canvasMouseDown(e) {
  if (!canvasState.img) return;
  canvasState.drawing = true;
  const { x, y } = getCanvasXY(e);
  canvasState.startX = x; canvasState.startY = y;
  canvasState.curX = x; canvasState.curY = y;
}
function canvasMouseMove(e) {
  if (!canvasState.drawing) return;
  const { x, y } = getCanvasXY(e);
  canvasState.curX = x; canvasState.curY = y;
  drawCanvas();
}
function canvasMouseUp(e) {
  if (!canvasState.drawing) return;
  canvasState.drawing = false;
  const { x, y } = getCanvasXY(e);
  canvasState.curX = x; canvasState.curY = y;

  // Convert to screen coords
  const p1 = canvasToScreen(
    Math.min(canvasState.startX, canvasState.curX),
    Math.min(canvasState.startY, canvasState.curY)
  );
  const p2 = canvasToScreen(
    Math.max(canvasState.startX, canvasState.curX),
    Math.max(canvasState.startY, canvasState.curY)
  );
  const w = p2.x - p1.x;
  const h = p2.y - p1.y;
  if (w > 10 && h > 10) {
    canvasState.roi = { left: p1.x, top: p1.y, width: w, height: h };
    updateRoiInfo();
  }
  drawCanvas();
}

function updateRoiInfo() {
  const info = $('#roi-info');
  if (!info) return;
  if (canvasState.roi) {
    const { left, top, width, height } = canvasState.roi;
    info.textContent = `ROI: 左 ${left}px, 上 ${top}px, 宽 ${width}px, 高 ${height}px（原始屏幕分辨率）`;
  } else {
    info.textContent = '';
  }
}

async function loadRoiPreview() {
  try {
    const r = await fetch('/api/ocr/roi-preview');
    if (!r.ok) throw new Error(r.statusText);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const img = $('#roi-preview-img');
    img.src = url;
    img.hidden = false;
    // revoke after load
    img.onload = () => URL.revokeObjectURL(url);
  } catch (err) {
    toast(`加载 ROI 预览失败: ${err.message}`, 'err');
  }
}

async function runOcrTest() {
  const resultEl = $('#ocr-test-result');
  if (resultEl) resultEl.textContent = '识别中…';
  try {
    const data = await apiPost('/api/ocr/test', {
      synthetic_messages: [
        '[帮派]明月心：晴天-周杰伦',
        '[帮派]唐青枫：点歌 泡沫-邓紫棋',
        '[帮派]叶开：切歌',
        '[帮派]路人甲：坐标 123-456 集合',
      ],
      enqueue: false,
    });
    if (resultEl) {
      // XSS-safe: JSON.stringify result only (numbers/booleans/sanitized strings from server)
      resultEl.textContent = JSON.stringify(data, null, 2);
    }
  } catch (err) {
    if (resultEl) resultEl.textContent = `错误: ${err.message}`;
    toast(`OCR 自检失败: ${err.message}`, 'err');
  }
}

// OCR real-time log polling
let ocrLogPollTimer = null;
function startOcrLogPoll() {
  async function poll() {
    try {
      const state = await apiGet('/api/state');
      if (state.ocr && Array.isArray(state.ocr.recent_logs)) {
        renderOcrLogs(state.ocr.recent_logs);
      }
    } catch (_) {}
    ocrLogPollTimer = setTimeout(poll, 2000);
  }
  poll();
}

function renderOcrLogs(logs) {
  const list = $('#ocr-log-list');
  if (!list) return;
  list.textContent = '';
  logs.slice(0, 30).forEach(log => {
    const li = el('li', { className: 'ocr-log-item' });
    const timeEl = el('span', { className: 'ocr-log-time' },
      log.timestamp ? new Date(log.timestamp * 1000).toLocaleTimeString() : '—');
    const typeEl = el('span', { className: 'ocr-log-type' },
      log.type === 'song' ? '点歌' : log.type === 'skip' ? '切歌' : log.type || '?');
    const contentEl = el('div', { className: 'ocr-log-content' });
    // XSS-safe: textContent only
    if (log.type === 'song') {
      const t = el('span', {}, log.title || '');
      const sep = el('span', {}, log.artist ? ` — ${log.artist}` : '');
      const req = el('span', { className: 'ocr-log-requester' }, ` ♟${log.requester || ''}`);
      contentEl.appendChild(t);
      contentEl.appendChild(sep);
      contentEl.appendChild(req);
    } else {
      contentEl.textContent = log.raw_text || log.type || '';
    }
    li.appendChild(timeEl);
    li.appendChild(typeEl);
    li.appendChild(contentEl);
    list.appendChild(li);
  });
}

// ─── YTMusic auth panel ───────────────────────────────────────────────────────

function initYtmPanel() {
  const authForm = $('#ytm-auth-form');
  const statusEl = $('#ytm-auth-status');

  authForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(authForm);
    const body = {
      proxy: fd.get('proxy') || '',
      raw_headers_or_cookie: fd.get('raw_headers_or_cookie') || '',
      cookies_txt_content: fd.get('cookies_txt_content') || '',
    };
    if (statusEl) { statusEl.textContent = '绑定中…'; statusEl.className = 'status-text'; }
    try {
      const res = await apiPost('/api/ytmusic/auth', body);
      const msg = res.message || (res.authenticated ? '认证成功' : '已切换匿名模式');
      if (statusEl) {
        statusEl.textContent = msg;
        statusEl.className = `status-text ${res.authenticated ? 'ok' : 'warn'}`;
      }
      toast(msg, res.authenticated ? 'ok' : 'warn');
    } catch (err) {
      if (statusEl) { statusEl.textContent = `错误: ${err.message}`; statusEl.className = 'status-text err'; }
      toast(`绑定失败: ${err.message}`, 'err');
    }
  });

  $('#btn-load-playlists').addEventListener('click', loadPlaylists);
  $('#btn-set-fallback').addEventListener('click', setFallback);
}

async function loadPlaylists() {
  const sel = $('#sel-playlists');
  sel.textContent = '';
  sel.appendChild(el('option', { value: '' }, '加载中…'));
  try {
    const playlists = await apiGet('/api/ytmusic/playlists');
    sel.textContent = '';
    playlists.forEach(p => {
      const opt = el('option', { value: p.playlist_id });
      opt.textContent = `${p.title}${p.count ? ` (${p.count}首)` : ''}`;
      sel.appendChild(opt);
    });
    if (!playlists.length) sel.appendChild(el('option', { value: '' }, '（无歌单）'));
  } catch (err) {
    sel.textContent = '';
    sel.appendChild(el('option', { value: '' }, `加载失败: ${err.message}`));
  }
}

async function setFallback() {
  const statusEl = $('#fallback-status');
  const selVal = $('#sel-playlists').value;
  const customSrc = $('#inp-fallback-src').value.trim();
  const src = customSrc || selVal;
  const shuffle = $('#sw-shuffle').checked;

  if (!src) { toast('请选择歌单或输入歌单 URL/搜索词', 'warn'); return; }
  if (statusEl) { statusEl.textContent = '设置中…'; statusEl.className = 'status-text'; }
  try {
    await apiPost('/api/ytmusic/fallback', { fallback_source: src, shuffle });
    const msg = '帮派兜底歌单已更新，播音时段内将自动切换';
    if (statusEl) { statusEl.textContent = msg; statusEl.className = 'status-text ok'; }
    toast(msg, 'ok');
  } catch (err) {
    if (statusEl) { statusEl.textContent = `错误: ${err.message}`; statusEl.className = 'status-text err'; }
    toast(`设置失败: ${err.message}`, 'err');
  }
}

// ─── LAN address ──────────────────────────────────────────────────────────────

function initLanBanner() {
  const lanUrl = $('#lan-url-text');
  const copyBtn = $('#btn-copy-lan');

  // Show current page URL as LAN address hint
  const url = location.href;
  if (lanUrl) lanUrl.textContent = url;

  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(url);
        toast('局域网地址已复制，可粘贴给队友！', 'ok');
      } catch {
        toast(`请手动复制：${url}`, 'warn', 6000);
      }
    });
  }
}

// ─── Periodic full state poll ─────────────────────────────────────────────────
// (fallback when WS state events are sparse)

let statePollTimer = null;
async function pollState() {
  try {
    const data = await apiGet('/api/state');
    handleStateUpdate(data);
  } catch (_) {}
  statePollTimer = setTimeout(pollState, 5000);
}

function initSchedulePanel() {
  const overrideBtn = $('#btn-schedule-override');
  if (overrideBtn) {
    overrideBtn.addEventListener('click', async () => {
      const targetActive = overrideBtn.dataset.targetActive === 'true';
      try {
        await apiPost('/api/schedule/override', { active: targetActive });
        const st = await apiGet('/api/state');
        handleStateUpdate(st);
        toast(targetActive ? '已开启临时试播' : '已结束试播并恢复静音待机', 'ok');
      } catch (err) {
        toast(`切换试播状态失败: ${err.message}`, 'err');
      }
    });
  }

  const schedForm = $('#schedule-config-form');
  if (schedForm) {
    schedForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const start = $('#inp-sched-start')?.value || '19:30';
      const end = $('#inp-sched-end')?.value || '20:20';
      if (end <= start) {
        toast('结束时间必须晚于开始时间', 'warn');
        return;
      }
      const days = $$('input[name="sched_day"]:checked')
        .map(cb => Number(cb.value))
        .filter(n => Number.isInteger(n) && n >= 0 && n <= 6);
      const payload = {
        enabled: Boolean($('#sw-schedule-enabled')?.checked),
        days,
        start,
        end,
        clear_queue_on_end: Boolean($('#sw-sched-clear-end')?.checked),
      };
      try {
        await apiPost('/api/schedule/config', payload);
        const st = await apiGet('/api/state');
        handleStateUpdate(st);
        toast('定时播音设置已保存并同步 OCR 时段', 'ok');
      } catch (err) {
        toast(`保存定时设置失败: ${err.message}`, 'err');
      }
    });
  }
}

// ─── Boot ─────────────────────────────────────────────────────────────────────

async function main() {
  initTabs();
  initSliders();
  initPlayerControls();
  initAudioForm();
  initOcrPanel();
  initYtmSearch();
  initYtmPanel();
  initSchedulePanel();
  initLanBanner();

  // Fix #1: Bind queue action buttons (addSong, clearQueue, song-input Enter)
  $('#btn-add-song').addEventListener('click', () => addSong(false));
  $('#btn-add-top').addEventListener('click', () => addSong(true));
  $('#btn-clear-queue').addEventListener('click', clearQueue);
  $('#song-input').addEventListener('keydown', e => { if (e.key === 'Enter') addSong(false); });

  // Load initial state
  try {
    const data = await apiGet('/api/state');
    handleStateUpdate(data);
  } catch (err) {
    toast(`连接后端失败，请确认服务已启动: ${err.message}`, 'err', 8000);
  }

  // Load audio devices for Tab 2
  await loadAudioDevices();

  // Load playlists for Tab 4
  loadPlaylists().catch(() => {});

  // Start WebSocket
  initWS();

  // Start periodic state poll
  pollState();

  // Start OCR log poll
  startOcrLogPoll();
}

main();

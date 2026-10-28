const app = (() => {
  let token = null;
  let status = null;        // /status payload
  let view = 'find';        // find | spots
  let query = '';
  let results = null;       // destination search results
  let searching = false;
  let dest = null;          // {name, lat, lon, address}
  let plan = null;          // /plan payload
  let planning = false;
  let planErr = null;
  let planSeq = 0;          // ignore replies to superseded plans
  let sort = 'best';        // best | walk | cost
  let selected = null;      // option key
  let picking = null;       // 'dest' | 'spot' while waiting for a map tap
  let flash = null;
  let flashTimer = null;
  let retryTimer = null;
  let map = null;
  let layers = {};
  let fittedFor = null;     // plan the map was last zoomed to
  let headFor;              // dest the controls were last drawn for
  let planTimer = null;
  const form = { ...defaultArrive(), hours: 2, radius: null, venue: '', eventStart: '19:00' };

  const WALK_VALUE_PER_MIN = 0.20;   // matches the backend's ranking
  const OVER_LIMIT_PENALTY = 25;
  const STAYS = [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8];
  const RADII = [[400, '¼ mi'], [800, '½ mi'], [1200, '¾ mi'], [1600, '1 mi'], [2000, '1¼ mi']];

  // ── Helpers ────────────────────────────────────────────────

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  function ymd(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  // Day + time as two selects rather than a datetime-local picker: Firefox
  // fires change on every field of that picker, which made it hard to set.
  function defaultArrive() {
    const d = new Date(Date.now() + 15 * 60000);
    d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15, 0, 0);
    return { day: ymd(d), time: `${pad(d.getHours())}:${pad(d.getMinutes())}` };
  }

  function dayOptions() {
    const out = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date();
      d.setDate(d.getDate() + i);
      const label = i === 0 ? 'Today' : i === 1 ? 'Tomorrow'
        : d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
      out.push([ymd(d), label]);
    }
    // Keep a day that's rolled into the past selectable (panel left open overnight).
    if (!out.some(([v]) => v === form.day)) {
      out.unshift([form.day, new Date(`${form.day}T12:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })]);
    }
    return out;
  }

  function timeOptions() {
    const out = [];
    for (let m = 0; m < 24 * 60; m += 15) {
      const d = new Date(2000, 0, 1, 0, m);
      out.push([`${pad(d.getHours())}:${pad(d.getMinutes())}`,
        d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })]);
    }
    return out;
  }

  function fmtMoney(x) { return `$${Number(x).toFixed(2)}`; }

  function fmtWhen(iso) {
    const d = new Date(iso);
    return d.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  }

  function fmtClock(iso) {
    return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  function fmtDist(m) {
    const ft = m * 3.281;
    return ft < 528 ? `${Math.round(ft / 10) * 10} ft` : `${(ft / 5280).toFixed(2)} mi`;
  }

  function fmtWalk(o) {
    const mins = Math.max(1, Math.round(o.walk_min));
    return `${o.walk_source === 'google' ? '' : '~'}${mins} min walk · ${fmtDist(o.walk_m)}`;
  }

  function fmtHours(h) { return h < 1 ? `${h * 60} min` : `${h} hr`; }

  function ago(iso) {
    if (!iso) return 'never';
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return mins < 1 ? 'just now' : `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    return hrs < 24 ? `${hrs} hr ago` : `${Math.round(hrs / 24)} days ago`;
  }

  function score(o) {
    return o.cost + o.walk_min * WALK_VALUE_PER_MIN + (o.over_limit ? OVER_LIMIT_PENALTY : 0);
  }

  function sorted(opts) {
    const out = [...opts];
    if (sort === 'walk') out.sort((a, b) => a.walk_min - b.walk_min);
    else if (sort === 'cost') out.sort((a, b) => a.cost - b.cost || a.walk_min - b.walk_min);
    else out.sort((a, b) => score(a) - score(b));
    return out;
  }

  function costColor(cost, blocked) {
    if (blocked) return '#666';
    if (cost === 0) return '#30d158';
    return cost < 5 ? '#ffd60a' : '#ff453a';
  }

  async function api(method, path, body) {
    const res = await fetch(`/api/curb${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let json = null;
    try { json = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new Error(json?.error || `HTTP ${res.status}`);
    return json;
  }

  function showFlash(text, err = false) {
    flash = { text, err };
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flash = null; renderFlash(); }, err ? 8000 : 4000);
    renderFlash();
  }

  // ── Rendering ──────────────────────────────────────────────

  function render() {
    if (!status) return;
    renderTabs();
    renderFlash();
    document.getElementById('find').classList.toggle('hidden', view !== 'find');
    document.getElementById('spots').classList.toggle('hidden', view !== 'spots');
    if (view === 'find') {
      renderBanners();
      renderSearch();
      // Controls are redrawn only when the destination changes, so a plan
      // coming back never wipes a control mid-edit.
      if (headFor !== dest) {
        headFor = dest;
        renderPlanHead();
      }
      renderPlanBody();
      updateMap();
    } else {
      renderSpots();
    }
    renderFooter();
  }

  function renderTabs() {
    const tab = (v, label, n) =>
      `<button class="tab ${view === v ? 'on' : ''}" onclick="app.show('${v}')">${label}${n === undefined ? '' : `<span class="n">${n}</span>`}</button>`;
    document.getElementById('tabs').innerHTML = tab('find', 'Find parking') + tab('spots', 'Saved spots', status.spots.length);
  }

  function renderFlash() {
    document.getElementById('flash').innerHTML = flash
      ? `<div class="flash ${flash.err ? 'err' : ''}">${esc(flash.text)}</div>` : '';
  }

  function renderBanners() {
    const out = [];
    if (picking) {
      const what = picking === 'spot' ? 'Tap the map where you park' : 'Tap the map where you\'re going';
      out.push(`<div class="banner pick"><span><strong>${what}</strong></span><button class="btn sm" onclick="app.cancelPick()">Cancel</button></div>`);
    }
    if (!status.configured) {
      out.push(`<div class="banner"><strong>No Google key yet.</strong> Add <code>google_api_key: !secret curb_google_api_key</code> under <code>curb:</code> in configuration.yaml for place search and real walk times. "Near me" and "Pick on map" work without it.</div>`);
    }
    if (!status.meters.count) {
      out.push(status.meters.error
        ? `<div class="banner"><strong>Couldn't load the city's meter map.</strong> ${esc(status.meters.error)}</div>`
        : `<div class="banner"><strong>Loading the city's meter map…</strong> First start takes about a minute.</div>`);
    }
    document.getElementById('banners').innerHTML = out.join('');
  }

  function renderSearch() {
    const el = document.getElementById('search-area');
    if (dest) { el.innerHTML = ''; return; }
    const list = results === null ? '' : results.length
      ? `<div class="results">${results.map((r, i) => `
          <div class="result" onclick="app.choose(${i})">
            <div class="name">${esc(r.name)}</div>
            <div class="addr">${esc(r.address)}</div>
          </div>`).join('')}</div>`
      : `<div class="empty">No places found.</div>`;
    el.innerHTML = `
      <form class="search" onsubmit="app.search(event)">
        <input id="q" type="search" placeholder="Where are you going?" value="${esc(query)}"
          oninput="app.setQuery(this.value)" ${status.configured ? '' : 'disabled'} />
        <button class="btn primary" ${searching || !status.configured ? 'disabled' : ''}>${searching ? 'Searching…' : 'Search'}</button>
      </form>
      <div class="quick">
        <button class="btn sm" onclick="app.nearMe()">📍 Near me</button>
        <button class="btn sm ${picking === 'dest' ? 'on' : ''}" onclick="app.pick('dest')">Pick on map</button>
      </div>
      ${list}`;
  }

  function renderPlanHead() {
    const el = document.getElementById('plan-head');
    if (!dest) { el.innerHTML = ''; return; }
    const venues = status.venues.map(v => `<option value="${v.key}" ${form.venue === v.key ? 'selected' : ''}>${esc(v.label)}</option>`).join('');
    el.innerHTML = `
      <div class="dest">
        <span>${esc(dest.name)}</span>
        <a href="#" class="clear" onclick="app.clearDest(event)">Change</a>
      </div>
      <div class="controls">
        <div class="ctl arrive"><label>Arrive</label>
          <div class="pair">
            <select onchange="app.setForm('day', this.value)">
              ${dayOptions().map(([v, l]) => `<option value="${v}" ${form.day === v ? 'selected' : ''}>${l}</option>`).join('')}
            </select>
            <select onchange="app.setForm('time', this.value)">
              ${timeOptions().map(([v, l]) => `<option value="${v}" ${form.time === v ? 'selected' : ''}>${l}</option>`).join('')}
            </select>
          </div></div>
        <div class="ctl"><label>Stay</label>
          <select onchange="app.setForm('hours', +this.value)">
            ${STAYS.map(h => `<option value="${h}" ${form.hours === h ? 'selected' : ''}>${fmtHours(h)}</option>`).join('')}
          </select></div>
        <div class="ctl"><label>Within</label>
          <select onchange="app.setForm('radius', +this.value)">
            ${RADII.map(([m, l]) => `<option value="${m}" ${form.radius === m ? 'selected' : ''}>${l}</option>`).join('')}
          </select></div>
      </div>
      <div class="controls event">
        <div class="ctl"><label>Event nearby (raises meter rates)</label>
          <select onchange="app.setForm('venue', this.value)">
            <option value="">None</option>${venues}
          </select></div>
        <div class="ctl ${form.venue ? '' : 'hidden'}"><label>Event starts</label>
          <input type="time" value="${form.eventStart}" onchange="app.setForm('eventStart', this.value)" /></div>
      </div>`;
  }

  function optionCard(o, i) {
    const free = o.cost === 0;
    const badge = o.kind === 'spot' ? ' <span class="pill purple">Saved</span>' : '';
    const warns = (o.warnings || []).map(w => `<div class="warn">⚠ ${esc(w)}</div>`).join('');
    const save = o.kind === 'meter'
      ? `<button class="btn sm" onclick="event.stopPropagation(); app.saveFromOption('${o.key}')">Save as spot</button>` : '';
    return `
      <div class="card ${selected === o.key ? 'sel' : ''}" id="opt-${o.key}" onclick="app.select('${o.key}')">
        <div class="row-head">
          <div>
            <div class="row-title"><span class="rank">${i + 1}</span>${esc(o.name)}${badge}</div>
            <div class="walk">🚶 ${fmtWalk(o)}</div>
          </div>
          <div class="cost ${free ? 'free' : ''}">${free ? 'Free' : fmtMoney(o.cost)}</div>
        </div>
        ${o.note ? `<div class="row-text">${esc(o.note)}</div>` : ''}
        ${o.rules ? `<div class="row-meta">${esc(o.rules)}</div>` : ''}
        ${warns}
        <div class="row-actions">
          <a class="btn sm primary" href="${esc(o.nav_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Navigate</a>
          ${save}
        </div>
      </div>`;
  }

  function renderPlanBody() {
    const el = document.getElementById('plan-body');
    if (!dest) { el.innerHTML = ''; return; }
    if (planErr) { el.innerHTML = `<div class="flash err">${esc(planErr)}</div>`; return; }
    if (!plan) { el.innerHTML = `<div class="empty">${planning ? 'Finding parking…' : ''}</div>`; return; }

    const radiusLabel = (RADII.find(([m]) => m === plan.radius) || [0, `${plan.radius} m`])[1];
    const lines = [
      `Arriving ${esc(fmtWhen(plan.arrive))} for ${fmtHours(plan.minutes / 60)} · ${plan.counts.meters} metered blocks within ${radiusLabel}, ${plan.counts.free} free for your stay`
        + (plan.counts.blocked ? `, ${plan.counts.blocked} with no parking during it` : ''),
    ];
    if (plan.event) {
      const start = new Date(plan.event.start);
      const from = new Date(start.getTime() - 180 * 60000);
      const to = new Date(start.getTime() + 60 * 60000);
      lines.push(plan.event.venue === 'convention'
        ? `${esc(plan.event.label)} event rates apply all day on meters in its zone.`
        : `${esc(plan.event.label)} event rates apply ${fmtClock(from)}–${fmtClock(to)} on meters in its zone.`);
    }
    if (plan.walk_error) lines.push(`Walk times are estimates (~): ${esc(plan.walk_error)}`);
    if (planning) lines.push('Updating…');

    const opts = sorted(plan.options);
    const sortBtn = (k, label) => `<button class="btn sm ${sort === k ? 'on' : ''}" onclick="app.setSort('${k}')">${label}</button>`;
    const list = opts.length
      ? opts.map(optionCard).join('')
      : `<div class="empty">No metered blocks or saved spots in range. Try a wider radius, or check the lots below.</div>`;

    const lots = plan.lots.length ? `
      <details class="lots">
        <summary>Lots & ramps nearby (${plan.lots.length})</summary>
        <div class="hint">From OpenStreetMap, which rarely lists prices. Check SpotHero or ParkWhiz for garage rates.</div>
        ${plan.lots.map(l => `
          <div class="small-row">
            <div>
              <div>${esc(l.name)} ${l.fee === 'free' ? '<span class="pill green">Free (per OSM)</span>' : l.fee === 'paid' ? '<span class="pill grey">Paid</span>' : ''}</div>
              <div class="meta">${esc(l.kind)} · ~${Math.max(1, Math.round(l.walk_min))} min walk${l.operator ? ` · ${esc(l.operator)}` : ''}${l.hours ? ` · ${esc(l.hours)}` : ''}</div>
            </div>
            <a class="btn sm" href="${esc(l.nav_url)}" target="_blank" rel="noopener">Navigate</a>
          </div>`).join('')}
      </details>`
      : plan.lots_pending ? '<div class="status">Loading lots & ramps from OpenStreetMap…</div>'
      : plan.lots_error ? `<div class="status">Lots unavailable: ${esc(plan.lots_error)}</div>` : '';

    el.innerHTML = `
      <div class="summary">${lines.join('<br>')}</div>
      <div class="sorts">Sort ${sortBtn('best', 'Best')}${sortBtn('walk', 'Closest')}${sortBtn('cost', 'Cheapest')}</div>
      ${list}
      ${lots}`;
  }

  function renderSpots() {
    const el = document.getElementById('spots');
    const rows = status.spots.map(s => `
      <div class="card">
        <div class="row-head">
          <div class="row-title">${esc(s.name)}</div>
          <div class="cost ${s.rate ? '' : 'free'}">${s.rate ? `${fmtMoney(s.rate)}/hr` : 'Free'}</div>
        </div>
        ${s.notes ? `<div class="row-text">${esc(s.notes)}</div>` : ''}
        <div class="row-actions">
          <a class="btn sm primary" href="https://www.google.com/maps/dir/?api=1&destination=${s.lat},${s.lon}&travelmode=driving" target="_blank" rel="noopener">Navigate</a>
          <button class="btn sm" onclick="app.editSpot(${s.id})">Edit</button>
        </div>
      </div>`).join('');
    el.innerHTML = `
      <div class="sorts" style="justify-content:space-between">
        <span>Shared with everyone in the house. Saved spots show up in results when they're in range.</span>
        <button class="btn sm primary" onclick="app.pick('spot')">+ Add spot</button>
      </div>
      ${rows || '<div class="empty">No saved spots yet. Add the places you already park, or tap "Save as spot" on a result.</div>'}`;
  }

  function renderFooter() {
    const m = status.meters;
    const line = m.count
      ? `City meter data: ${m.count.toLocaleString()} blocks · updated ${ago(m.updated)} · <a href="#" onclick="app.refreshMeters(event)">Refresh</a>`
      : '';
    document.getElementById('footer').innerHTML = `
      ${line}${m.error && m.count ? `<div class="status err">${esc(m.error)}</div>` : ''}
      <div>Rules come from the city's meter map. Posted signs win; holidays aren't accounted for.</div>`;
  }

  // ── Map ────────────────────────────────────────────────────

  function ensureMap() {
    const el = document.getElementById('map');
    const wasHidden = el.classList.contains('hidden');
    el.classList.remove('hidden');
    document.getElementById('legend').classList.toggle('hidden', !plan);
    if (!map) {
      map = L.map('map').setView([status.home.lat, status.home.lon], 14);
      // Esri Dark Gray Canvas: no key, no Referer check. CARTO now needs a
      // key, and OSM's tile servers block requests without a Referer, which
      // HA strips (Referrer-Policy: no-referrer). Labels are a separate layer.
      const esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas';
      const opts = { maxNativeZoom: 16, maxZoom: 19 };
      L.tileLayer(`${esri}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
        ...opts, attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
      }).addTo(map);
      L.tileLayer(`${esri}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, opts).addTo(map);
      layers.meters = L.layerGroup().addTo(map);
      layers.lots = L.layerGroup().addTo(map);
      layers.spots = L.layerGroup().addTo(map);
      layers.pins = L.layerGroup().addTo(map);
      map.on('click', onMapClick);
    } else if (wasHidden) {
      setTimeout(() => map.invalidateSize(), 0);
    }
  }

  function pinIcon(cls, text, size = 26) {
    return L.divIcon({ className: '', html: `<div class="pin ${cls}">${text}</div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
  }

  function updateMap() {
    const el = document.getElementById('map');
    if (!dest && !picking) {
      el.classList.add('hidden');
      document.getElementById('legend').classList.add('hidden');
      return;
    }
    ensureMap();
    Object.values(layers).forEach(l => l.clearLayers());

    for (const s of status.spots) {
      L.marker([s.lat, s.lon], { icon: pinIcon('spot', 'S', 20) })
        .bindTooltip(esc(s.name)).addTo(layers.spots);
    }
    if (!plan) return;

    for (const m of plan.map.meters) {
      const color = costColor(m.cost, m.blocked);
      L.polygon(m.ring, { color, weight: selected === m.key ? 4 : 2, fillOpacity: 0.3, dashArray: m.blocked ? '4 4' : null })
        .on('click', e => { L.DomEvent.stopPropagation(e); if (!m.blocked) select(m.key); })
        .addTo(layers.meters);
    }
    for (const l of plan.lots) {
      L.marker([l.lat, l.lon], { icon: pinIcon('lot', 'P', 20) }).bindTooltip(esc(l.name)).addTo(layers.lots);
    }
    const opts = sorted(plan.options).slice(0, 10);
    opts.forEach((o, i) => {
      const cls = `${o.cost === 0 ? 'free' : ''} ${o.kind === 'spot' ? 'spot' : ''} ${selected === o.key ? 'sel' : ''}`;
      L.marker([o.lat, o.lon], { icon: pinIcon(cls, i + 1), zIndexOffset: selected === o.key ? 1000 : 0 })
        .on('click', () => select(o.key)).addTo(layers.pins);
    });
    L.marker([plan.destination.lat, plan.destination.lon], { icon: pinIcon('dest', '', 18), zIndexOffset: 2000 })
      .bindTooltip(esc(plan.destination.name)).addTo(layers.pins);

    if (fittedFor !== plan) {
      fittedFor = plan;
      const pts = [[plan.destination.lat, plan.destination.lon], ...opts.slice(0, 6).map(o => [o.lat, o.lon])];
      map.fitBounds(pts, { padding: [30, 30], maxZoom: 17 });
    }
  }

  function onMapClick(e) {
    if (!picking) return;
    const { lat, lng } = e.latlng;
    const what = picking;
    picking = null;
    if (what === 'dest') {
      setDest({ name: 'Dropped pin', lat, lon: lng });
    } else {
      render();
      openSpotModal({ name: '', notes: '', rate: 0, lat, lon: lng });
    }
  }

  // ── Actions ────────────────────────────────────────────────

  async function load() {
    try {
      status = await api('GET', '/status');
    } catch (e) {
      document.getElementById('footer').innerHTML = `<div class="status err">Couldn't load Curb: ${esc(e.message)}</div>`;
      return;
    }
    if (!form.radius) form.radius = status.default_radius;
    render();
    clearTimeout(retryTimer);
    if (!status.meters.count && !status.meters.error) retryTimer = setTimeout(load, 10000);
  }

  function show(v) {
    view = v;
    if (v === 'spots') picking = null;
    render();
  }

  function setQuery(v) { query = v; }

  async function search(e) {
    e.preventDefault();
    if (query.trim().length < 2) return;
    searching = true;
    renderSearch();
    try {
      results = (await api('GET', `/search?q=${encodeURIComponent(query.trim())}`)).results;
    } catch (err) {
      results = null;
      showFlash(err.message, true);
    }
    searching = false;
    renderSearch();
  }

  function choose(i) {
    const r = results[i];
    setDest({ name: r.name, address: r.address, lat: r.lat, lon: r.lon });
  }

  function nearMe() {
    if (!navigator.geolocation) { showFlash('Location isn\'t available here — search or pick on the map', true); return; }
    showFlash('Finding your location…');
    navigator.geolocation.getCurrentPosition(
      pos => setDest({ name: 'Near me', lat: pos.coords.latitude, lon: pos.coords.longitude }),
      err => showFlash(`Couldn't get your location (${err.message}) — search or pick on the map`, true),
      { enableHighAccuracy: true, timeout: 15000 },
    );
  }

  function pick(what) {
    view = 'find';
    picking = picking === what ? null : what;
    render();
    document.getElementById('map').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function cancelPick() {
    picking = null;
    render();
  }

  function setDest(d) {
    dest = d;
    results = null;
    plan = null;
    selected = null;
    runPlan();
  }

  function clearDest(e) {
    e.preventDefault();
    dest = null;
    plan = null;
    planErr = null;
    selected = null;
    render();
    document.getElementById('q')?.focus();
  }

  function setForm(k, v) {
    form[k] = v;
    if (k === 'venue') renderPlanHead();  // shows/hides the event start time
    // The event time input can fire on each digit; wait for typing to settle.
    clearTimeout(planTimer);
    planTimer = setTimeout(runPlan, k === 'eventStart' ? 600 : 0);
  }

  function setSort(k) {
    sort = k;
    renderPlanBody();
    updateMap();
  }

  async function runPlan() {
    if (!dest) return;
    const seq = ++planSeq;
    planning = true;
    planErr = null;
    render();
    const body = {
      lat: dest.lat, lon: dest.lon, name: dest.name, arrive: `${form.day}T${form.time}`,
      hours: form.hours, radius: form.radius,
      event: form.venue ? { venue: form.venue, start: form.eventStart } : null,
    };
    try {
      const res = await api('POST', '/plan', body);
      if (seq !== planSeq) return;
      plan = res;
      if (!plan.options.some(o => o.key === selected)) selected = null;
    } catch (err) {
      if (seq !== planSeq) return;
      planErr = err.message;
    }
    planning = false;
    render();
    if (plan?.lots_pending) loadLots(seq);
  }

  async function loadLots(seq) {
    const p = plan;
    try {
      const res = await api('GET', `/lots?lat=${p.destination.lat}&lon=${p.destination.lon}&radius=${p.radius}`);
      if (seq !== planSeq) return;
      p.lots = res.lots;
      p.lots_error = res.error;
    } catch (err) {
      if (seq !== planSeq) return;
      p.lots_error = err.message;
    }
    p.lots_pending = false;
    renderPlanBody();
    updateMap();
  }

  function select(key) {
    selected = selected === key ? null : key;
    renderPlanBody();
    updateMap();
    const o = plan?.options.find(x => x.key === key);
    if (selected && o) {
      map.panTo([o.lat, o.lon]);
      document.getElementById(`opt-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  async function refreshMeters(e) {
    e.preventDefault();
    showFlash('Re-downloading the city\'s meter map…');
    try {
      status = await api('POST', '/meters/refresh');
      showFlash(status.meters.error ? status.meters.error : `Loaded ${status.meters.count.toLocaleString()} meter blocks`, !!status.meters.error);
      render();
      if (dest) runPlan();
    } catch (err) {
      showFlash(err.message, true);
    }
  }

  // ── Saved spots ────────────────────────────────────────────

  function saveFromOption(key) {
    const o = plan.options.find(x => x.key === key);
    if (o) openSpotModal({ name: o.name, notes: o.rules || '', rate: 0, lat: o.lat, lon: o.lon });
  }

  function editSpot(id) {
    const s = status.spots.find(x => x.id === id);
    if (s) openSpotModal(s);
  }

  function openSpotModal(s) {
    document.getElementById('modal').innerHTML = `
      <div class="modal-bg" onclick="if (event.target === this) app.closeModal()">
        <div class="modal">
          <h2>${s.id ? 'Edit spot' : 'Save a spot'}</h2>
          <div class="field"><label>Name</label>
            <input id="sp-name" value="${esc(s.name)}" placeholder="Harmon & 11th" /></div>
          <div class="field"><label>Cost ($/hr, 0 = free)</label>
            <input id="sp-rate" type="number" min="0" max="50" step="0.25" value="${s.rate ?? 0}" />
            <div class="hint">Used to price this spot in results.</div></div>
          <div class="field"><label>Notes</label>
            <textarea id="sp-notes" placeholder="Twins nights: quieter than North Loop. Free after 6 on weekdays.">${esc(s.notes)}</textarea></div>
          <div class="hint">${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}</div>
          <div id="sp-err" class="form-err"></div>
          <div class="modal-actions">
            ${s.id ? `<button class="btn danger" onclick="app.deleteSpot(${s.id})">Delete</button>` : ''}
            <div class="right">
              <button class="btn" onclick="app.closeModal()">Cancel</button>
              <button class="btn primary" onclick="app.saveSpot(${s.id || 0}, ${s.lat}, ${s.lon})">Save</button>
            </div>
          </div>
        </div>
      </div>`;
    document.getElementById('sp-name').focus();
  }

  function closeModal() {
    document.getElementById('modal').innerHTML = '';
  }

  async function saveSpot(id, lat, lon) {
    const body = {
      name: document.getElementById('sp-name').value,
      rate: document.getElementById('sp-rate').value || 0,
      notes: document.getElementById('sp-notes').value,
      lat, lon,
    };
    try {
      await (id ? api('PUT', `/spots/${id}`, body) : api('POST', '/spots', body));
    } catch (err) {
      document.getElementById('sp-err').textContent = err.message;
      return;
    }
    closeModal();
    await load();
    showFlash(id ? 'Spot updated' : 'Spot saved');
    if (dest) runPlan();
  }

  async function deleteSpot(id) {
    if (!confirm('Delete this saved spot?')) return;
    try {
      await api('DELETE', `/spots/${id}`);
    } catch (err) {
      document.getElementById('sp-err').textContent = err.message;
      return;
    }
    closeModal();
    await load();
    if (dest) runPlan();
  }

  // ── Boot ───────────────────────────────────────────────────

  let initialized = false;
  window.addEventListener('message', e => {
    if (e.data?.type === 'auth' && e.data.token) {
      token = e.data.token;
      if (!initialized) {
        initialized = true;
        load();
      }
    }
  });

  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  return {
    show, setQuery, search, choose, nearMe, pick, cancelPick, clearDest, setForm, setSort,
    select, refreshMeters, saveFromOption, editSpot, saveSpot, deleteSpot, closeModal,
  };
})();

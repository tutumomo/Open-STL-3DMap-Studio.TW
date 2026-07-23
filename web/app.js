/* Open STL-3DMap Studio - browser side.
 * The browser owns the project object; the server is stateless. */

// Printable bodies, in print order. Colours mirror config.py DEFAULT_BANDS.
const BODY_DEFS = [
  { key: 'base', label: 'Water / onderplaat', color: '#2f6f9f', slot: 1 },
  { key: 'roads', label: 'Wegen', color: '#e8e4d9', slot: 3 },
  { key: 'land', label: 'Land / maaiveld', color: '#7d9a6a', slot: 2 },
  { key: 'shrubs', label: 'Struiken / groen', color: '#6f9a52', slot: 2 },
  { key: 'trees', label: 'Bomen / bos', color: '#3f6a2e', slot: 2 },
  { key: 'buildings', label: 'Gebouwen', color: '#b7643c', slot: 4 },
  { key: 'design', label: 'Eigen ontwerp', color: '#d9b310', slot: 4 },
];

const DEFAULT_PROJECT = () => ({
  id: 'kaart',
  name: 'Kaart',
  bbox_wgs84: null,
  settings: {
    max_size_mm: 200, layer_height: 0.2, first_layer_height: 0.2,
    base_top: 1.2, road_top: 1.8, land_top: 2.0,
    height_exaggeration: 1.5, min_building_height_mm: 0.6, max_building_height_mm: 30,
    min_road_width_mm: 0.9, min_feature_area_mm2: 0.8, simplify_mm: 0.06,
    shrub_height_mm: 1.0, tree_height_mm: 3.0, tree_radius_mm: 0.9,
    road_style: 'recessed', detailed_buildings: false,
    include_water: true, include_roads: true, include_buildings: true,
    include_shrubs: false, include_trees: false,
    slots: {},
  },
  hidden_building_ids: [],
  design: {
    filename: '', anchor_lonlat: null, rotation_deg: 0,
    relative_scale: 1.0, z_offset_mm: 0, slot: 4, enabled: true,
  },
});

// Merge stored project over defaults so projects saved by an older version pick
// up new settings (vegetation, slots, detail) instead of arriving undefined.
function withDefaults(stored) {
  const base = DEFAULT_PROJECT();
  if (!stored) return base;
  return {
    ...base, ...stored,
    settings: { ...base.settings, ...(stored.settings || {}),
                slots: { ...(stored.settings && stored.settings.slots || {}) } },
    design: { ...base.design, ...(stored.design || {}) },
  };
}

let project = withDefaults(load());
let buildingLayer = null, contextLayer = null, areaRect = null, designMarker = null;
let mode = null;            // null | 'area' | 'box'
let hideMode = 'click';     // 'click' | 'box'

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------- map
const map = L.map('map', { preferCanvas: true, zoomControl: true }).setView([52.09, 5.12], 15);

const baseLayers = {
  'Kaart': L.tileLayer(
    'https://service.pdok.nl/brt/achtergrondkaart/wmts/v2_0/standaard/EPSG:3857/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '&copy; PDOK / Kadaster' }).addTo(map),
  'Grijs': L.tileLayer(
    'https://service.pdok.nl/brt/achtergrondkaart/wmts/v2_0/grijs/EPSG:3857/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '&copy; PDOK / Kadaster' }),
  'Luchtfoto': L.tileLayer(
    'https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg',
    { maxZoom: 19, attribution: '&copy; PDOK Luchtfoto' }),
};
L.control.layers(baseLayers, {}, { position: 'topright' }).addTo(map);

// --------------------------------------------------------------------------- helpers
function busy(on, text) {
  $('busy').classList.toggle('hidden', !on);
  if (text) $('busy-text').textContent = text;
}

function status(text, cls) {
  const el = $('status');
  el.textContent = text;
  el.className = cls || '';
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function save() { localStorage.setItem('3dmaps.project', JSON.stringify(project)); }
function load() {
  try { return JSON.parse(localStorage.getItem('3dmaps.project')); } catch { return null; }
}

// --------------------------------------------------------------------------- search
let searchTimer = null;
$('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $('search-results').classList.add('hidden'); return; }
  searchTimer = setTimeout(() => runSearch(q), 300);
});

async function runSearch(q) {
  try {
    const { results } = await api('/api/search?q=' + encodeURIComponent(q));
    const ul = $('search-results');
    ul.innerHTML = '';
    results.forEach((r) => {
      const li = document.createElement('li');
      li.innerHTML = `${r.label}<small>${r.type}</small>`;
      li.onclick = () => {
        map.setView([r.lat, r.lon], 16);
        ul.classList.add('hidden');
        $('search').value = r.label;
      };
      ul.appendChild(li);
    });
    ul.classList.toggle('hidden', results.length === 0);
  } catch (err) {
    status('Zoeken mislukt: ' + err.message, 'err');
  }
}

// --------------------------------------------------------------------------- area selection
function setMode(next) {
  mode = next;
  map.getContainer().classList.toggle('drawing', next !== null);
  if (next) { map.dragging.disable(); } else { map.dragging.enable(); }
  $('btn-draw').classList.toggle('active', next === 'area');
}

let dragStart = null, dragRect = null;

map.on('mousedown', (e) => {
  if (!mode) return;
  dragStart = e.latlng;
  const style = mode === 'area'
    ? { color: '#4f9be0', weight: 2, fillOpacity: 0.08 }
    : { color: '#d9604a', weight: 1, dashArray: '4 3', fillOpacity: 0.12 };
  dragRect = L.rectangle(L.latLngBounds(dragStart, dragStart), style).addTo(map);
});

map.on('mousemove', (e) => {
  if (!dragStart || !dragRect) return;
  dragRect.setBounds(L.latLngBounds(dragStart, e.latlng));
});

map.on('mouseup', (e) => {
  if (!dragStart || !dragRect) return;
  const bounds = L.latLngBounds(dragStart, e.latlng);
  const wasMode = mode;
  map.removeLayer(dragRect);
  dragStart = null; dragRect = null;

  if (bounds.getNorth() - bounds.getSouth() < 1e-5) { setMode(null); return; }

  if (wasMode === 'area') {
    applyBbox([bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()]);
    setMode(null);
  } else if (wasMode === 'box') {
    hideInBounds(bounds, !e.originalEvent.shiftKey);
  }
});

function applyBbox(bbox) {
  project.bbox_wgs84 = bbox;
  save();
  drawAreaRect();
  status('Gebied gekozen. Klik "Kaartdata ophalen".');
}

function drawAreaRect() {
  if (areaRect) { map.removeLayer(areaRect); areaRect = null; }
  if (!project.bbox_wgs84) return;
  const [w, s, e, n] = project.bbox_wgs84;
  areaRect = L.rectangle([[s, w], [n, e]], {
    color: '#4f9be0', weight: 2, fill: false, dashArray: '6 4',
  }).addTo(map);
}

$('btn-draw').onclick = () => setMode(mode === 'area' ? null : 'area');

$('btn-center-area').onclick = () => {
  const c = map.getCenter();
  const w = Math.max(50, Number($('area-w').value) || 1000);
  const h = Math.max(50, Number($('area-h').value) || 1000);
  const dLat = (h / 2) / 111320;
  const dLon = (w / 2) / (111320 * Math.cos(c.lat * Math.PI / 180));
  applyBbox([c.lng - dLon, c.lat - dLat, c.lng + dLon, c.lat + dLat]);
  map.fitBounds(areaRect.getBounds(), { padding: [40, 40] });
};

// --------------------------------------------------------------------------- load area
$('btn-load').onclick = async () => {
  if (!project.bbox_wgs84) { status('Selecteer eerst een gebied.', 'err'); return; }
  readSettings();
  busy(true, 'Gebouwen (3DBAG) en OSM-data ophalen…');
  try {
    const data = await post('/api/area', { project, refresh: false });
    renderArea(data);
    if (data.stats.surfaces_error) {
      status('Gebouwen geladen; water/wegen niet (OSM onbereikbaar).', 'err');
    } else {
      status('Gebied geladen.', 'ok');
    }
  } catch (err) {
    status(err.message, 'err');
  } finally {
    busy(false);
  }
};

function renderArea(data) {
  if (buildingLayer) map.removeLayer(buildingLayer);
  if (contextLayer) map.removeLayer(contextLayer);

  const features = data.geojson.features;
  const context = features.filter((f) => f.properties.kind !== 'building');
  const buildings = features.filter((f) => f.properties.kind === 'building');

  const CONTEXT_STYLE = {
    water: { fillColor: '#2f6f9f', fillOpacity: 0.55 },
    shrubs: { fillColor: '#6f9a52', fillOpacity: 0.40 },
    woods: { fillColor: '#3f6a2e', fillOpacity: 0.45 },
    roads: { fillColor: '#cfc9ba', fillOpacity: 0.45 },
  };
  contextLayer = L.geoJSON({ type: 'FeatureCollection', features: context }, {
    style: (f) => ({ weight: 0, color: '#000', ...(CONTEXT_STYLE[f.properties.kind] || CONTEXT_STYLE.roads) }),
    pointToLayer: (f, latlng) => L.circleMarker(latlng, {
      radius: 2.5, color: '#2f5225', weight: 0, fillColor: '#3f6a2e', fillOpacity: 0.9,
    }),
    interactive: false,
  }).addTo(map);

  const hidden = new Set(project.hidden_building_ids);
  buildingLayer = L.geoJSON({ type: 'FeatureCollection', features: buildings }, {
    style: (f) => buildingStyle(hidden.has(f.properties.id)),
    onEachFeature: (f, layer) => {
      layer.on('click', () => {
        if (hideMode !== 'click') return;
        toggleHidden(f.properties.id);
      });
      const p = f.properties;
      layer.bindTooltip(
        `${p.height_m} m hoog${p.floors ? ` &middot; ${p.floors} lagen` : ''}` +
        `${p.year ? ` &middot; ${p.year}` : ''}`,
        { sticky: true },
      );
    },
  }).addTo(map);

  renderAreaStats(data.stats);
  $('sec-buildings').classList.remove('disabled');
  $('sec-design').classList.remove('disabled');
  updateHiddenCount();
}

function buildingStyle(isHidden) {
  return isHidden
    ? { color: '#d9604a', weight: 1, fillColor: '#d9604a', fillOpacity: 0.12, dashArray: '3 2' }
    : { color: '#7a4327', weight: 0.6, fillColor: '#b7643c', fillOpacity: 0.75 };
}

function renderAreaStats(s) {
  $('area-stats').classList.remove('hidden');
  $('area-stats').innerHTML = `
    <dl>
      <dt>Gebied</dt><dd>${s.width_m} &times; ${s.height_m} m</dd>
      <dt>Plaat</dt><dd>${s.plate_mm[0]} &times; ${s.plate_mm[1]} mm</dd>
      <dt>Schaal</dt><dd>${s.scale_ratio}</dd>
      <dt>Gebouwen</dt><dd>${s.buildings}</dd>
      <dt>Wegen</dt><dd>${s.roads}</dd>
      <dt>Bomen</dt><dd>${s.trees}</dd>
    </dl>
    ${s.warn_large ? '<span class="warn">Groot gebied: ophalen en genereren duurt even.</span>' : ''}
    ${s.surfaces_error ? `<span class="warn">${s.surfaces_error} De gebouwen zijn er wel. Klik nogmaals "Kaartdata ophalen" om water en wegen alsnog op te halen.</span>` : ''}`;
}

// --------------------------------------------------------------------------- hiding buildings
function toggleHidden(id) {
  const idx = project.hidden_building_ids.indexOf(id);
  const nowHidden = idx === -1;
  if (nowHidden) project.hidden_building_ids.push(id);
  else project.hidden_building_ids.splice(idx, 1);
  // A pand split into parts arrives as several features sharing one BAG id,
  // so restyle every layer with that id, not just the one that was clicked.
  restyle(new Set([id]), nowHidden);
  save();
  updateHiddenCount();
  ensureDesignMarker();
}

function restyle(ids, isHidden) {
  if (!buildingLayer) return;
  buildingLayer.eachLayer((l) => {
    if (ids.has(l.feature.properties.id)) l.setStyle(buildingStyle(isHidden));
  });
}

function hideInBounds(bounds, hide) {
  if (!buildingLayer) return;
  const touched = new Set();
  buildingLayer.eachLayer((layer) => {
    if (bounds.intersects(layer.getBounds())) touched.add(layer.feature.properties.id);
  });
  if (!touched.size) return;

  const set = new Set(project.hidden_building_ids);
  touched.forEach((id) => (hide ? set.add(id) : set.delete(id)));
  project.hidden_building_ids = [...set];
  restyle(touched, hide);
  save();
  updateHiddenCount();
  ensureDesignMarker();
}

function updateHiddenCount() {
  const n = project.hidden_building_ids.length;
  $('hidden-count').innerHTML = `<dl><dt>Verborgen</dt><dd>${n} gebouw${n === 1 ? '' : 'en'}</dd></dl>`;
}

$('btn-mode-click').onclick = () => {
  hideMode = 'click'; setMode(null);
  $('btn-mode-click').classList.add('active');
  $('btn-mode-box').classList.remove('active');
};
$('btn-mode-box').onclick = () => {
  hideMode = 'box'; setMode('box');
  $('btn-mode-box').classList.add('active');
  $('btn-mode-click').classList.remove('active');
  status('Sleep een vak om te verbergen. Shift + slepen maakt weer zichtbaar.');
};
$('btn-clear-hidden').onclick = () => {
  project.hidden_building_ids = [];
  save();
  if (buildingLayer) buildingLayer.eachLayer((l) => l.setStyle(buildingStyle(false)));
  updateHiddenCount();
};

// --------------------------------------------------------------------------- own design
$('design-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('project_id', project.id);
  form.append('file', file);

  busy(true, 'Ontwerp inlezen…');
  try {
    const info = await api('/api/design/upload', { method: 'POST', body: form });
    project.design.filename = info.filename;
    project.design.enabled = true;
    if (!project.design.anchor_lonlat) project.design.anchor_lonlat = defaultAnchor();
    save();
    $('design-info').classList.remove('hidden');
    $('design-info').innerHTML = `<dl>
      <dt>Bestand</dt><dd>${info.name}</dd>
      <dt>Afmeting</dt><dd>${info.size_m[0]} &times; ${info.size_m[1]} &times; ${info.size_m[2]} m</dd>
      <dt>Driehoeken</dt><dd>${info.triangles.toLocaleString('nl-NL')}</dd>
    </dl>`;
    $('design-controls').classList.remove('hidden');
    fillSlotSelect($('design-slot'), project.design.slot);
    ensureDesignMarker();
    renderSlots();
    status('Ontwerp geladen.', 'ok');
  } catch (err) {
    status(err.message, 'err');
  } finally {
    busy(false);
  }
});

function defaultAnchor() {
  if (buildingLayer && project.hidden_building_ids.length) {
    const hidden = new Set(project.hidden_building_ids);
    let bounds = null;
    buildingLayer.eachLayer((l) => {
      if (!hidden.has(l.feature.properties.id)) return;
      bounds = bounds ? bounds.extend(l.getBounds()) : L.latLngBounds(l.getBounds().getSouthWest(), l.getBounds().getNorthEast());
    });
    if (bounds) return [bounds.getCenter().lng, bounds.getCenter().lat];
  }
  if (project.bbox_wgs84) {
    const [w, s, e, n] = project.bbox_wgs84;
    return [(w + e) / 2, (s + n) / 2];
  }
  const c = map.getCenter();
  return [c.lng, c.lat];
}

function ensureDesignMarker() {
  if (!project.design.filename) return;
  if (!project.design.anchor_lonlat) project.design.anchor_lonlat = defaultAnchor();
  const [lon, lat] = project.design.anchor_lonlat;

  if (!designMarker) {
    designMarker = L.circleMarker([lat, lon], {
      radius: 9, color: '#7a6300', weight: 2, fillColor: '#d9b310', fillOpacity: 0.95,
    }).addTo(map).bindTooltip('Eigen ontwerp - sleep om te verplaatsen');

    // circleMarker has no dragging, so move it by hand.
    designMarker.on('mousedown', () => {
      map.dragging.disable();
      const move = (ev) => designMarker.setLatLng(ev.latlng);
      const up = (ev) => {
        map.off('mousemove', move); map.off('mouseup', up);
        map.dragging.enable();
        project.design.anchor_lonlat = [ev.latlng.lng, ev.latlng.lat];
        save();
      };
      map.on('mousemove', move); map.on('mouseup', up);
    });
  } else {
    designMarker.setLatLng([lat, lon]);
  }
}

['design-rot', 'design-scale', 'design-z', 'design-slot'].forEach((id) => {
  $(id).addEventListener('change', () => {
    project.design.rotation_deg = Number($('design-rot').value) || 0;
    project.design.relative_scale = Number($('design-scale').value) || 1;
    project.design.z_offset_mm = Number($('design-z').value) || 0;
    project.design.slot = Number($('design-slot').value) || 4;
    project.settings.slots.design = project.design.slot;
    save();
    renderSlots();
  });
});

$('btn-design-remove').onclick = async () => {
  if (project.design.filename) {
    await api(`/api/design?project_id=${encodeURIComponent(project.id)}&filename=${encodeURIComponent(project.design.filename)}`,
      { method: 'DELETE' }).catch(() => {});
  }
  project.design = DEFAULT_PROJECT().design;
  save();
  if (designMarker) { map.removeLayer(designMarker); designMarker = null; }
  $('design-info').classList.add('hidden');
  $('design-controls').classList.add('hidden');
  $('design-file').value = '';
  renderSlots();
};

// --------------------------------------------------------------------------- settings
function readSettings() {
  const s = project.settings;
  s.max_size_mm = Number($('set-size').value) || 200;
  s.layer_height = Number($('set-layer').value) || 0.2;
  s.first_layer_height = s.layer_height;
  s.base_top = Number($('set-base').value) || 1.2;
  s.road_top = Number($('set-road').value) || 1.8;
  s.land_top = Number($('set-land').value) || 2.0;
  s.height_exaggeration = Number($('set-exag').value) || 1;
  s.min_road_width_mm = Number($('set-roadw').value) || 0.9;
  s.road_style = $('set-roadstyle').value;
  s.include_water = $('set-water').checked;
  s.include_roads = $('set-roads').checked;
  s.include_buildings = $('set-buildings').checked;
  s.include_shrubs = $('set-shrubs').checked;
  s.include_trees = $('set-trees').checked;
  s.detailed_buildings = $('set-detailed').checked;
  save();
  renderSlots();
}

function writeSettings() {
  const s = project.settings;
  $('set-size').value = s.max_size_mm;
  $('set-layer').value = s.layer_height;
  $('set-base').value = s.base_top;
  $('set-road').value = s.road_top;
  $('set-land').value = s.land_top;
  $('set-exag').value = s.height_exaggeration;
  $('set-roadw').value = s.min_road_width_mm;
  $('set-roadstyle').value = s.road_style;
  $('set-water').checked = s.include_water;
  $('set-roads').checked = s.include_roads;
  $('set-buildings').checked = s.include_buildings;
  $('set-shrubs').checked = s.include_shrubs;
  $('set-trees').checked = s.include_trees;
  $('set-detailed').checked = s.detailed_buildings;
  $('design-rot').value = project.design.rotation_deg;
  $('design-scale').value = project.design.relative_scale;
  $('design-z').value = project.design.z_offset_mm;
  fillSlotSelect($('design-slot'), project.design.slot);
  renderSlots();
}

document.querySelectorAll('#panel input, #panel select').forEach((el) => {
  if (el.id.startsWith('set-')) el.addEventListener('change', readSettings);
});

// --------------------------------------------------------------------------- CFS slots
const MAX_SLOTS = 8;

function fillSlotSelect(sel, value) {
  sel.innerHTML = '';
  for (let i = 1; i <= MAX_SLOTS; i++) {
    const o = document.createElement('option');
    o.value = i; o.textContent = i;
    sel.appendChild(o);
  }
  sel.value = value || 1;
}

// Which bodies are actually in play, so the >4 warning only counts real ones.
function activeBodyKeys() {
  const s = project.settings;
  const keys = [];
  if (s.include_water) keys.push('base');
  if (s.include_roads) keys.push('roads');
  keys.push('land');
  if (s.include_shrubs) keys.push('shrubs');
  if (s.include_trees) keys.push('trees');
  if (s.include_buildings) keys.push('buildings');
  if (project.design.filename && project.design.enabled) keys.push('design');
  return keys;
}

function slotFor(key) {
  const def = BODY_DEFS.find((b) => b.key === key);
  const override = project.settings.slots[key];
  return override || (def ? def.slot : 4);
}

function renderSlots() {
  const active = new Set(activeBodyKeys());
  const rows = BODY_DEFS.filter((b) => active.has(b.key)).map((b) => {
    const slot = slotFor(b.key);
    const opts = Array.from({ length: MAX_SLOTS }, (_, i) =>
      `<option value="${i + 1}" ${slot === i + 1 ? 'selected' : ''}>${i + 1}</option>`).join('');
    return `<div class="slotrow">
      <span class="swatch" style="background:${b.color}"></span>
      <span class="name">${b.label}</span>
      <select data-key="${b.key}">${opts}</select>
    </div>`;
  }).join('');
  $('slots').innerHTML = rows;

  $('slots').querySelectorAll('select').forEach((sel) => {
    sel.onchange = () => {
      const key = sel.dataset.key;
      project.settings.slots[key] = Number(sel.value);
      if (key === 'design') project.design.slot = Number(sel.value);
      save();
      updateSlotWarning();
    };
  });
  updateSlotWarning();
}

function updateSlotWarning() {
  const active = activeBodyKeys();
  const used = new Set(active.map(slotFor));
  const warn = $('slot-warn');
  if (used.size > 4) {
    warn.textContent = `Je gebruikt ${used.size} sloten; de CFS-C heeft er 4. `
      + `Dat kan met een handmatige filamentwissel, of geef lagen hetzelfde slot.`;
    warn.classList.add('show');
  } else {
    warn.classList.remove('show');
  }
}

// --------------------------------------------------------------------------- export folder
async function refreshExportDir() {
  try {
    const { path } = await api('/api/export-dir');
    $('export-dir').value = path;
  } catch { /* server not ready yet */ }
}

$('btn-pick-dir').onclick = async () => {
  busy(true, 'Wachten op de mapkeuze…');
  try {
    const res = await api('/api/export-dir/pick', { method: 'POST' });
    $('export-dir').value = res.path;
    if (res.changed) status('Exportmap ingesteld.', 'ok');
  } catch (err) {
    status(err.message, 'err');
  } finally {
    busy(false);
  }
};

$('btn-reveal').onclick = () => api('/api/reveal', { method: 'POST' }).catch(() => {});

// --------------------------------------------------------------------------- build
async function build(chooseDir) {
  if (!project.bbox_wgs84) { status('Selecteer eerst een gebied.', 'err'); return; }
  readSettings();
  busy(true, chooseDir ? 'Wachten op de mapkeuze…' : 'Model genereren… dit kan even duren.');
  try {
    const res = await post('/api/build', { project, choose_dir: chooseDir });
    renderBuild(res);
    $('export-dir').value = res.output_dir.replace(/\\[^\\]+$/, '');
    $('btn-reveal').classList.remove('hidden');
    status('Klaar. Opgeslagen in ' + res.output_dir, 'ok');
  } catch (err) {
    // 409 is the user cancelling the folder dialog, which is not an error.
    if (err.message === 'Geannuleerd.') status('Opslaan geannuleerd.');
    else status(err.message, 'err');
  } finally {
    busy(false);
  }
}

$('btn-build').onclick = () => build(false);
$('btn-build-as').onclick = () => build(true);

function renderBuild(res) {
  const el = $('build-result');
  el.classList.remove('hidden');
  const bands = res.bands.map((b) => `
    <div class="band">
      <span class="swatch" style="background:${b.color}"></span>
      <span>${b.label}</span>
      <span class="z">slot ${b.slot} &middot; ${b.z0.toFixed(1)}&ndash;${b.z1 === null ? 'top' : b.z1.toFixed(1)} mm</span>
    </div>`).join('');

  const files = res.files.map((f) => `
    <li><a href="/api/download/${res.project_id}/${encodeURIComponent(f.name)}" download>
      <span>${f.name}</span><span>${f.size_kb} kB</span></a></li>`).join('');

  el.innerHTML = `
    <div class="card"><dl>
      <dt>Plaat</dt><dd>${res.stats.plate_mm[0]} &times; ${res.stats.plate_mm[1]} mm</dd>
      <dt>Hoogte</dt><dd>${res.stats.total_height_mm} mm</dd>
      <dt>Schaal</dt><dd>${res.stats.scale_ratio}</dd>
      <dt>Driehoeken</dt><dd>${res.stats.triangles.toLocaleString('nl-NL')}</dd>
      <dt>Filamentwissels</dt><dd>${res.stats.filament_changes}</dd>
    </dl></div>
    <div class="bands">${bands}</div>
    <ul class="files">${files}</ul>`;
}

// --------------------------------------------------------------------------- topbar
function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute('data-theme', theme);
  else document.documentElement.removeAttribute('data-theme');
  localStorage.setItem('oststl.theme', theme || '');
}
$('btn-theme').onclick = () => {
  const now = document.documentElement.getAttribute('data-theme');
  applyTheme(now === 'dark' ? 'light' : 'dark');
};
applyTheme(localStorage.getItem('oststl.theme') || '');

let appInfo = null;
async function loadAppInfo() {
  try {
    appInfo = await api('/api/appinfo');
    $('app-version').textContent = 'v' + appInfo.version;
    $('btn-feedback').onclick = () => window.open(appInfo.feedback_url, '_blank', 'noopener');
  } catch { /* server not ready */ }
}

async function checkUpdate() {
  try {
    const u = await api('/api/update-check');
    if (u.update_available) {
      const pill = $('update-pill');
      pill.href = u.url;
      pill.textContent = `⬆ Update ${u.latest}`;
      pill.classList.add('show');
    }
  } catch { /* offline or no releases */ }
}

// --------------------------------------------------------------------------- boot
writeSettings();
updateHiddenCount();
renderSlots();
refreshExportDir();
loadAppInfo();
checkUpdate();
if (project.bbox_wgs84) {
  drawAreaRect();
  map.fitBounds(areaRect.getBounds(), { padding: [40, 40] });
}

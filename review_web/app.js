const $ = (id) => document.getElementById(id);
let state = null;
let selected = new Set();
let customPresets = [];
let dragStart = null;

const svgNS = 'http://www.w3.org/2000/svg';
const editableTags = new Set(['path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text']);

function message(value, error = false) {
  $('notice').textContent = value;
  $('notice').classList.toggle('error', error);
}

async function request(path, payload = undefined) {
  const options = payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  };
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); }
  catch { throw new Error(`Request failed: ${response.status}`); }
  if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
  return body;
}

async function command(path, payload = {}) {
  try {
    const result = await request(path, payload);
    if (result.svg) { state = result; draw(); }
    message('Updated');
    return result;
  } catch (error) { message(error.message, true); return null; }
}

function parseSvg(text) {
  const parsed = new DOMParser().parseFromString(text, 'image/svg+xml');
  if (parsed.querySelector('parsererror')) throw new Error('The SVG could not be displayed');
  return document.importNode(parsed.documentElement, true);
}

function editableElements(svg) {
  return [...svg.querySelectorAll('[id]')].filter(el => editableTags.has(el.localName) && state.elements.some(item => item.id === el.id));
}

function item(id) { return state.elements.find(value => value.id === id); }

function colorFor(id) {
  let hash = 0;
  for (const character of id) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return `hsl(${hash % 360} 79% 49%)`;
}

function mount(host, svg) {
  host.replaceChildren(svg);
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.style.width = '100%';
  svg.style.height = '100%';
}

function selectedClone({structure = false, crop = false, ids = selected} = {}) {
  const svg = parseSvg(state.svg);
  const hasSelection = ids.size > 0;
  for (const el of editableElements(svg)) {
    const meta = item(el.id);
    if (structure) {
      if (meta.locked) { el.style.display = 'none'; continue; }
      el.style.setProperty('fill', 'rgba(0,0,0,0.015)', 'important');
      el.style.setProperty('stroke', ids.has(el.id) ? '#ff9f1c' : colorFor(el.id), 'important');
      el.style.setProperty('stroke-width', ids.has(el.id) ? '3' : '1.5', 'important');
      el.setAttribute('vector-effect', 'non-scaling-stroke');
      el.setAttribute('pointer-events', 'all');
      el.style.opacity = hasSelection && !ids.has(el.id) ? '0.31' : '0.9';
      el.dataset.pickId = el.id;
    } else if (!ids.has(el.id)) {
      el.style.visibility = 'hidden';
      el.style.pointerEvents = 'none';
    } else {
      el.style.setProperty('fill', 'rgba(255,159,28,0.24)', 'important');
      el.style.setProperty('stroke', '#ff9f1c', 'important');
      el.style.setProperty('stroke-width', '2.5', 'important');
      el.setAttribute('vector-effect', 'non-scaling-stroke');
      el.style.opacity = '0.9';
    }
  }
  if (!structure) {
    for (const el of svg.querySelectorAll('image, use')) el.style.visibility = 'hidden';
  }
  if (crop && ids.size) {
    const boxes = [...ids].map(id => item(id)?.bbox).filter(Boolean);
    if (boxes.length) {
      const x0 = Math.min(...boxes.map(box => box[0]));
      const y0 = Math.min(...boxes.map(box => box[1]));
      const x1 = Math.max(...boxes.map(box => box[2]));
      const y1 = Math.max(...boxes.map(box => box[3]));
      const padding = Math.max(x1 - x0, y1 - y0, 8) * 0.15;
      svg.setAttribute('viewBox', `${x0-padding} ${y0-padding} ${x1-x0+padding*2} ${y1-y0+padding*2}`);
    }
  }
  return svg;
}

function putOverlay(hostId, crop = false) {
  const host = $(hostId);
  if (!selected.size) { host.replaceChildren(); return; }
  mount(host, selectedClone({crop}));
}

function selectionGroup(id) {
  const kind = item(id)?.type;
  if (kind === 'text') return [id];
  const maps = state.arrow_shield_map;
  if (maps.some(entry => (entry.arrow_path_ids || []).includes(id))) {
    return [...new Set(maps.flatMap(entry => [...(entry.arrow_path_ids || []), ...(entry.shield_path_ids || [])]))];
  }
  const preset = state.shield_groups.find(group => (group.path_ids || []).includes(id));
  if (preset) return preset.path_ids;
  const mapped = maps.find(entry => (entry.shield_path_ids || []).includes(id));
  return mapped ? mapped.shield_path_ids : [id];
}

function toggleSelection(id) {
  const group = selectionGroup(id).filter(value => !item(value)?.locked);
  const allSelected = group.every(value => selected.has(value));
  for (const value of group) allSelected ? selected.delete(value) : selected.add(value);
  drawSelection();
}

function pointInside(el, event) {
  try {
    const matrix = el.getScreenCTM();
    if (!matrix) return false;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    const geometry = typeof el.isPointInFill === 'function';
    if (geometry && el.isPointInFill(point)) return true;
    if (typeof el.isPointInStroke === 'function' && el.isPointInStroke(point)) return true;
    if (geometry && el.localName !== 'text') return false;
    const box = el.getBBox();
    return point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
  } catch { return false; }
}

function candidatesAt(svg, event) {
  return editableElements(svg).filter(el => !item(el.id)?.locked && pointInside(el, event)).reverse();
}

function showCandidates(candidates, event) {
  const menu = $('candidate-menu');
  menu.replaceChildren();
  for (const el of candidates) {
    const button = document.createElement('button');
    const meta = item(el.id);
    const text = meta.type === 'text' ? ` · ${meta.text || ''}` : '';
    const thumb = selectedClone({crop:true, ids:new Set([el.id])});
    const label = document.createElement('span');
    label.textContent = `${meta.type}${text} · ${el.id}`;
    button.append(thumb, label);
    button.onclick = () => { toggleSelection(el.id); menu.classList.add('hidden'); };
    menu.append(button);
  }
  menu.style.left = `${Math.min(event.clientX, window.innerWidth - 180)}px`;
  menu.style.top = `${Math.min(event.clientY, window.innerHeight - 240)}px`;
  menu.classList.remove('hidden');
}

function handleSvgClick(event) {
  const svg = event.currentTarget;
  const candidates = candidatesAt(svg, event);
  if (candidates.length === 1) toggleSelection(candidates[0].id);
  else if (candidates.length > 1) showCandidates(candidates, event);
}

function drawPresets() {
  const host = $('presets');
  host.replaceChildren();
  const allArrows = [...new Set(state.arrow_shield_map.flatMap(entry => [...(entry.arrow_path_ids || []), ...(entry.shield_path_ids || [])]))];
  const presets = [
    {group_id:'All arrows + shields', path_ids:allArrows},
    ...state.shield_groups, ...state.arrow_groups, ...customPresets
  ];
  for (const group of presets) {
    if (!group.path_ids?.length) continue;
    const button = document.createElement('button');
    button.textContent = group.group_id;
    button.onclick = () => {
      selected = new Set(group.path_ids.filter(id => item(id) && !item(id).locked));
      drawSelection();
    };
    host.append(button);
  }
}

function drawSelection() {
  if (!state) return;
  selected = new Set([...selected].filter(id => item(id) && !item(id).locked));
  putOverlay('reference-projection');
  putOverlay('normal-highlight');
  const preview = $('selected-preview');
  if (selected.size) mount(preview, selectedClone({crop:true}));
  else preview.replaceChildren(Object.assign(document.createElement('span'), {className:'muted', textContent:'Selected elements appear here'}));
  $('selected-title').textContent = selected.size ? `${selected.size} selected` : 'Nothing selected';
  $('selected-list').replaceChildren(...[...selected].map(id => {
    const span = document.createElement('span');
    span.textContent = id;
    return span;
  }));
  const structure = $('structure-svg').querySelector('svg');
  if (structure) {
    for (const el of editableElements(structure)) {
      if (item(el.id)?.locked) continue;
      el.style.setProperty('stroke', selected.has(el.id) ? '#ff9f1c' : colorFor(el.id), 'important');
      el.style.setProperty('stroke-width', selected.has(el.id) ? '3' : '1.5', 'important');
      el.style.opacity = selected.size && !selected.has(el.id) ? '0.31' : '0.9';
    }
  }
  const only = selected.size === 1 ? item([...selected][0]) : null;
  $('text-controls').classList.toggle('hidden', only?.type !== 'text');
  if (only?.type === 'text') {
    $('text-content').value = only.text || '';
    $('font-size').value = parseFloat(only.font_size) || '';
    $('text-width').value = only.text_length || '';
    $('font-family').value = only.font_family || '';
    for (const control of ['text-content', 'font-size', 'text-width', 'font-family', 'apply-text']) {
      $(control).disabled = !only.text_editable || state.finalized;
    }
  }
  for (const button of document.querySelectorAll('[data-layer]')) button.disabled = selected.size !== 1 || state.finalized;
  for (const button of document.querySelectorAll('[data-move]')) button.disabled = state.finalized;
  const warnings = $('warnings');
  warnings.replaceChildren();
  const problematic = state.elements.filter(value => !value.locked && (value.out_of_bounds || (value.touches_boundary && selected.has(value.id))));
  for (const value of problematic.slice(0, 8)) {
    const line = document.createElement('div');
    line.textContent = `${value.out_of_bounds ? 'Outside canvas' : 'Touches canvas edge'}: ${value.id}`;
    warnings.append(line);
  }
}

function drawResults() {
  const current = state.prepared && state.prepared_directions.length;
  $('results').classList.toggle('hidden', !current);
  $('finalize').disabled = !current || state.finalized;
  if (!current) return;
  $('official-render').src = `/api/prepared/edited.png?r=${state.revision}`;
  const host = $('direction-grid');
  host.replaceChildren();
  for (const direction of state.prepared_directions) {
    const card = document.createElement('article');
    card.className = 'direction-card';
    const title = document.createElement('h3');
    title.textContent = direction;
    const pair = document.createElement('div');
    pair.className = 'direction-previews';
    const isolated = document.createElement('div');
    isolated.className = 'direction-preview';
    const overlay = document.createElement('div');
    overlay.className = 'direction-preview overlay';
    const url = `/api/prepared/directions/${encodeURIComponent(direction)}?r=${state.revision}`;
    const arrowImage = document.createElement('img'); arrowImage.src = url; arrowImage.alt = `Isolated ${direction}`;
    const signImage = document.createElement('img'); signImage.src = `/api/prepared/edited.png?r=${state.revision}`; signImage.alt = `Edited sign with ${direction}`;
    const overlayImage = document.createElement('img'); overlayImage.src = url; overlayImage.alt = '';
    isolated.append(arrowImage); overlay.append(signImage, overlayImage); pair.append(isolated, overlay);
    card.append(title, pair); host.append(card);
  }
}

function layoutFrames() {
  if (!state) return;
  const ratio = state.canvas[2] / state.canvas[3];
  for (const panel of document.querySelectorAll('.view-panel')) {
    const frame = panel.querySelector('.canvas-frame');
    const heading = panel.querySelector('.view-title');
    const availableWidth = Math.max(panel.clientWidth - 4, 1);
    const availableHeight = Math.max(panel.clientHeight - heading.offsetHeight - 8, 1);
    const width = Math.min(availableWidth, availableHeight * ratio);
    frame.style.width = `${width}px`;
    frame.style.height = `${width / ratio}px`;
  }
}

function draw() {
  $('task-label').textContent = state.task_id;
  $('revision-label').textContent = `Revision ${state.revision}${state.prepared ? ' · outputs current' : ''}`;
  if (state.finalized) $('revision-label').textContent += ' · finalized';
  const [x, y, width, height] = state.canvas;
  for (const frame of document.querySelectorAll('.canvas-frame')) frame.style.aspectRatio = `${width} / ${height}`;
  layoutFrames();
  mount($('normal-svg'), parseSvg(state.svg));
  mount($('structure-svg'), selectedClone({structure:true}));
  $('normal-svg').querySelector('svg').addEventListener('click', handleSvgClick);
  $('structure-svg').querySelector('svg').addEventListener('click', handleSvgClick);
  $('undo').disabled = !state.can_undo || state.finalized;
  $('redo').disabled = !state.can_redo || state.finalized;
  $('prepare').disabled = !state.punchout_available || state.finalized;
  $('font-options').replaceChildren(...state.font_options.map(name => {
    const option = document.createElement('option'); option.value = name; return option;
  }));
  drawPresets(); drawSelection(); drawResults();
}

async function move(dx, dy) {
  if (state.finalized) return message('This review task is finalized', true);
  if (!selected.size) return message('Select an element first', true);
  const axis = $('axis').value;
  if (axis === 'x') dy = 0;
  if (axis === 'y') dx = 0;
  if (dx === 0 && dy === 0) return;
  await command('/api/action', {type:'move', ids:[...selected], dx, dy, revision:state.revision});
}

function moveDirection(direction) {
  const step = Number($('step').value);
  if (!Number.isFinite(step) || step <= 0) return message('Move step must be positive', true);
  const deltas = {up:[0,-step], down:[0,step], left:[-step,0], right:[step,0]};
  move(...deltas[direction]);
}

function installEvents() {
  $('clear-selection').onclick = () => { selected.clear(); drawSelection(); };
  for (const button of document.querySelectorAll('[data-move]')) button.onclick = () => moveDirection(button.dataset.move);
  for (const button of document.querySelectorAll('[data-layer]')) button.onclick = () => {
    if (selected.size === 1) command('/api/action', {type:'layer', ids:[...selected], direction:button.dataset.layer, revision:state.revision});
  };
  $('apply-text').onclick = () => {
    if (selected.size !== 1) return;
    const current = item([...selected][0]);
    const changes = {content:$('text-content').value};
    if ($('font-size').value) changes.font_size = $('font-size').value;
    if ($('font-family').value) changes.font_family = $('font-family').value;
    if ($('text-width').value || current.text_length) changes.text_length = $('text-width').value || null;
    command('/api/action', {type:'text', ids:[...selected], changes, revision:state.revision});
  };
  $('undo').onclick = () => command('/api/undo');
  $('redo').onclick = () => command('/api/redo');
  $('save-draft').onclick = async () => {
    const result = await command('/api/save-draft');
    if (result) message(`Draft saved: ${result.draft_svg}`);
  };
  $('prepare').onclick = async () => {
    const result = await command('/api/prepare');
    if (result) message('Direction outputs are ready below. Inspect before final confirmation.');
  };
  $('finalize').onclick = async () => {
    const result = await command('/api/finalize');
    if (result) {
      state = await request('/api/state'); draw();
      message(`Final outputs saved in ${result.output_dir}`);
    }
  };
  $('save-preset').onclick = () => {
    if (!selected.size) return message('Select elements first', true);
    customPresets.push({group_id:`Selection ${customPresets.length + 1}`, path_ids:[...selected]});
    drawPresets(); message('Saved this selection for the current editing session');
  };
  document.addEventListener('click', event => {
    if (!$('candidate-menu').contains(event.target)) $('candidate-menu').classList.add('hidden');
  });
  document.addEventListener('keydown', event => {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
    if (event.ctrlKey && event.key.toLowerCase() === 'z') { event.preventDefault(); command('/api/undo'); return; }
    if (event.ctrlKey && event.key.toLowerCase() === 'y') { event.preventDefault(); command('/api/redo'); return; }
    const direction = {ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right'}[event.key];
    if (direction) { event.preventDefault(); moveDirection(direction); }
  });
  const reference = $('reference-frame');
  reference.addEventListener('pointerdown', event => {
    if (!selected.size) return;
    dragStart = {x:event.clientX, y:event.clientY};
    reference.setPointerCapture(event.pointerId);
  });
  reference.addEventListener('pointermove', event => {
    if (!dragStart) return;
    $('reference-projection').style.transform = `translate(${event.clientX-dragStart.x}px,${event.clientY-dragStart.y}px)`;
  });
  reference.addEventListener('pointerup', event => {
    if (!dragStart) return;
    const rect = reference.getBoundingClientRect();
    const dx = (event.clientX-dragStart.x) * state.canvas[2] / rect.width;
    const dy = (event.clientY-dragStart.y) * state.canvas[3] / rect.height;
    dragStart = null;
    $('reference-projection').style.transform = '';
    if (Math.abs(dx) + Math.abs(dy) > 0.05) move(Number(dx.toFixed(3)), Number(dy.toFixed(3)));
  });
  reference.addEventListener('pointercancel', () => { dragStart = null; $('reference-projection').style.transform = ''; });
  window.addEventListener('resize', layoutFrames);
}

installEvents();
request('/api/state').then(value => { state = value; draw(); message('Ready'); }).catch(error => message(error.message, true));

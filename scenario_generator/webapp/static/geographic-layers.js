// Geographic layers and vehicle routing share the existing View and editing controls.

const routeFields = {
  start_time_s: ["start-time", 1], start_speed_mps: ["start-speed", 3.6],
  end_speed_mps: ["end-speed", 3.6], reference_speed_mps: ["reference-speed", 3.6],
  acceleration_mps2: ["acceleration", 1], deceleration_mps2: ["deceleration", 1],
};

function cancelRouteQueue() { state.routeEpoch = (state.routeEpoch || 0) + 1; }

function renderGeodata() {
  const geo = state.scenario.geodata, settings = state.scenario.settings;
  for (const key of ["show_lanelet", "show_xodr_origin", "show_lanelet_origin", "show_orthophoto"])
    $("#" + key.replaceAll("_", "-")).checked = settings[key];
  for (const key of ["orthophoto_opacity", "map_opacity", "lanelet_opacity"])
    $("#" + key.replaceAll("_", "-")).value = settings[key];
  $("#lanelet-remove").disabled = !geo.lanelet_path;
  $("#show-lanelet").disabled = !geo.lanelet_path;
  $("#show-lanelet-origin").disabled = !geo.lanelet_path;
  $("#show-xodr-origin").disabled = !state.scenario.map.roads.length;
  $("#show-orthophoto").disabled = !geo.frame;
  $("#show-orthophoto").title = geo.frame ? "" : "Set a map georeference to load orthophotos";
  $("#georeference-summary").textContent = geo.warning || (geo.frame
    ? `${geo.frame.crs}; working origin ${geo.frame.origin_lat.toFixed(7)}, ${geo.frame.origin_lon.toFixed(7)}; local XODR origin X=0, Y=0.`
    : "Local map without geographic reference; XODR origin X=0, Y=0.");
  $("#lanelet-origin-summary").textContent = geo.lanelet_origin
    ? `${geo.lanelet_count} lanelets. Projection origin near the first node: ${geo.lanelet_origin.lat.toFixed(7)}, ${geo.lanelet_origin.lon.toFixed(7)}. This is the origin used for import, not a stored OSM origin.`
    : "Load a Lanelet2 map using Load map.";
  const frameChanged = state.frameRevision !== geo.revision;
  if (frameChanged) {
    state.frameRevision = geo.revision;
    $("#frame-crs").value = geo.frame?.crs || "";
    for (const key of ["x", "y", "heading"]) $("#frame-" + key).value = geo.frame?.[key] || 0;
    for (const tile of state.imageTiles?.values() || []) tile.image?.close();
    state.imageAbort?.abort(); state.imageAbort = new AbortController();
    state.imageTiles = new Map();
    state.laneletBounds = null;
    for (const lane of state.lanelet || []) for (const [x, y] of lane.points) {
      state.laneletBounds ||= [x, y, x, y];
      const b = state.laneletBounds; b[0] = Math.min(b[0], x); b[1] = Math.min(b[1], y); b[2] = Math.max(b[2], x); b[3] = Math.max(b[3], y);
    }
  }
  const actor = selectedActor(), vehicle = actor?.dimensions.actor_type === "vehicle";
  if (frameChanged && !actor?.trajectory_options?.routing_settings && !actor?.trajectory_options?.generation)
    $("#route-source").value = geo.lanelet_path ? "lanelet2" : "xodr";
  const context = `${actor?.name}|${geo.revision}|${settings.map_mode}`;
  if (state.routeContext !== context) { state.routeContext = context; cancelRouteQueue(); }
  $("#route-controls").hidden = settings.map_mode || !vehicle;
  $("#route-source option[value=lanelet2]").disabled = !geo.lanelet_path;
  $("#route-source option[value=xodr]").disabled = !state.scenario.map.roads.length;
  const routeSettingsKey = JSON.stringify(actor?.trajectory_options?.routing_settings || actor?.trajectory_options?.generation?.parameters || {});
  if (state.routeActor !== actor?.name || state.routeSettingsKey !== routeSettingsKey) {
    state.routeActor = actor?.name;
    state.routeSettingsKey = routeSettingsKey;
    const saved = actor?.trajectory_options?.routing_settings || actor?.trajectory_options?.generation?.parameters || {};
    $("#route-source").value = saved.source || actor?.trajectory_options?.generation?.source || (geo.lanelet_path ? "lanelet2" : "xodr");
    for (const [key, [id, divisor]] of Object.entries(routeFields)) {
      const field = $("#route-" + id);
      if (field !== document.activeElement) field.value = saved[key] !== undefined ? saved[key] * divisor : key === "start_time_s" ? 0 : divisor === 3.6 ? 30 : 2;
    }
    state.routeHasPoint = false;
    $("#route-summary").textContent = "";
  }
  const last = actor?.waypoints.at(-1);
  for (const id of ["start-time", "start-speed"]) $("#route-" + id).readOnly = Boolean(last);
  if (last) {
    $("#route-start-time").value = last.time_s;
    $("#route-start-speed").value = (last.speed_mps ?? actor.curve.speed_mps.at(-1) ?? 0) * 3.6;
  } else if (state.routeHasPoint) {
    const saved = actor?.trajectory_options?.routing_settings;
    $("#route-start-time").value = saved?.start_time_s ?? 0;
    $("#route-start-speed").value = (saved?.start_speed_mps ?? 30 / 3.6) * 3.6;
  }
  state.routeHasPoint = Boolean(last);
  const available = vehicle && !settings.map_mode && Boolean($("#route-source").value === "lanelet2"
    ? geo.lanelet_path : state.scenario.map.roads.length);
  $("#route-mode").disabled = !available;
  $("#route-mode").title = available ? "Append a route to the last waypoint" : "Choose an available map in Routing settings";
  if (!available) state.routeMode = false;
  $("#route-mode").checked = Boolean(state.routeMode);
  $("#waypoint-timing").disabled = Boolean(state.routeMode);
  $("#lane-snap").disabled = settings.map_mode || Boolean(state.routeMode);
  if (state.routeMode || $("#routing-settings").open) updateRouteLanes();
}

for (const key of ["show_lanelet", "show_xodr_origin", "show_lanelet_origin", "show_orthophoto"])
  $("#" + key.replaceAll("_", "-")).onchange = (event) => updateSettings({ [key]: event.target.checked }, event.target);
for (const key of ["orthophoto_opacity", "map_opacity", "lanelet_opacity"]) {
  const slider = $("#" + key.replaceAll("_", "-"));
  slider.oninput = () => { state.scenario.settings[key] = Number(slider.value); draw(); };
  slider.onchange = () => updateSettings({ [key]: Number(slider.value) }, slider);
}
$("#lanelet-upload").onchange = async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const data = new FormData(); data.append("file", file);
  try { setStatus("Loading Lanelet2...", true); await api("/api/lanelet", { method: "POST", body: data }); state.camera = null; await refresh(); setStatus("Lanelet2 loaded"); }
  catch (error) { setStatus(error.message); }
  finally { event.target.value = ""; }
};
$("#lanelet-remove").onclick = async () => {
  try { await api("/api/lanelet", { method: "DELETE" }); await refresh(); renderMapLoadOptions(); setStatus("Lanelet2 removed"); }
  catch (error) { setStatus(error.message); }
};
$("#frame-apply").onclick = async () => {
  const frame = { ...state.scenario.geodata.frame, crs: $("#frame-crs").value };
  for (const key of ["x", "y", "heading"]) frame[key] = Number($("#frame-" + key).value);
  try { await api("/api/georeference", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(frame) }); state.camera = null; await refresh(); }
  catch (error) { setStatus(error.message); }
};

function drawGeographicLayers(context, view, transform) {
  const settings = state.scenario.settings;
  if (settings.show_lanelet) {
    context.save(); context.globalAlpha = settings.lanelet_opacity;
    context.lineWidth = 1; context.strokeStyle = "#13847b";
    for (const lane of visibleLayerItems("lanelet", state.lanelet || [], view)) {
      drawPolyline(context, lane.left, transform); drawPolyline(context, lane.right, transform);
      context.save(); context.setLineDash([4, 4]); context.strokeStyle = "#d36a08";
      drawPolyline(context, lane.centerline, transform); context.restore();
    }
    context.restore();
  }
  const mark = (point, label, opacity) => {
    context.save(); context.globalAlpha = opacity;
    context.strokeStyle = "#bd184b"; context.fillStyle = "#bd184b"; context.lineWidth = 1;
    const [x, y] = transform(point); context.beginPath(); context.moveTo(x - 8, y); context.lineTo(x + 8, y);
    context.moveTo(x, y - 8); context.lineTo(x, y + 8); context.stroke(); drawLabel(context, x, y, label, "#bd184b"); context.restore();
  };
  if (settings.show_map && settings.show_xodr_origin && state.scenario.map.roads.length) mark([0, 0], "XODR origin", settings.map_opacity);
  if (settings.show_lanelet && settings.show_lanelet_origin && state.scenario.geodata.lanelet_origin)
    mark(state.scenario.geodata.lanelet_origin.point, "Lanelet2 projection origin", settings.lanelet_opacity);
}

function drawOrthophotos(context, view, transform) {
  if (!state.scenario.settings.show_orthophoto || !state.scenario.geodata.frame) return;
  const revision = state.scenario.geodata.revision;
  const xmax = view.minX + view.canvas.width / devicePixelRatio / view.scale;
  const ymax = view.minY + view.canvas.height / devicePixelRatio / view.scale;
  let level = Math.max(-3, Math.min(9, Math.ceil(Math.log2(2 / view.scale))));
  while (level < 9 && (Math.ceil((xmax - view.minX) / (256 * 2 ** level)) + 1) * (Math.ceil((ymax - view.minY) / (256 * 2 ** level)) + 1) > 48) level += 1;
  const size = 256 * 2 ** level;
  state.imageTiles ||= new Map(); state.imageAbort ||= new AbortController();
  context.save(); context.globalAlpha = state.scenario.settings.orthophoto_opacity;
  const needed = [];
  const bounds = state.scenario.map.bounds || state.laneletBounds;
  const xmin = bounds ? Math.max(view.minX, bounds[0] - size) : view.minX;
  const ymin = bounds ? Math.max(view.minY, bounds[1] - size) : view.minY;
  const xlimit = bounds ? Math.min(xmax, bounds[2] + size) : xmax;
  const ylimit = bounds ? Math.min(ymax, bounds[3] + size) : ymax;
  for (let x = Math.floor(xmin / size); x <= Math.floor(xlimit / size); x += 1) {
    for (let y = Math.floor(ymin / size); y <= Math.floor(ylimit / size); y += 1) {
      const key = `${revision}/${level}/${x}/${y}`, tile = state.imageTiles.get(key);
      if (tile?.image) {
        const [px, py] = transform([x * size, (y + 1) * size]);
        context.drawImage(tile.image, px, py, size * view.scale + 0.5, size * view.scale + 0.5);
      } else if (!tile || tile.retryAt < Date.now()) needed.push(key);
    }
  }
  context.restore();
  for (const key of needed) {
    if ((state.imageRequests || 0) >= 4) break;
    const tiles = state.imageTiles, signal = state.imageAbort.signal;
    tiles.set(key, { retryAt: Infinity }); state.imageRequests = (state.imageRequests || 0) + 1;
    api(`/api/orthophoto/${key}`, { signal, cache: "no-store" }, false).then(async (response) => {
      const bitmap = await createImageBitmap(await response.blob());
      if (revision !== state.scenario.geodata.revision || signal.aborted) { bitmap.close(); return; }
      tiles.set(key, { image: bitmap }); $("#imagery-status").textContent = "";
      if (tiles.size > 128) { const first = tiles.keys().next().value; tiles.get(first)?.image?.close(); tiles.delete(first); }
    }).catch((error) => {
      tiles.set(key, { retryAt: Date.now() + 30000 });
      if (!signal.aborted) $("#imagery-status").textContent = error.message;
    }).finally(() => { state.imageRequests -= 1; draw(); });
  }
}

function routingValues() {
  const values = { source: $("#route-source").value, lane_id: $("#route-lane").value };
  for (const [key, [id, divisor]] of Object.entries(routeFields)) {
    const field = $("#route-" + id);
    if (!field.value.trim() || !Number.isFinite(Number(field.value)) || Number(field.value) < Number(field.min))
      throw new Error("Complete the speed profile with valid values.");
    values[key] = Number(field.value) / divisor;
  }
  if (selectedActor()?.waypoints.length) {
    const saved = selectedActor().trajectory_options?.routing_settings;
    values.start_time_s = saved?.start_time_s ?? 0;
    values.start_speed_mps = saved?.start_speed_mps ?? 30 / 3.6;
  }
  return values;
}

async function updateRouteLanes() {
  const actor = selectedActor(), last = actor?.waypoints.at(-1), source = $("#route-source").value;
  const key = `${actor?.name}|${state.scenario.geodata.revision}|${source}|${last?.x_m}|${last?.y_m}`;
  if (state.routeLaneKey === key) return;
  state.routeLaneKey = key;
  const selected = actor?.trajectory_options?.routing_settings?.lane_id || "";
  $("#route-lane").replaceChildren(new Option("Automatic", ""));
  if (!last) return;
  try {
    const { lanes } = await api(`/api/actors/${encodeURIComponent(actor.name)}/route/lanes?source=${source}`);
    if (state.routeLaneKey !== key) return;
    state.routeLanes = lanes;
    for (const lane of lanes) $("#route-lane").add(new Option(`${lane.lane_id} (${Math.round(lane.heading_rad * 180 / Math.PI)}°)`, lane.lane_id));
    if (lanes.some((lane) => lane.lane_id === selected)) $("#route-lane").value = selected;
    draw();
  } catch (error) { if (state.routeLaneKey === key) $("#route-summary").textContent = error.message; }
}

async function saveRoutingSettings() {
  try {
    const actor = selectedActor(); if (!actor) return;
    const values = routingValues();
    await api(`/api/actors/${encodeURIComponent(actor.name)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routing_settings: values }) });
    await refresh();
  } catch (error) { setStatus(error.message); }
}
for (const input of $("#routing-settings").querySelectorAll("input, select")) input.onchange = () => {
  if (input.id === "route-source") { cancelRouteQueue(); state.routeLaneKey = null; updateRouteLanes(); }
  saveRoutingSettings(); draw();
};
$("#routing-settings").ontoggle = () => { if ($("#routing-settings").open) updateRouteLanes(); };
$("#route-mode").onchange = () => {
  cancelRouteQueue(); state.routeMode = $("#route-mode").checked;
  if (state.routeMode) { clearMeasurement(); setStatus("Route mode: click targets to extend the selected vehicle's trajectory"); }
  renderGeodata(); draw();
};

async function appendRouteTarget(x_m, y_m) {
  const actorName = selectedActor()?.name, epoch = state.routeEpoch, mapRevision = state.scenario.geodata.revision;
  let values;
  try { values = routingValues(); } catch (error) { $("#route-summary").textContent = error.message; setStatus(error.message); return; }
  const dt = state.scenario.settings.time_step_s;
  state.routePending = (state.routePending || 0) + 1;
  setStatus("Calculating route...", true);
  try {
    const result = await api(`/api/actors/${encodeURIComponent(actorName)}/route/append`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      prepare: () => {
        if (!state.routeMode || state.routeEpoch !== epoch || state.selected !== actorName
            || state.scenario.settings.map_mode || state.scenario.geodata.revision !== mapRevision) return null;
        return { body: JSON.stringify({ source: values.source, lane_id: values.lane_id, target: { x_m, y_m },
          parameters: { ...values, dt_s: dt }, actor_revision: selectedActor().revision, map_revision: mapRevision }) };
      },
    });
    if (!result) return;
    await refresh();
    if (state.routeEpoch !== epoch) return;
    const route = result.route;
    $("#route-summary").textContent = route.seeded ? "Start set. Click the next target."
      : `${route.length_m.toFixed(1)} m; ${route.duration_s.toFixed(2)} s; ${route.lane_changes} lane changes; ${route.lane_ids.join(" → ")}`;
    setStatus(route.seeded ? "Route start set" : "Route appended; click the next target");
  } catch (error) {
    if (state.routeEpoch === epoch) { cancelRouteQueue(); $("#route-summary").textContent = error.message; setStatus(error.message); }
  } finally { state.routePending -= 1; }
}

function drawRoutePreview(context, transform) {
  if (!state.routeMode) return;
  const last = selectedActor()?.waypoints.at(-1); if (!last) return;
  const lane = state.routeLanes?.find((lane) => lane.lane_id === $("#route-lane").value);
  context.save(); context.strokeStyle = "#b31864"; context.fillStyle = "#b31864"; context.lineWidth = 2;
  const start = transform([last.x_m, last.y_m]); context.beginPath(); context.arc(...start, 7, 0, Math.PI * 2); context.stroke();
  if (lane) drawArrow(context, start, [start[0] + 25 * Math.cos(lane.heading_rad), start[1] - 25 * Math.sin(lane.heading_rad)]);
  context.restore();
}

// Georeferenced layers, asynchronous imagery and explicit route generation.

const routeFields = {
  start_time_s: ["start-time", 1], start_speed_mps: ["start-speed", 3.6],
  end_speed_mps: ["end-speed", 3.6], reference_speed_mps: ["reference-speed", 3.6],
  acceleration_mps2: ["acceleration", 1], deceleration_mps2: ["deceleration", 1], dt_s: ["dt", 1],
};

function renderGeodata() {
  const geo = state.scenario.geodata, settings = state.scenario.settings;
  for (const key of ["show_lanelet", "show_origins", "show_orthophoto"]) $("#" + key.replaceAll("_", "-")).checked = settings[key];
  $("#orthophoto-opacity").value = settings.orthophoto_opacity;
  $("#map-opacity").value = settings.map_opacity;
  $("#lanelet-remove").disabled = !geo.lanelet_path;
  $("#show-orthophoto").disabled = !geo.frame;
  $("#georeference-summary").textContent = geo.warning || (geo.frame
    ? `${geo.frame.crs}; origin ${geo.frame.origin_lat.toFixed(7)}, ${geo.frame.origin_lon.toFixed(7)}; ${geo.lanelet_count} lanelets. Lanelet2 uses a projection origin near its first node.`
    : "Local map without geographic reference.");
  if (state.frameRevision !== geo.revision) {
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
    if (!$("#route-points").value.trim()) $("#route-source").value = geo.lanelet_path ? "lanelet2" : "xodr";
    clearRoutePreview();
  }
  const actor = selectedActor();
  $("#route-generator").hidden = settings.map_mode || !actor || actor.dimensions.actor_type !== "vehicle";
  $("#route-source option[value=lanelet2]").disabled = !geo.lanelet_path;
  $("#route-source option[value=xodr]").disabled = !state.scenario.map.roads.length;
  if (state.routeActor !== actor?.name) {
    state.routeActor = actor?.name;
    const generation = actor?.trajectory_options?.generation;
    $("#route-source").value = generation?.source || (geo.lanelet_path ? "lanelet2" : "xodr");
    $("#route-points").value = generation ? generation.points.map((point) => `${point.x_m}, ${point.y_m}${point.lane_id ? ", " + point.lane_id : ""}`).join("\n") : "";
    for (const [key, [id, divisor]] of Object.entries(routeFields)) $("#route-" + id).value = generation?.parameters?.[key] !== undefined
      ? generation.parameters[key] * divisor : key === "start_time_s" ? 0 : key === "dt_s" ? 0.1 : divisor === 3.6 ? 30 : 2;
    clearRoutePreview();
  }
}

async function geographicSetting(key, value) {
  try { await api("/api/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [key]: value }) }); await refresh(); }
  catch (error) { setStatus(error.message); }
}
for (const key of ["show_lanelet", "show_origins", "show_orthophoto"]) $("#" + key.replaceAll("_", "-")).onchange = (event) => geographicSetting(key, event.target.checked);
for (const key of ["orthophoto_opacity", "map_opacity"]) $("#" + key.replaceAll("_", "-")).onchange = (event) => geographicSetting(key, Number(event.target.value));
$("#lanelet-load").onclick = () => $("#lanelet-upload").click();
$("#lanelet-upload").onchange = async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const data = new FormData(); data.append("file", file);
  try { setStatus("Loading Lanelet2...", true); await api("/api/lanelet", { method: "POST", body: data }); state.camera = null; await refresh(); setStatus("Lanelet2 loaded"); }
  catch (error) { setStatus(error.message); }
  finally { event.target.value = ""; }
};
$("#lanelet-remove").onclick = async () => {
  try { await api("/api/lanelet", { method: "DELETE" }); await refresh(); } catch (error) { setStatus(error.message); }
};
$("#frame-apply").onclick = async () => {
  const frame = { ...state.scenario.geodata.frame, crs: $("#frame-crs").value };
  for (const key of ["x", "y", "heading"]) frame[key] = Number($("#frame-" + key).value);
  try { await api("/api/georeference", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(frame) }); state.camera = null; await refresh(); }
  catch (error) { setStatus(error.message); }
};

function drawGeographicLayers(context, view, transform) {
  if (state.scenario.settings.show_lanelet) {
    context.lineWidth = 1; context.strokeStyle = "#13847b";
    for (const lane of visibleLayerItems("lanelet", state.lanelet || [], view)) {
      drawPolyline(context, lane.left, transform); drawPolyline(context, lane.right, transform);
      context.save(); context.setLineDash([4, 4]); context.strokeStyle = "#d36a08";
      drawPolyline(context, lane.centerline, transform); context.restore();
    }
  }
  if (state.scenario.settings.show_origins && state.scenario.geodata.frame) {
    context.strokeStyle = "#bd184b"; context.fillStyle = "#bd184b";
    const mark = (point, label) => {
      const [x, y] = transform(point); context.beginPath(); context.moveTo(x - 8, y); context.lineTo(x + 8, y);
      context.moveTo(x, y - 8); context.lineTo(x, y + 8); context.stroke(); drawLabel(context, x, y, label, "#bd184b");
    };
    mark([0, 0], "Working frame origin");
    if (state.lanelet?.length) mark(state.scenario.geodata.lanelet_origin.point, "Lanelet2 projection origin (near map)");
  }
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

function clearRoutePreview() {
  state.routePreview = null; $("#route-apply").disabled = true; $("#route-summary").textContent = "";
}
function routePoints() {
  return $("#route-points").value.split("\n").filter((line) => line.trim()).map((line) => {
    const values = line.split(",").map((v) => v.trim());
    if (values.length < 2 || !values[0] || !values[1] || !Number.isFinite(Number(values[0])) || !Number.isFinite(Number(values[1]))) throw new Error("Each route point needs two numeric coordinates.");
    const geographic = $("#route-coordinates").value === "geographic";
    return { [geographic ? "lon" : "x_m"]: Number(values[0]), [geographic ? "lat" : "y_m"]: Number(values[1]), ...(values[2] ? { lane_id: values[2] } : {}) };
  });
}
for (const kind of ["start", "via", "target"]) $("#route-pick-" + kind).onclick = () => {
  state.routePick = kind; setStatus(`Select route ${kind} on the canvas`);
};
$("#route-pick-cancel").onclick = () => { state.routePick = null; setStatus("Route picking cancelled"); };
function pickRoutePoint(x, y) {
  if ($("#route-coordinates").value !== "local" && $("#route-points").value.trim()) { setStatus("Select local coordinates before picking points on the canvas"); return; }
  $("#route-coordinates").value = "local";
  const lines = $("#route-points").value.split("\n").filter((line) => line.trim());
  const point = `${x.toFixed(3)}, ${y.toFixed(3)}`;
  if (state.routePick === "start") { if (lines.length) lines[0] = point; else lines.push(point); }
  else if (state.routePick === "target") { if (lines.length >= 2) lines[lines.length - 1] = point; else lines.push(point); }
  else lines.splice(Math.max(1, lines.length - 1), 0, point);
  $("#route-points").value = lines.join("\n"); state.routePick = null; clearRoutePreview(); draw(); setStatus("Route point selected");
}
$("#route-generator").addEventListener("input", clearRoutePreview);
$("#route-preview").onclick = async () => {
  const actor = selectedActor(); if (!actor) return;
  try {
    const parameters = {};
    for (const [key, [id, divisor]] of Object.entries(routeFields)) {
      const raw = $("#route-" + id).value;
      if (!raw.trim()) throw new Error("Complete the speed profile fields.");
      parameters[key] = Number(raw) / divisor;
    }
    setStatus("Calculating route...", true);
    const preview = await api(`/api/actors/${actor.name}/route/preview`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: $("#route-source").value, points: routePoints(), parameters }) });
    state.routePreview = preview; $("#route-apply").disabled = false;
    $("#route-summary").textContent = `${preview.length_m.toFixed(1)} m; ${preview.duration_s.toFixed(2)} s; ${preview.lane_changes} lane changes. Confirm direction along lanes: ${preview.lane_ids.join(" → ")}`;
    draw(); setStatus("Route preview ready; use the route to replace the selected actor's trajectory");
  } catch (error) { clearRoutePreview(); setStatus(error.message); }
};
$("#route-apply").onclick = async () => {
  const actor = selectedActor(), preview = state.routePreview; if (!actor || !preview) return;
  try {
    await api(`/api/actors/${actor.name}/route/apply`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(preview) });
    clearRoutePreview(); await refresh(actor.name); setStatus("Route applied; its points and speeds are editable");
  } catch (error) { clearRoutePreview(); setStatus(error.message); }
};
function drawRoutePreview(context, transform) {
  if (!state.routePreview) return;
  context.save(); context.lineWidth = 4; context.strokeStyle = "#b31864";
  drawPolyline(context, state.routePreview.points, transform);
  const p = state.routePreview.points;
  if (p.length > 1) { context.fillStyle = "#b31864"; drawArrow(context, transform(p[0]), transform(p[Math.min(4, p.length - 1)])); }
  context.restore();
}

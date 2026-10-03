// Run explicitly with `node tests/e2e/benchmark-large-maps.js` against a local server.
// Uses the user-provided Aachen example maps; does not change either source file.
const { chromium } = require("@playwright/test");
const path = require("path");

async function main() {
  const browser = await chromium.launch();
  const baseline = process.env.SCENARIO_BENCHMARK_BASELINE === "1";
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(process.env.PLAYWRIGHT_BASE_URL || "http://localhost:8000");
    await page.waitForFunction(() => state.scenario?.actors.length === 2);
    const results = { viewport: "1440x1000", orthophotos: "off" };
    const started = Date.now();
    await page.locator("#map-upload").setInputFiles(path.resolve("example_maps/campus.xodr"));
    await page.waitForFunction(() => state.scenario?.map.roads.length === 286);
    results.xodr_import_and_render_ms = Date.now() - started;
    if (!baseline) {
      const laneletStarted = Date.now();
      await page.locator("#lanelet-upload").setInputFiles(path.resolve("example_maps/aachen.osm"));
      await page.waitForFunction(() => state.scenario?.geodata.lanelet_count === 5614);
      results.lanelet_import_and_render_ms = Date.now() - laneletStarted;
    }
    const layerRequests = [];
    page.on("request", (request) => { if (request.url().includes("/api/layers")) layerRequests.push(request.url()); });
    results.state = await page.evaluate(async () => {
      const result = {};
      for (const suffix of ["", "?compact=true"]) {
        const start = performance.now();
        const response = await fetch("/api/scenario" + suffix);
        const body = await response.text();
        result[suffix ? "compact" : "full"] = { ms: performance.now() - start, bytes: new TextEncoder().encode(body).length };
      }
      return result;
    });
    const choices = {
      xodr: [{ x_m: -52.572678, y_m: -417.765777, lane_id: "19515:0:1" }, { x_m: -40.702079, y_m: -225.194136, lane_id: "19515:0:1" }],
      lanelet2: [{ x_m: 464.000459, y_m: 185.292432, lane_id: "10043980" }, { x_m: 447.100332, y_m: 322.425402, lane_id: "10043980" }],
    };
    if (!baseline) results.routes = await page.evaluate(async (choices) => {
      const result = {};
      for (const [source, points] of Object.entries(choices)) {
        result[source] = [];
        for (let i = 0; i < 2; i += 1) {
          const start = performance.now();
          const preview = await api("/api/actors/vehicle_1/route/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source, points, parameters: { start_speed_mps: 0, end_speed_mps: 0 } }) });
          result[source].push({ ms: performance.now() - start, length_m: preview.length_m, duration_s: preview.duration_s, midpoint: preview.points[Math.floor(preview.points.length / 2)] });
          if (source === "lanelet2" && i === 1) {
            await api("/api/actors/vehicle_1/route/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(preview) });
            await refresh();
          }
        }
      }
      return result;
    }, choices);
    if (!baseline) results.append_routes = await page.evaluate(async ({ choices, midpoints }) => {
      const result = {};
      for (const [source, points] of Object.entries(choices)) {
        await api("/api/actors/vehicle_1", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ waypoints: [] }) });
        const seed = { source, lane_id: points[0].lane_id, target: points[0],
          parameters: { start_speed_mps: 0, end_speed_mps: 0, dt_s: 0.1 } };
        await api("/api/actors/vehicle_1/route/append", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...seed, map_revision: state.scenario.geodata.revision, actor_revision: state.scenario.actors[0].revision }) });
        const targets = [{ x_m: midpoints[source][0], y_m: midpoints[source][1], lane_id: points[0].lane_id }, points[1]];
        result[source] = [];
        let before;
        for (const target of targets) {
          const start = performance.now();
          const response = await api("/api/actors/vehicle_1/route/append", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...seed, target, map_revision: state.scenario.geodata.revision, actor_revision: state.scenario.actors[0].revision }) });
          await refresh();
          result[source].push({ ms: performance.now() - start, length_m: response.route.length_m, duration_s: response.route.duration_s });
          const after = state.scenario.actors[0].curve;
          if (before && ["time_s", "x_m", "y_m", "speed_mps", "yaw_rad"].some((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key].slice(0, before[key].length)))) throw new Error("Appending changed the previous route curve");
          before = JSON.parse(JSON.stringify(after));
        }
      }
      await api("/api/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lanelet_opacity: 0.6 }) });
      await refresh();
      await api("/api/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lanelet_opacity: 1 }) });
      await refresh();
      return result;
    }, { choices, midpoints: Object.fromEntries(Object.entries(results.routes).map(([source, values]) => [source, values[1].midpoint])) });
    results.actor_edit_ms = await page.evaluate(async () => {
      const times = [];
      for (let i = 0; i < 5; i += 1) {
        const pointIndex = Math.min(5, state.scenario.actors[0].waypoints.length - 2);
        const point = state.scenario.actors[0].waypoints[pointIndex];
        const start = performance.now();
        await api(`/api/actors/vehicle_1/waypoints/${pointIndex}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ x_m: point.x_m + 0.01 }) });
        await refresh();
        await new Promise(requestAnimationFrame);
        times.push(performance.now() - start);
      }
      return times;
    });
    results.animation = {};
    for (const mode of ["pan", "zoom", "playback"]) {
      results.animation[mode] = await page.evaluate(async (mode) => {
        const original = { minX: state.view.minX, minY: state.view.minY, scale: state.view.scale };
        const renderFrame = typeof drawScene === "function" ? drawScene : draw;
        state.camera = { ...original };
        renderFrame();
        const frames = [], cpu = [];
        let previous = await new Promise(requestAnimationFrame);
        for (let index = 0; index < 180; index += 1) {
          const now = await new Promise(requestAnimationFrame);
          frames.push(now - previous); previous = now;
          if (mode === "pan") state.camera.minX += 4 / state.camera.scale;
          if (mode === "zoom") state.camera.scale = original.scale * (1 + 0.2 * Math.sin(index / 30));
          if (mode === "playback") state.playbackTime = index / 10;
          const start = performance.now(); renderFrame(); cpu.push(performance.now() - start);
        }
        state.camera = original; draw();
        frames.sort((a, b) => a - b); cpu.sort((a, b) => a - b);
        return { fps: 1000 / (frames.reduce((a, b) => a + b, 0) / frames.length), frame_p95_ms: frames[Math.floor(frames.length * 0.95)], draw_median_ms: cpu[Math.floor(cpu.length / 2)], draw_p95_ms: cpu[Math.floor(cpu.length * 0.95)] };
      }, mode);
    }
    results.actor_mutation_layer_requests = layerRequests.length;
    results.browser_errors = errors;
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });

const { expect, test } = require("@playwright/test");
const path = require("path");
const { openScenario } = require("./helpers");
const fixture = (name) => path.join(__dirname, "fixtures", name);

async function loadMap(page, type, file) {
  await page.locator("#map-load-button").click();
  await page.locator("#map-type").selectOption(type);
  const chooser = page.waitForEvent("filechooser");
  await page.locator("#map-upload-button").click();
  await (await chooser).setFiles(fixture(file));
  await expect.poll(() => page.evaluate((type) => type === "lanelet2" ? state.scenario.geodata.lanelet_count : state.scenario.map.roads.length, type)).toBe(type === "lanelet2" ? 4 : 1);
}

async function worldClick(page, x, y, button = "left") {
  const point = await page.evaluate(([x, y]) => {
    const view = state.view, rect = view.canvas.getBoundingClientRect();
    return [rect.left + (x - view.minX) * view.scale,
      rect.top + view.canvas.height / devicePixelRatio - (y - view.minY) * view.scale];
  }, [x, y]);
  await page.mouse.click(...point, { button });
}

async function startRoute(page) {
  await openScenario(page);
  await loadMap(page, "xodr", "reference.xodr");
  await loadMap(page, "lanelet2", "lanes.osm");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#clear-actor").click();
  await expect.poll(() => page.evaluate(() => selectedActor().waypoints.length)).toBe(0);
  await page.locator("#time-step").fill("0.2");
  await page.locator("#time-step").press("Tab");
  await expect.poll(() => page.evaluate(() => state.scenario.settings.time_step_s)).toBe(0.2);
  await page.locator("#route-mode").check();
  await worldClick(page, 5, 0);
  await expect.poll(() => page.evaluate(() => selectedActor().waypoints.length)).toBe(1);
}

test("loads both map types and keeps expandable layer controls stable", async ({ page }) => {
  const errors = []; page.on("pageerror", (e) => errors.push(e.message));
  await openScenario(page);
  await loadMap(page, "xodr", "reference.xodr");
  await page.locator("#actor-list .actor").nth(1).click();
  const selected = await page.evaluate(() => state.selected);
  await loadMap(page, "lanelet2", "lanes.osm");
  expect(await page.evaluate(() => state.selected)).toBe(selected);
  await expect(page.locator("#geographic-layers")).toHaveCount(0);
  await expect(page.locator("#route-generator")).toHaveCount(0);
  await page.getByText("View", { exact: true }).click();
  for (const layer of ["XODR", "Lanelet2", "Orthophoto"]) {
    const disclosure = page.getByRole("button", { name: `${layer} settings`, exact: true });
    await disclosure.click(); await expect(disclosure).toHaveAttribute("aria-expanded", "true");
  }
  await page.evaluate(() => {
    window.drawnOriginLabels = [];
    const original = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
      if (text.includes("origin")) window.drawnOriginLabels.push(text);
      return original.call(this, text, ...args);
    };
  });
  await page.locator("#show-xodr-origin").check();
  await page.locator("#show-lanelet-origin").check();
  await expect.poll(() => page.evaluate(() => window.drawnOriginLabels.includes("XODR origin") && window.drawnOriginLabels.includes("Lanelet2 projection origin"))).toBe(true);
  await page.evaluate(() => { window.drawnOriginLabels = []; });
  await page.locator("#view-show_map").uncheck();
  await expect(page.locator("#show-xodr-origin")).toBeChecked();
  await expect.poll(() => page.evaluate(() => window.drawnOriginLabels.includes("Lanelet2 projection origin"))).toBe(true);
  expect(await page.evaluate(() => window.drawnOriginLabels.includes("XODR origin"))).toBe(false);
  await expect(page.locator("#show-lanelet")).toBeChecked();
  await page.locator("#lanelet-opacity").evaluate((input) => { window.savedLayerSlider = input; });
  await page.locator("#lanelet-opacity").focus();
  await page.locator("#lanelet-opacity").press("ArrowLeft");
  await expect.poll(() => page.evaluate(() => state.scenario.settings.lanelet_opacity)).toBe(0.95);
  expect(await page.evaluate(() => document.activeElement === window.savedLayerSlider && window.savedLayerSlider === document.querySelector("#lanelet-opacity"))).toBe(true);
  await expect(page.locator("#lanelet-view-options")).toBeVisible();
  await page.getByText("View", { exact: true }).click();
  await page.locator("#map-load-button").click();
  await page.locator("#lanelet-remove").click();
  await expect.poll(() => page.evaluate(() => state.scenario.geodata.lanelet_path)).toBe(null);
  expect(await page.evaluate(() => state.scenario.map.roads.length)).toBe(1);
  expect(errors).toEqual([]);
});

test("route clicks append in order with the shared time step and inherited endpoint", async ({ page }) => {
  const errors = []; page.on("pageerror", (e) => errors.push(e.message));
  await startRoute(page);
  await expect(page.locator("#waypoint-timing")).toBeDisabled();
  let requests = 0;
  await page.route("**/route/append?*", async (route) => {
    requests += 1;
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({ response });
  });
  await worldClick(page, 35, 0);
  await worldClick(page, 90, 0);
  await expect.poll(() => page.evaluate(() => selectedActor().trajectory_options?.generation?.segments?.length)).toBe(2);
  const result = await page.evaluate(() => ({ points: selectedActor().waypoints, curve: selectedActor().curve }));
  expect(Math.abs(result.points.at(-1).x_m - 90)).toBeLessThan(0.2);
  expect(requests).toBe(2);
  for (let i = 1; i < result.points.length; i += 1) {
    expect(result.points[i].time_s - result.points[i - 1].time_s).toBeGreaterThan(0);
    expect(result.points[i].time_s - result.points[i - 1].time_s).toBeLessThanOrEqual(0.2000001);
  }
  await page.locator("#routing-settings summary").click();
  await expect(page.locator("#route-start-time")).toHaveJSProperty("readOnly", true);
  await expect(page.locator("#route-start-speed")).toHaveJSProperty("readOnly", true);
  expect(await page.locator("#route-start-time").inputValue()).toBe(String(result.points.at(-1).time_s));
  await page.locator("#route-mode").uncheck();
  await expect(page.locator("#waypoint-timing")).toBeEnabled();
  expect(errors).toEqual([]);
});

test("switching actors cancels targets still waiting in the route queue", async ({ page }) => {
  await startRoute(page);
  let requests = 0;
  let release; const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/route/append?*", async (route) => {
    requests += 1; const response = await route.fetch(); await gate; await route.fulfill({ response });
  });
  await page.evaluate(() => { activateCanvasPoint(35, 0); activateCanvasPoint(90, 0); });
  await expect.poll(() => requests).toBe(1);
  await page.locator("#actor-list .actor").nth(1).click();
  release();
  await expect.poll(() => page.evaluate(() => state.routePending)).toBe(0);
  expect(requests).toBe(1);
  const actors = await page.evaluate(() => state.scenario.actors);
  expect(actors[0].waypoints.at(-1).x_m).toBeCloseTo(35, 5);
  expect(actors[1].waypoints).toHaveLength(3);
  expect(await page.evaluate(() => state.selected)).toBe(actors[1].name);
});

test("middle click deletes exactly one endpoint without dragging or confirmation", async ({ page }) => {
  await openScenario(page);
  const dialogs = []; page.on("dialog", async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  const before = await page.evaluate(() => selectedActor().waypoints);
  await worldClick(page, before.at(-1).x_m, before.at(-1).y_m, "middle");
  await expect.poll(() => page.evaluate(() => selectedActor().waypoints.length)).toBe(before.length - 1);
  expect(await page.evaluate(() => selectedActor().waypoints.map((p) => [p.x_m, p.y_m]))).toEqual(before.slice(0, -1).map((p) => [p.x_m, p.y_m]));
  expect(await page.evaluate(() => state.dragTarget)).toBe(null);
  expect(dialogs).toEqual([]);
  await page.locator("#mode-toggle").click();
  await expect.poll(() => page.evaluate(() => state.scenario.settings.map_mode)).toBe(true);
  await page.mouse.click(500, 350, { button: "middle" });
  expect(await page.evaluate(() => selectedActor().waypoints.length)).toBe(before.length - 1);
});

test("restores saved routing preferences for the same actor and disables a missing source", async ({ page }) => {
  await startRoute(page);
  await page.evaluate(() => activateCanvasPoint(35, 0));
  const config = await page.evaluate(async () => {
    await api(`/api/actors/${state.selected}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routing_settings: {
      source: "lanelet2", reference_speed_mps: 12, end_speed_mps: 6, acceleration_mps2: 3, deceleration_mps2: 4,
    } }) });
    await refresh();
    const response = await api("/api/export/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_name: "controls" }) }, false);
    return response.text();
  });
  await page.evaluate(async () => {
    await api(`/api/actors/${state.selected}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routing_settings: { source: "xodr", reference_speed_mps: 5 } }) });
    await refresh();
  });
  await expect(page.locator("#route-source")).toHaveValue("xodr");
  await page.locator("#scenario-upload").setInputFiles({ name: "controls_config.json", mimeType: "application/json", buffer: Buffer.from(config) });
  await expect.poll(() => page.evaluate(() => selectedActor().trajectory_options.routing_settings.reference_speed_mps)).toBe(12);
  await expect(page.locator("#route-source")).toHaveValue("lanelet2");
  await expect(page.locator("#route-end-speed")).toHaveValue("21.6");
  await expect(page.locator("#time-step")).toHaveValue("0.2");
  await page.evaluate(async () => { await api("/api/lanelet", { method: "DELETE" }); await refresh(); });
  await expect(page.locator("#route-mode")).toBeDisabled();
  await expect(page.locator("#waypoint-timing")).toBeEnabled();
});

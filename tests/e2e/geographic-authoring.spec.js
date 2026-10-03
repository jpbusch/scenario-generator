const { expect, test } = require("@playwright/test");
const path = require("path");
const { openScenario } = require("./helpers");

const fixture = (name) => path.join(__dirname, "fixtures", name);

test("loads aligned layers, appends routes, edits samples and retains compact map state", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => state.scenario.map.roads.length)).toBe(1);
  await page.locator("#lanelet-upload").setInputFiles(fixture("lanes.osm"));
  await expect.poll(() => page.evaluate(() => state.scenario.geodata.lanelet_count)).toBe(4);
  await page.getByText("View", { exact: true }).click();
  await page.getByRole("button", { name: "Lanelet2 settings", exact: true }).click();
  await expect(page.locator("#lanelet-origin-summary")).toContainText("4 lanelets");
  await page.locator("#show-lanelet").uncheck();
  await page.locator("#show-lanelet").check();
  await page.getByText("View", { exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#clear-actor").click();
  await expect.poll(() => page.evaluate(() => selectedActor().waypoints.length)).toBe(0);
  await page.locator("#time-step").fill("0.05");
  await page.locator("#time-step").press("Tab");
  await page.locator("#routing-settings summary").click();
  await page.locator("#route-source").selectOption("lanelet2");
  await page.locator("#route-start-speed").fill("0");
  await page.locator("#route-start-speed").press("Tab");
  await page.locator("#route-end-speed").fill("0");
  await page.locator("#route-end-speed").press("Tab");
  await page.locator("#route-mode").check();
  await page.evaluate(() => activateCanvasPoint(5, 0));
  await expect(page.locator("#route-summary")).toContainText("Start set");
  const layerRequests = [];
  page.on("request", (request) => { if (request.url().includes("/api/layers")) layerRequests.push(request.url()); });
  await page.evaluate(() => activateCanvasPoint(90, 0));
  await expect(page.locator("#route-summary")).toContainText("1000 → 1001");
  await expect.poll(() => page.evaluate(() => selectedActor().trajectory_options.interpolation_sections.at(-1).mode)).toBe("kinematic");
  await expect(page.locator("#waypoint-body tr")).toHaveCount(100);
  await page.locator("#waypoints-panel .table-pager input").fill("2");
  await page.locator("#waypoints-panel .table-pager input").press("Tab");
  await expect(page.locator("#waypoint-100-speed_mps")).toBeVisible();
  await page.locator("#waypoint-100-speed_mps").fill("6");
  await page.locator("#waypoint-100-speed_mps").press("Tab");
  await expect.poll(() => page.evaluate(() => state.scenario.actors[0].waypoints[100].speed_mps)).toBe(6);
  expect(layerRequests).toHaveLength(0);
  expect(errors).toEqual([]);
});

test("imagery failures leave authoring usable and failed routes retain previous points", async ({ page }) => {
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => Boolean(state.scenario.geodata.frame))).toBe(true);
  await page.route("**/api/orthophoto/**", (route) => route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "WMS timeout" }) }));
  await page.getByText("View", { exact: true }).click();
  await page.getByRole("button", { name: "Orthophoto settings", exact: true }).click();
  await page.locator("#show-orthophoto").check();
  await expect(page.locator("#imagery-status")).toContainText("WMS timeout");
  await page.getByText("View", { exact: true }).click();
  await page.locator("#route-mode").check();
  const before = await page.evaluate(() => JSON.stringify(selectedActor().waypoints));
  await page.evaluate(() => activateCanvasPoint(90, -1.5));
  await expect(page.locator("#route-summary")).toContainText("Reference speed");
  expect(await page.evaluate(() => JSON.stringify(selectedActor().waypoints))).toBe(before);
  await page.locator("#waypoint-1-speed_mps").fill("12");
  await page.locator("#waypoint-1-speed_mps").press("Tab");
  await expect.poll(() => page.evaluate(() => selectedActor().waypoints[1].speed_mps)).toBe(12);
});

test("discards pending imagery when the working frame changes", async ({ page }) => {
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => Boolean(state.scenario.geodata.frame))).toBe(true);
  const pending = [];
  await page.route("**/api/orthophoto/**", (route) => { pending.push(route); });
  await page.getByText("View", { exact: true }).click();
  await page.locator("#show-orthophoto").check();
  await expect.poll(() => pending.length).toBeGreaterThan(0);
  const originalRevision = await page.evaluate(() => state.scenario.geodata.revision);
  const png = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1;
    return canvas.toDataURL("image/png").split(",")[1];
  }), "base64");
  await page.evaluate(async () => {
    const frame = state.scenario.geodata.frame;
    await api("/api/georeference", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...frame, x: frame.x + 10 }) });
    await refresh();
  });
  const revision = await page.evaluate(() => state.scenario.geodata.revision);
  expect(revision).toBeGreaterThan(originalRevision);
  await expect.poll(() => pending.some((route) => route.request().url().includes(`/orthophoto/${revision}/`))).toBe(true);
  // Late replies from the previous frame may finish after the new requests.
  const old = pending.filter((route) => route.request().url().includes(`/orthophoto/${originalRevision}/`));
  for (const route of old) await route.fulfill({ contentType: "image/png", body: png }).catch(() => undefined);
  await page.route("**/api/orthophoto/**", (route) => route.fulfill({ contentType: "image/png", body: png }));
  for (const route of pending.filter((route) => !old.includes(route))) await route.fulfill({ contentType: "image/png", body: png });
  await expect.poll(() => page.evaluate(() => [...state.imageTiles.values()].some((tile) => Boolean(tile.image)))).toBe(true);
  const keys = await page.evaluate(() => [...state.imageTiles.keys()]);
  expect(keys.every((key) => key.startsWith(`${revision}/`))).toBe(true);
});

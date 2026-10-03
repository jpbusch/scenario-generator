const { expect, test } = require("@playwright/test");
const path = require("path");
const { openScenario } = require("./helpers");

const fixture = (name) => path.join(__dirname, "fixtures", name);

test("loads aligned layers, previews a route, edits samples and retains compact map state", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => state.scenario.map.roads.length)).toBe(1);
  await page.locator("#lanelet-upload").setInputFiles(fixture("lanes.osm"));
  await expect.poll(() => page.evaluate(() => state.scenario.geodata.lanelet_count)).toBe(4);
  await page.locator("#geographic-layers summary").first().click();
  await expect(page.locator("#georeference-summary")).toContainText("4 lanelets");
  await page.locator("#show-lanelet").uncheck();
  await page.locator("#show-lanelet").check();

  await page.locator("#route-generator summary").click();
  await page.locator("#route-source").selectOption("lanelet2");
  await page.locator("#route-points").fill("5, 0, 1000\n90, 0, 1001");
  await page.locator("#route-start-speed").fill("0");
  await page.locator("#route-end-speed").fill("0");
  const original = await page.evaluate(() => state.scenario.actors[0].waypoints.length);
  await page.locator("#route-preview").click();
  await expect(page.locator("#route-summary")).toContainText("1000 → 1001");
  expect(await page.evaluate(() => state.scenario.actors[0].waypoints.length)).toBe(original);
  const layerRequests = [];
  page.on("request", (request) => { if (request.url().includes("/api/layers")) layerRequests.push(request.url()); });
  await page.locator("#route-apply").click();
  await expect.poll(() => page.evaluate(() => state.scenario.actors[0].trajectory_options.interpolation_mode)).toBe("kinematic");
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

test("imagery failures leave authoring usable and stale previews cannot overwrite edits", async ({ page }) => {
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => Boolean(state.scenario.geodata.frame))).toBe(true);
  await page.route("**/api/orthophoto/**", (route) => route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "WMS timeout" }) }));
  await page.locator("#geographic-layers summary").first().click();
  await page.locator("#show-orthophoto").check();
  await expect(page.locator("#imagery-status")).toContainText("WMS timeout");
  await page.locator("#route-generator summary").click();
  await page.locator("#route-points").fill("5, -1.5\n90, -1.5");
  await page.locator("#route-preview").click();
  await expect(page.locator("#route-apply")).toBeEnabled();
  await page.locator("#waypoint-1-speed_mps").fill("12");
  await page.locator("#waypoint-1-speed_mps").press("Tab");
  await expect.poll(() => page.evaluate(() => state.scenario.actors[0].waypoints[1].speed_mps)).toBe(12);
  await page.locator("#route-apply").click();
  await expect(page.locator("#status")).toContainText("changed");
  expect(await page.evaluate(() => state.scenario.actors[0].waypoints.length)).toBe(3);
});

test("discards pending imagery when the working frame changes", async ({ page }) => {
  await openScenario(page);
  await page.locator("#map-upload").setInputFiles(fixture("reference.xodr"));
  await expect.poll(() => page.evaluate(() => Boolean(state.scenario.geodata.frame))).toBe(true);
  const pending = [];
  await page.route("**/api/orthophoto/**", (route) => { pending.push(route); });
  await page.locator("#geographic-layers summary").first().click();
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

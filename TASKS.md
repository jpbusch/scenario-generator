# Georeferenced scenario authoring

## Integrated authoring controls

| Task | Status | Acceptance |
| --- | --- | --- |
| Expandable layers in View → Map view | Done | Independent visibility, opacity and origins; stable controls and accessible disclosures |
| Shared Load map dialog | Done | XODR defaults/upload and Lanelet2 upload/removal; retain both layers |
| Click-to-append vehicle routing | Done | Current endpoint/time/speed, shared time step, ordered clicks and atomic revision checks |
| Section interpolation and persistence | Done | Preserve previous curves; mixed manual/kinematic editing, playback and export roundtrips |
| Middle-button delete | Done | Delete exactly one last waypoint without dragging or browser autoscroll |
| Integrated regression and performance checks | Done | Python, Playwright, lint, wheel and large-map cache checks; results recorded below |


### Integrated controls — results (2026-10-03)

- **200 Python tests pass**, including 17 append/section regressions: unchanged manual and generated prefixes, inferred legacy speeds, exact position/time/speed consistency, in-lane start transitions, atomic failures and revision conflicts, both routing sources, bidirectional matching, section edits, config/JSON roundtrips and settings migration.
- **30 Playwright cases pass**: the complete 29-case regression suite plus the new saved-preferences/missing-source case; the changed routing controls were additionally checked after the final availability change. Coverage includes the existing accessibility states, real file-chooser imports, persistent layer controls/focus, independent origins, consecutive pointer targets, cancellation on actor switches and middle-button deletion without confirmation/dragging.
- Ruff, `git diff --check`, wheel build and installed-wheel smoke checks pass. Compose was restarted with the updated backend and remains available at `http://localhost:8000`; its orthophoto cache volume is retained.
- Repeat the large-map measurement with `node tests/e2e/benchmark-large-maps.js`. It now measures two appended sections per source, checks that the first section's curve stays byte-for-byte unchanged and changes Lanelet2 opacity while observing layer requests. These changes do not modify either example map.

| Warm measurement, both example maps | Integrated controls |
| --- | ---: |
| XODR upload + transfer + render | 3.40 s |
| Lanelet2 upload + transfer + render | 4.01 s |
| Compact state, HTTP | 3.2 ms / 9,477 bytes |
| XODR preview, cold / warm | 710 / 7.8 ms |
| Lanelet2 preview, cold / warm | 324 / 9.8 ms |
| Append XODR section + update browser | 42.5–62.7 ms |
| Append Lanelet2 section + update browser | 34.6–57.3 ms |
| Waypoint edit + next browser frame | 57.1–84.4 ms |
| Pan / zoom / playback | 57.8 / 59.7 / 60.0 FPS |
| Layer requests during warm actor and opacity changes | 0 |
| Browser exceptions | 0 |

Same machine, Compose limits, viewport and imagery-off conditions as documented below. The updated playback trajectory contains two independently generated 0 → 30 → 0 km/h sections sampled at 0.1 s. Default UI sampling continues to use the existing Time step value; enabling Route does not change it. Main layer visibility gates annotations and origin markers, while retaining each subordinate setting. Both origins are off and Lanelet2 opacity is 1 by default; the legacy shared origin flag migrates to both independent flags.

Implementation order: performance → shared map frame / Lanelet2 → NRW imagery → routing / kinematics → local deployment.

| Task | Status | Acceptance |
| --- | --- | --- |
| Baseline and large-map performance | Done | Revision caches, compact state, spatial filtering, reusable vector/raster drawing, binary-search playback and 100-row tables; measurements below |
| Shared georeference and Lanelet2 layer | Done | XODR offset/rotation, aligned OSM layer, independent visibility/origins, transformed actors/layers on frame changes, retained export reference |
| NRW orthophotos | Done | EPSG:25832 WMS → local-frame reprojection; four asynchronous requests, coalescing, 512 MiB persistent cache, stale-response cancellation and recoverable errors |
| Routing and kinematic trajectories | Done | Native German vehicle rules, directed XODR lane sections/road/junction links, via points, corridor-checked lane changes, revision-checked preview/apply, analytical motion and editable samples |
| Project persistence and exports | Done | Version 5 and old configs load; frame/layer/interpolation/generation metadata; relative XODR/OSM bundle dependencies; missing-reference messages |
| Docker Compose and documentation | Done | Local image build, non-root runtime, Rasterio system library and persistent cache volume; localhost instructions in README |
| Regression and acceptance | Done | Python, browser, lint, license, installed-wheel, Compose, actual NRW tile, cache restart and full example-map export checks |

## Measurement conditions

Reference inputs: local `example_maps/campus.xodr` (286 roads, about 8.8 MB) and `example_maps/aachen.osm` (5,614 lanelets, about 5.4 MB; 2,861 native vehicle directions). These user-provided files are not modified or implicitly added to git. Exported original map bytes were compared successfully with both inputs.

Measured on 2026-10-02, Linux x86_64, AMD Ryzen Threadripper PRO 5995WX, Python 3.12 host environment and Python 3.11 Compose runtime (default two-CPU / 2 GiB limits). Browser: headless Playwright Chromium, 1440 × 1000 CSS pixels, pixel ratio 1, imagery off, default display settings. Animation measurements use 180 requestAnimationFrame ticks per mode after warming drawing caches. Original comparison: isolated source copy of commit `a80b00f` on port 8001, the same installed dependency environment and browser; XODR only because Lanelet2 did not exist there. Updated browser measurements use both maps and a generated, sampled vehicle trajectory. These are local automated measurements, not a guarantee for other machines or map sizes.

Targets after warming caches: 30 FPS navigation/playback, typical actor edits <200 ms, route previews <2 s. All three targets passed under these conditions.

## Results

### Performance

| Measurement | Original | Updated |
| --- | ---: | ---: |
| XODR import, backend only | 11.14 s | 0.78–1.09 s |
| XODR upload + geometry transfer + browser render | 12.32 s | 3.47 s |
| Lanelet2 upload + geometry transfer + browser render | Unavailable | 3.99 s |
| Full snapshot calculation, backend | 0.48–0.51 s per call | 0.79 s cold; 0.14 ms warm |
| Actor edit + compact snapshot calculation, backend | 486 ms with full snapshot | 0.47–0.55 ms |
| Full state fetch including transfer, browser | 647 ms / 13,561,157 bytes | 127 ms / 13,561,861 bytes |
| Compact state fetch including transfer, browser | Full state only | 3.2 ms / 9,271 bytes |
| Waypoint edit + response + refreshed tables + next browser frame | 1,408–1,572 ms | 51–59 ms |
| Panning | 29.7 FPS | 57.8 FPS |
| Zooming | 23.0 FPS | 59.7 FPS |
| Playback | 23.0 FPS | 60.0 FPS |
| Browser drawing CPU, median, pan / zoom / playback | 15.7 / 16.0 / 16.0 ms | 1.7 / 0.5 / 0.5 ms |
| Browser frame interval, p95, pan / zoom / playback | 50.1 / 50.1 / 50.1 ms | 16.8 / 16.8 / 16.7 ms |
| XODR route preview, cold graph / warm graph, HTTP | Unavailable | 735 / 7.3 ms |
| Lanelet2 route preview, cold graph / warm graph, HTTP | Unavailable | 341 / 10.2 ms |

The warm actor-change test observes **zero `/api/layers` requests**. Python checks additionally make a map-geometry rebuild fail the test and verify the unchanged actor's cached payload is reused. Full responses remain available for existing API clients; their geometry serialization and transfer intentionally still cost more than compact responses. Initial geometry upload/render is measured separately from warm editing.

Route examples: 195 m on XODR lane `19515:0:1`, and 159 m on Lanelet2 `10043980`, using 0 → 30 → 0 km/h, 2 m/s² acceleration/braking and 0.1 s sampling. Reported HTTP timings include preview geometry and the analytical profile. Intersection and lane-change cases are covered separately by deterministic tests.

Reproduce the browser measurements with a running local server and the existing E2E dependencies installed:

```bash
node tests/e2e/benchmark-large-maps.js
```

The script reads the two local example maps and prints JSON. An original server can be measured using `PLAYWRIGHT_BASE_URL=http://localhost:8001 SCENARIO_BENCHMARK_BASELINE=1` before the same command. The script is explicit rather than part of routine CI: user-provided maps stay unversioned.

### Validation

- **Python:** 183 tests pass, including legacy behavior and 31 geographic-authoring cases. Projection roundtrips, offset/rotation/export, frame changes with parked heading, native Lanelet2 alignment, directed road/section/junction links, changed lane IDs, contact points, added editor roads, via points, legal/forbidden and too-short lane changes, infeasible profiles, exact geometry/profile events, editing/export roundtrips, revision rejection and map caching are covered.
- **Browser:** 25 Playwright tests pass in the Compose runtime, including the existing accessibility, authoring, map-editor, playback, export and session-isolation checks. New cases cover aligned layers, preview/apply, paged sample editing, compact mutation responses, WMS failures and delayed tiles after a frame change.
- **Lint / packaging:** Ruff and `git diff --check` pass. A wheel is built and installed in a separate environment; the existing wheel smoke test runs outside the repository with port 8000 free. New Python modules and geographic-layer assets are packaged. The existing LicenseCheck command passes.
- **Imagery:** mocked WMS tests cover offset/rotation reprojection, concurrent request coalescing, cache eviction, timeout recovery and restart reuse. An actual `nw_dop_rgb` tile was retrieved through the Compose API: 512 × 512 RGBA PNG, 533,559 bytes, 0.827 s cold and 0.003 s cached. After container restart the same tile was served in 0.012 s with an identical SHA-256. WMS URLs use EPSG:25832; browser responses bypass URL caching so repeated session revision numbers cannot reuse imagery from another frame.
- **Compose:** build and start pass using the existing non-root UID/GID configuration. Loaded both full example maps, generated/applied a Lanelet2 route, and exported JSON/XOSC/XODR/OSM/version-5 config together. Original XODR/OSM bytes and relative references were checked. The named cache volume survives restart. UI: `http://localhost:8000`.

Reproduction commands:

```bash
uv run --locked --extra dev python -m pytest
uv run --locked --extra dev ruff check .
uv build --wheel
(cd tests/e2e && npm ci && npm run test:e2e)
docker compose -f docker/docker-compose.yml up --build
```

The wheel smoke test requires its own installed environment and a free port 8000, as in the existing CI job. Python emits existing Matplotlib/Pyparsing deprecation notices and Rasterio's expected warnings for PNG test images without embedded georeferencing; the WMS bounds supply their actual transform.

### Retained limits and intentional exclusions

- Routing uses one source per route and supports vehicles. Existing cyclist/pedestrian/manual workflows remain available. The reference speed is explicit and does not simulate signs, signals or other actors.
- Lane changes use a smooth 15 m transition inside the sampled driving corridor. Missing/unsupported XODR lane-change markings are handled conservatively; disconnected geometry above 2 m and insufficient transition space produce an error instead of joining unrelated roads.
- Generated sections retain their source/parameters and kinematic interpolation after edits. Route mode explicitly appends new sections; it never regenerates earlier sections. The legacy preview/apply API remains available. Manual sections retain smoothing.
- Lanelet2 remains read-only and needs the Linux x86_64 bindings, without ROS. No OSM/XODR conversion or complete simple-scenario JSON importer was added.
- Orthophotos remain opt-in online tiles, capped at 512 MiB on disk; there is no whole-map offline downloader. Save project/export bundles before restarting because editor sessions remain temporary. Extracted bundle map dependencies are uploaded before its config, matching the existing separate-file import workflow.

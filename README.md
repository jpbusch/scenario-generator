<p align="left"><img src="scenario_generator/webapp/branding/logo.svg" width="500" alt="scenario.generator" /></p>

<p align="left">
  scenario.generator is a web app based workspace to create and export driving scenarios easily and accurately.
</p>

<p align="left">
  <a href="https://scenario.center/generator"><strong>Try scenario.generator online →</strong></a>
</p>

![scenario.generator interface](assets/screenshot.png)

## Example use cases

scenario.generator supports a range of scenario-design workflows and can be
extended for project-specific needs. Typical examples include:

- **Rapid prototyping:** turn a test idea into a multi-actor traffic scenario.
- **Simulation preparation:** create scenario and map inputs for a simulator or
  an automated-driving test environment.
- **Scenario adaptation:** visually inspect and refine existing scenario data
  for a new test purpose.
- **Criticality exploration:** compare traffic interactions and investigate
  near-conflict situations.

## How to use it

scenario.generator provides two complementary authoring modes.

### Trajectory mode

Add vehicles, cyclists, or pedestrians and draw their trajectories on the
interactive canvas. Positions, timestamps, and speeds can be refined in the
trajectory table or velocity profile. An OpenDRIVE map can be loaded as context
and trajectory points can be snapped to its lane centerlines.

### Map mode

Create an OpenDRIVE map from scratch or load an existing one. Road geometry,
lane properties, and connections can be edited before switching back to
trajectory mode to place road users on the map.

In either mode, use the playback and calculated metrics to inspect the result.
Then add scenario details such as actor dimensions, actions, controllers,
environment data, or perception gaps, and export the required files. Selected
postprocessing steps can be applied as part of the export.

For a guided introduction, open
the [tutorial overview](scenario_generator/webapp/documentation/README.md) or
start with one of these examples:

1. [Create an intersecting conflict (no map)](scenario_generator/webapp/documentation/01-intersection-conflict.md)
2. [Build a simple map and drive it](scenario_generator/webapp/documentation/02-create-simple-map.md)
3. [Create a rainy pedestrian crossing](scenario_generator/webapp/documentation/03-import-adapt-map-scenario.md)
4. [Test OpenADStack in OpenADSim](scenario_generator/webapp/documentation/04-openads-scenario.md)

## Outputs

Scenarios can be exported in different formats for further usage:

- `Scenario (.xosc)`: OpenSCENARIO XML with actors and storyboard actions.
- `Map (.xodr)`: an editable or generated OpenDRIVE map.
- `Trajectories (.json)`: sampled trajectories with time, position, speed,
  yaw, dimensions, and optional detection availability.
- `Omega-Prime (.mcap)`: [MCAP](https://mcap.dev/spec) with
  [ASAM OSI](https://www.asam.net/standards/detail/osi/) GroundTruth data and
  map data when available.
- `scenario_config.json`: the editable scenario.generator project state. Saved
  waypoint timestamps and speeds are preserved when the project is loaded
  again. Maps remain separate OpenDRIVE files referenced by the config; export
  bundles include the referenced map automatically.

## Development setup

### Run locally with Docker Compose

From the repository root:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Open [http://localhost:8000](http://localhost:8000). Compose builds the existing
Dockerfile from your checkout; `SCENARIO_GENERATOR_IMAGE` still controls the
image name. The `orthophoto-cache` named volume retains downloaded NRW imagery
across container restarts. Save the scenario configuration/export bundle before
restarting: editor sessions themselves remain temporary, as in the original app.

### Georeferenced maps and automatic vehicle routes

1. Load an OpenDRIVE map as usual. Its `geoReference` and header offset establish
   the local working frame. For this checkout, try `example_maps/campus.xodr`.
2. Open **Geographic layers** and import a Lanelet2 `.osm` map, for example
   `example_maps/aachen.osm`. It is projected into the XODR frame; importing it
   does not replace the XODR. Lanelet2 alone establishes a nearby local UTM frame.
3. Toggle **NRW orthophotos**, Lanelet2 and coordinate origins independently.
   Use the opacity sliders to compare the maps. Orthophotos load online only
   near the map extent and visible viewport; failures leave the editor usable.
4. Select a vehicle and open **Create vehicle route**. Choose XODR or Lanelet2,
   pick a start and target on the canvas, and optionally add via points. Numeric
   entries accept local X/Y or longitude/latitude, one point per line. An optional
   third value selects a lane: `road_id:section_index:lane_id` for imported XODR,
   `road_name:lane_id` for synthetic maps, or a Lanelet2 ID. An inverted Lanelet2
   direction uses its negative ID when vehicle traffic rules permit it.
5. Enter start/end/reference speeds in km/h, positive acceleration and braking
   in m/s², a start time and sampling interval. **Preview route** displays its
   direction, matched lanes, length, duration and lane changes. **Use route**
   replaces only the selected actor's trajectory.
6. Edit the generated points and speeds with the existing tools. Large tables
   have pages of 100 rows; the canvas thins sample labels for readability.
   Generated routes use kinematic interpolation so their constant acceleration
   profile is retained. Manual trajectories keep their established smoothing.

Routing observes lane direction, vehicle accessibility, topology and lane-change
permissions. Lane changes need a continuous driving corridor and at least 15 m
of usable parallel lane length. The speed profile follows the explicitly entered
reference speed; it does not simulate traffic lights, interactions or speed-limit
compliance. Short routes reduce the reachable peak speed; impossible endpoint
speeds produce an error. Editing does not regenerate a route automatically.

For an XODR without a geographic reference, enter a projected metric CRS, origin
and rotation under **Set map georeference** before using geographic overlays.
The origin is the projected coordinate of local `(0, 0)`; rotation is in radians.
Lanelet2's displayed projection origin is a working reference near its first
node, not an origin assumed to be stored in the OSM file.

Project configuration version 5 saves the working frame, layer settings and
generation/interpolation metadata. Older configurations still load. Export
bundles include the referenced `.xodr` and `.osm` dependencies. To reload an
extracted bundle, upload its map files first, then its configuration. Missing
map references are reported and can be supplied separately. Imagery stays in
the cache rather than the export bundle.

Lanelet2's packaged Python bindings currently target Linux x86_64, matching the
local Docker setup; ROS is not required. The NRW service and source are
[documented by map.nrw](https://map.nrw/dokumentation/latest/). Implementation
status, checks and performance measurements are tracked in [TASKS.md](TASKS.md).

### Develop in VS Code

This repository has one supported local development environment: its
[VS Code Dev Container](https://code.visualstudio.com/docs/devcontainers/containers).
The host only needs:

- [Docker](https://docs.docker.com/get-docker/);
- [Visual Studio Code](https://code.visualstudio.com/); and
- the
  [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).

Open the cloned repository in VS Code and select **Dev Containers: Reopen in
Container** from the command palette. The initial container creation installs
the locked Python environment with `uv`, as well as the Node.js packages and
Chromium required by the browser tests. Python, Conda, Node.js, and project
dependencies do not need to be installed on the host.

After the setup completes, start the application from the VS Code terminal:

```bash
uv run --locked scenario.generator
```

VS Code forwards port 8000 automatically. Open the forwarded URL shown by VS
Code, or use [http://localhost:8000](http://localhost:8000).

## Postprocessing and extensions

Postprocessing steps and metric calculations are designed to be modular. To
add your own postprocessing steps or metrics, see the dedicated README files:
- [`scenario_generator/postprocessing_scripts/`](scenario_generator/postprocessing_scripts/README.md)
- [`scenario_generator/metrics/`](scenario_generator/metrics/README.md)

## Acknowledgments

The foundations of scenario.generator were developed within the following
projects:

<table>
  <thead>
    <tr>
      <th width="170">Project</th>
      <th>Funding source</th>
      <th width="170"></th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td width="170" align="center"><a href="https://www.ika.rwth-aachen.de/de/kompetenzen/projekte/automatisiertes-fahren/4-cad.html"><img src="https://www.ika.rwth-aachen.de/images/projekte/4cad/4cad-logo.svg" alt="4-CAD" height="40" /></a></td>
      <td>Funded by the Deutsche Forschungsgemeinschaft (DFG, German Research Foundation), DFG project no. 503852364.</td>
      <td width="170" align="center"><img src="https://www.ika.rwth-aachen.de/images/foerderer/dfg.svg" alt="Deutsche Forschungsgemeinschaft" height="40" /></td>
    </tr>
    <tr>
      <td width="170" align="center"><a href="https://synergies-ccam.eu/"><img src="assets/synergies.svg" alt="SYNERGIES" height="40" /></a></td>
      <td>Funded by the European Union's Horizon Europe Research and Innovation Programme under Grant Agreement No 101146542. Views and opinions expressed are those of the author(s) only.</td>
      <td width="170" align="center"><img src="assets/funded_by_eu.svg" alt="Funded by the European Union" height="40" /></td>
    </tr>
  </tbody>
</table>

> [!IMPORTANT]
> This repository is open source and maintained by the [Institute for Automotive
> Engineering (ika) at RWTH Aachen University](https://www.ika.rwth-aachen.de/).
> For questions, contact opensource@ika.rwth-aachen.de.

# MADS WebSockets Bridge

`mads-websockets` is a MADS agent that exposes MADS topics over WebSockets and
can also serve a browser UI over plain HTTP. It subscribes to the MADS broker,
forwards matching MADS JSON messages to WebSocket clients, and accepts JSON
published by WebSocket clients back into the MADS network.

The bridge is implemented as a small layered runtime:

- `MadsTransport` handles broker-side I/O through `Mads::Agent`
- `WebSocketTransport` handles client-side I/O through `libwebsockets`
- the embedded web UI lives in [`web`](web) as a single vanilla HTML file and
  is served as static assets by `libwebsockets`
- `BridgeCore` routes validated messages between the two transports
- the FSM in [`src/fsm_agent_impl.hpp`](src/fsm_agent_impl.hpp) manages agent
  lifecycle

## Protocol

The WebSocket API is topic-oriented.

- A client connects to `ws://<host>:<port>/mads/<topic>`
- The WebSocket path selects the MADS topic
- The WebSocket payload is raw JSON, without any envelope

Examples:

- listen to one topic:
  `ws://localhost:8080/mads/perf_assess`
- listen to all topics:
  `ws://localhost:8080/mads/_all`
- publish JSON to one topic:
  connect to `ws://localhost:8080/mads/mytopic` and send the JSON payload

`_all` is a wildcard subscription for receiving data. A connection on
`/mads/_all` receives all MADS topics forwarded by the bridge.

## Requirements

The project expects these libraries to be available on the system:

- Mads
- libwebsockets
- readline

The current CMake configuration finds Mads with `find_package(Mads CONFIG)` and
finds `libwebsockets` through `pkg-config`. The terminal QR helper dependency
`libqrcode` is fetched automatically with CMake `FetchContent`.

## Build

Configure and build with CMake and Ninja:

```bash
cmake -B build -G Ninja
cmake --build build
```

This produces two executables:

- `build/src/mads-websockets`
- `build/src/ws_client`

## Test

Run the unit test target with:

```bash
ctest --test-dir build --output-on-failure
```

## Configuration

The bridge reads its settings from the MADS broker or from a local `mads.ini`
file, depending on how the agent is launched.

A typical settings block is:

```toml
[websockets]
ws_port = 8080
ws_silent = true
sub_topic = [""]
```

Relevant settings:

- `ws_host`: interface to bind, default `0.0.0.0`
- `ws_port`: WebSocket server port, default `9002`
- `ws_path`: WebSocket root path, default `/mads`
- `http_enabled`: serve the browser UI, default `true`
- `http_host`: HTTP bind interface, default `0.0.0.0`
- `http_port`: HTTP UI port, default `8080`
- `http_path`: HTTP UI root path, default `/mads`
- `period`: FSM loop period in milliseconds
- `receive_timeout`: MADS receive timeout in milliseconds
- `ws_silent`: if `true`, suppress `libwebsockets` log messages
- `sub_topic`: MADS subscription topics for the bridge agent

`sub_topic = [""]` subscribes the bridge to all MADS topics.

## Running The Bridge

Typical usage with broker-hosted settings:

```bash
./build/src/mads-websockets tcp://localhost:9092
```

You can also override the agent name:

```bash
./build/src/mads-websockets --name websockets tcp://localhost:9092
```

At startup the agent prints the standard MADS info block, then:

- `Websocket addr: ws://<host>:<port>/mads` for each detected external IPv4
  endpoint
- `HTTP UI addr: http://<host>:<port>/mads` for each detected external IPv4
  endpoint when HTTP is enabled
- `Browser QR:` containing the first detected HTTP UI URL
- `Connected clients: N`

The connected client count is updated in place whenever a client connects or
disconnects.

## Browser UI

The repository includes a browser dashboard for the Huron U-Mill 5 under
[`web`](web). It is a single `index.html` file with inline CSS and JavaScript
(plots and 3-D drawn with canvas and WebGL, no libraries): no build tools, no
Node.js, no internet connection needed. During the CMake build the contents of
`web/` are embedded into the binary and served at `http://<host>:<http_port>/mads`.

The browser page fetches `http://<host>:<http_port>/bootstrap.json` from the
same origin to learn the WebSocket `scheme`, `port`, and `path`, then opens one
WebSocket per topic and reconnects on its own when the bridge restarts. The
dot in the top bar shows the connections (green: all open).

Three views, from the left menu (bottom bar on a phone):

- **Telemetry**: the subscribed topics (comma-separated, saved in the browser),
  one card each, tiled to fill the screen (cards and plots resize with the
  window; with many topics the page scrolls). Every array of up to 12 numbers
  gets a plot, every other number a tile with a sparkline; times and counters
  (`t_us`, `timecode`, `timestamp`, `..._id`) are left out. The Plots button of
  a card hides them; the Message button shows the latest message as a live
  tree, where ◦ next to a number adds it to the selected custom plot. These
  topics are read at 50 messages per second by default.
- **Machine**: a 3-D model of the machine, positioned by the EKF state, with
  the tool-tip path drawn on the rotary table; the cutting/air state; the axis
  positions (readout and plots of X Y Z and A C); the cutting force (|F|, Fx,
  Fy, Fz and their plot); KPI tiles with sparklines; the health of the filter
  with a plot of the NIS. The EKF and KPI topics are read at full rate.
- **Custom**: plots you build. The Fields list holds every number (and
  boolean, as 0/1) that has arrived on the subscribed topics, with its live
  value; arrays are one row that opens into their elements. Drag a field, or a
  whole array, onto a plot, or onto the bar that appears at the bottom to start
  a new plot; or tick it to add it to the selected plot (click a plot to select
  it). `+ New plot` adds an empty one. Each plot holds up to 8 signals, shown as
  chips: × removes one, dragging a chip moves it to another plot. Titles can be
  edited. The plots tile the page and are saved in the browser. A field shows
  its last minute at once: the page keeps a short history of every field.

Every card has an enlarge button (top right of the card, Esc to close). The
plots of the Machine view can also be resized by dragging their bottom-right
corner (the height is remembered). On a plot:

| Action | Effect |
|---|---|
| hover | values of every series under the cursor |
| click | pause / resume |
| drag sideways | scroll back in time (up to 60 s, or 3 time windows) |
| Ctrl + wheel (or pinch) | zoom the time window of that plot |
| double-click | back to live, default window |
| click a legend entry | hide / show that series |

Defaults, all changeable in Settings (gear, top right):

| Setting | Default | |
|---|---|---|
| EKF topic | `eekf_plugin` | |
| Position field | `eekf.position` | X Y Z [mm], A C [deg], array or `{x, y, z, a, c}` |
| Force field | `eekf.force_n` | Fx Fy Fz [N] |
| Cutting flag | `eekf.cutting` | |
| Health object | `eekf` | `nis`, `measurements`, `rejected_updates`, `overruns`, `gaps`, `fmu_rejections` |
| KPI topic | empty = any topic | each number in the KPI object becomes a tile with a sparkline |
| KPI field | `kpi` | |
| Axis directions | X −1, others +1 | the X table moves opposite to the programmed X |
| Tool | 110 mm × Ø16, nose at 260 mm | tip 150 mm above the A axis, as in mt_plugin |

Open the page with `?demo` (`http://<host>:<http_port>/mads?demo`) to run it
on generated data, without the bridge or the machine.

> To enable the browser UI, launch the agent with the option `-w` or `--webserver`.
> 
> **NOTE**: upon launch, the command `mads-websockets -w` presents a QR code that links to the WebUI. Since the latter is exposed on unencrypted http://, though, come smartphone could point the browser to `https://<ip>:8080`, which is not available. If that happens, just manually edit the URL from `https://<ip>:8080` to `http://<ip>:8080` (no 's').
>
> Publishing from the browser is not part of this dashboard: use `ws_client -p`.

## `ws_client`

`ws_client` is a simple CLI tool for testing the bridge.

### Options

- `-a, --address`: base WebSocket address, default `ws://localhost:8080`
- `-t, --topic`: repeatable topic name appended as `/mads/<topic>`
- `-l, --listen`: listen and print incoming JSON to stdout
- `-p, --publish`: read JSON from stdin using `readline` and publish it
- `--debug`: enable `libwebsockets` logs

If no `-t` is given in listen mode, the default topic is `_all`.

Publish mode requires exactly one explicit `-t`.

### Listen Examples

Listen to all topics:

```bash
./build/src/ws_client -a ws://localhost:8080 -l
```

Listen to one topic:

```bash
./build/src/ws_client -a ws://localhost:8080 -t perf_assess -l
```

Listen to multiple selected topics:

```bash
./build/src/ws_client \
  -a ws://localhost:8080 \
  -t perf_assess \
  -t agent_event \
  -l
```

Each topic opens its own WebSocket connection under the same client process.

### Publish Example

Publish to one topic:

```bash
./build/src/ws_client -a ws://localhost:8080 -t mytopic -p
```

Then type JSON payloads at the prompt, for example:

```json
{"value": 42}
```

An empty line exits publish mode.

## Behavior Summary

- MADS messages are forwarded to WebSocket clients as raw JSON
- WebSocket client messages are published into MADS as raw JSON
- one WebSocket connection corresponds to one topic path
- `_all` is supported for wildcard receive subscriptions
- multiple topic subscriptions on the client side are implemented as multiple
  WebSocket connections

## Notes

- CURVE-related CLI flags are accepted by `mads-websockets` for interface
  compatibility with other MADS commands, but bridge-specific crypto handling is
  not implemented here.
- `remote_control` is intentionally not used in this application.

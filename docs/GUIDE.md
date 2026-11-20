# Timesgate Web — Detailed Guide

For a brief overview, see the [project README](../README.md).

A separate, web-only Times Gate controller. No Qt, native desktop application,
Capacitor, mobile build chain, Redis or host cron is required.

## Initial scope

- One authenticated administrator, up to ten configured Times Gate devices.
- Hardware 402: port 9000, `/divoom_api`, per-device numeric `LocalToken`.
- Optional hardware 400 routing: port 80, `/post`; physical testing is pending.
- Five independent screens per device: unmanaged/native, image/GIF, clock, text,
  Strava cycling totals, weather, countdown, RSS/Atom news or custom designs.
- Interval, daily and clock time-change schedules, persisted in SQLite and executed
  in the container.
- YTD/all-time cycling distance, ride count and moving hours;
  metric/imperial units; OAuth, token rotation, cached polling and rate-limit errors.
- HTTP media delivery using `Device/PlayGif`. Normalization creates **opaque
  128x128 full frames**, eliminating the transparency/disposal dependency that
  caused motion trails in the physically tested 402.
- Per-screen previews and a bounded job history. Device errors remain failures;
  uncertain writes are not silently retried.
- Per-screen widget accents: **Electric Cyan**, **Neon Mint**, **Acid Lime**,
  **Solar Gold**, **Tangerine**, **Hot Pink**, **Ultraviolet** and **Glacier Blue**.
  Choose a swatch under **Accent**, then **Preview** or save. The heading, header
  divider, clock day-of-month and existing accent labels use that colour;
  main values stay white. Clock weekday/month labels stay neutral.
  **Original** preserves the previous mint/orange styling and is the default for
  existing configurations. Choices persist on the server independently of the UI
  theme. Image/GIF content and custom-design layer colours are unchanged.
- Four selectable UI themes: **Graphite**, **Porcelain**, **Carbon** and **Paper**.
  Graphite/Carbon use grey-black dark palettes; Porcelain/Paper are light themes.
  Use the **Theme** selector on the sign-in page or page header. It applies
  immediately without saving configuration or discarding edits, and is remembered
  in this browser for this application origin. Graphite is the default.
  Themes change the administration UI, not rendered images sent to the device.

Video conversion, panoramic splitting, playlists, MQTT and native face
editing are not included. Widgets are rendered images, not self-running
device-native faces.

## Architecture

```text
Browser --authenticated--> FastAPI + static web UI :8080
                               |
                          SQLite + worker
                               |
                        Device/PlayGif -----> Times Gate :9000
                               |                   |
                         generated GIFs            | HTTP download
                               v                   v
                         shared media <----- Nginx :8081

                  Strava OAuth/API <---- backend only
```

The backend is one Python process with one background worker. SQLite persists
configuration, due times, cached Strava totals and jobs. A process lock prevents
two controllers from owning the same data directory. Do not run multiple
replicas or Uvicorn workers.

Nginx can read only the media directory. It serves only opaque generated
filenames matching `/[32 lowercase hex characters].gif`; there is no directory
listing, upload handler, application database or credential mount in that service.

## Container image publishing

GitHub Actions tests and publishes the application and media images only when a
GitHub Release is published. Ordinary commits, pushes and tag creation do not
trigger the workflow. Use a release tag such as `v0.1.1` to publish both
`ghcr.io/jaimefordham/timesgate-web:0.1.1` and
`ghcr.io/jaimefordham/timesgate-web-media:0.1.1`.
Stable releases also update both `latest` tags; prereleases do not.
Wait for the workflow to succeed before pulling the images on OMV.

## Start with Docker Compose

For **OpenMediaVault 8 / ARM64**, use the detailed
[OMV deployment guide](OPENMEDIAVAULT.md), including the plugin's Build action.

For an ordinary Docker host with this source checked out:

```sh
cp .env.example .env
mkdir -p data media
sudo chown 10001:10001 data media
sudo chmod 700 data
sudo chmod 755 media
```

Edit `.env`. Set the real browser-facing `APP_BASE_URL`, device-facing
**HTTP** `MEDIA_BASE_URL`, free ports and persistent storage paths. Prefer the
NAS's reserved IPv4 address for device media, rather than a hostname the device
might not resolve. Do not use a Docker service name, localhost, an HTTPS origin,
or a URL that redirects to HTTPS.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Open the configured application URL. On first start, create the single
administrator account with your chosen username and a password of at least
8 characters. No setup code is required. The first visitor can create the account,
so restrict access to your trusted network and complete setup immediately.
Subsequent visits show the username/password sign-in page. There is no public
registration or second account.

### Login and persistence

- Saved configuration, schedules, uploaded media and the Strava connection live
  on the server, not in the browser. Closing the browser or signing out does not
  delete them or stop automatic pushes. Unsaved edits are not persisted.
- Login uses an HttpOnly, SameSite=Strict cookie and a server-side session with
  a fixed 12-hour lifetime. Sessions survive an application restart, but the
  cookie is browser-session-only: closing the browser normally signs you out
  (browser session restoration may retain it). Signing out revokes that session.
- The theme remains a browser-local preference, independent of login.
- Use HTTPS for administration outside localhost. Set `APP_BASE_URL` to the exact
  HTTPS origin so the login cookie is also marked Secure. State-changing API
  requests require the matching `Origin` header.
- Keep the separate HTTP media service **outside any login/reverse-proxy
  authentication**. Device GIF downloads never require administrator credentials.

Existing token-based installations retain their saved settings, but must complete
the one-time account setup after upgrading. `ADMIN_TOKEN` and `admin.token` are no
longer used for authentication; remove the old environment entry if present.

If you forget the password, stop the application and reset only the account:

```sh
# Local installation; the application must be stopped.
.venv/bin/python -m timesgate.auth --reset-admin --data-dir ./data

# Compose installation; leaves the media service running.
docker compose stop app
docker compose run --rm --no-deps app python -m timesgate.auth --reset-admin
docker compose up -d app
```

Restart a local installation and create the administrator again in the web
interface. Restrict access until setup is complete. Resetting revokes all login
sessions but preserves configuration, media and
Strava credentials. The command refuses to run while the controller holds its lock.

### First device update

1. In **Devices**, enter the private IP, hardware 402 and its LocalToken from
   Divoom settings. Save. The token is not returned by the state API.
2. In **Media**, upload a non-sensitive test image/GIF. Source limits:
   8 MiB, 4 megapixels, 300 frames. Non-square content is contained on black.
3. In **Screens**, click one of the five overview screens, then choose its media
   in the focused editor below. Leave the other four unmanaged.
4. Click **Send** and inspect **Activity**, then the physical device.
5. Only after visible playback is confirmed, enable automatic pushes and save.

**Acknowledged means the command was accepted.** The device can acknowledge a
download that subsequently fails. Check the URL from a machine on the device's
network; then check the display itself. No software-only check proves rendering.

### Scheduling and recovery

- **Activity** starts with a **Scheduled updates** table covering every enabled
  screen across all devices. **RGB startup restoration** rows appear first,
  one per configured device, including disabled/not-yet-configured devices.
  Night schedules and screen schedules follow, grouped by device. **Failures only**
  filters both tables to red/error results; interrupted/amber jobs are not errors.
  Error details expand and stay open during polling. Times show relative
  countdowns/ages alongside absolute browser-local timestamps.
  The table shows the widget/task type, interval/daily/time-change
  schedule, next update, last automatic run and its details. Both tables refresh
  every three seconds. Green means the latest automatic command was acknowledged
  (not verified physical playback); amber means awaiting a first run, queued,
  running, interrupted, cancelled or demo-only; red means it failed. Text labels
  accompany the colours, with green/amber/red text in every UI theme and
  contrast-adjusted shades for light themes. Manual sends do not replace automatic-run status.
  The latest automatic result for each enabled screen survives restarts and
  recent-history cleanup. Old results already removed before upgrading cannot
  be recovered; those screens show awaiting their first retained run.
- Schedule and interval/daily-time controls are disabled while automatic pushes
  are off; their values are preserved when toggling automatic pushes.
- Clock screens also offer **On time change**: with automatic pushes enabled,
  updates are due at each local minute boundary in the configured timezone,
  beginning with the next minute after saving. No fixed interval is needed.
  The worker checks once per second; queued work and network/device delays can
  delay playback. Missed minutes coalesce into one current update.
- Intervals are in seconds, minimum ten; daily schedules use the configured IANA
  timezone, including DST. A nonexistent spring-forward time runs at its
  timestamp-normalized time; a repeated fall-back time uses its first occurrence.
- Enabling an interval schedule makes it due immediately. Daily schedules begin
  at the next selected wall-clock time. Saving unchanged configuration does not
  reset its schedule.
- The worker queues one latest update per overdue screen, not an outage backlog.
  Active writes on the same screen are not duplicated by scheduling.
- Screen playback requests are sequential, with at least two seconds between
  completion of one request and the next screen send to the same device IP,
  including after failures. Manual and automatic sends share this spacing;
  it does not change saved schedules or confirm that rendering has finished.
  Other device IPs have independent spacing, though the single worker may still
  delay their jobs while waiting. Demo sends do not wait.
- Device API requests use a five-second connection timeout and a 45-second read
  timeout. These are connection/read inactivity limits, not a total deadline.
  An explicit device-returned `command timeout` is still reported as an error.
- Failed automatic writes remain errors. The next normal scheduled update can
  send the latest content; there are no immediate command retries.
- Pending/running jobs at restart become **interrupted**, with unknown device
  effect where applicable. Old manual writes are never replayed automatically.
- Generated widget files are retained for 24 hours; uploaded media persists.
  Review disk usage and back up the storage directories.
- Keep the containers running for clocks and statistics to refresh. Already
  downloaded GIF animations continue on the device.

### LCD controls and native tools (experimental)

The device command bar on **Screens** and **Tools** has an LCD brightness draft,
**Apply LCD brightness**, **Sleep displays**, **Wake displays**, **Restore layout /
resume**, and a shortcut to RGB lighting. LCD brightness is independent of RGB
brightness. The five-screen overview selects one editor at a time; accent
borders, automatic-run RAG badges and saved/draft labels distinguish configuration,
last results and previews. These are **not live photographs or device readbacks**.

Controls use the saved connection and the same single worker as screen/lighting
jobs. Native controls are based on the source controller but **physical hardware
402 behaviour is unverified**. Test one control at a time on your device.
Acknowledgements do not establish that a setting visibly took effect.
No failed/uncertain command is immediately retried. Preference/time operations
send multiple commands sequentially; Activity reports how many were accepted if
a later command fails. Changing the saved target invalidates queued controls
and hides the old target's runtime state.

- **Sleep displays** requests LCD sleep, not whole-device shutdown or RGB off.
  It enables a persistent screen-send guard *before* attempting the command.
  That guard remains if the response fails or the app restarts.
  **Wake displays** clears it only after acknowledgement and makes enabled screen
  schedules due. Night mode's dim action never implicitly wakes sleeping displays.
- **Tools** offers device-native 12/24-hour format, Celsius/Fahrenheit and mirror
  preferences; those native time/unit settings do not reconfigure rendered widgets.
  **Sync device time** sends current UTC plus the configured IANA timezone's
  current offset, including DST. Firmware timezone format is experimental;
  this manual action does not continuously update the native clock at DST changes.
- Native timer, stopwatch, scoreboard and noise meter may replace multiple
  screens. First takeover requires confirmation; the controller pauses this
  device's screen uploads **before** sending, even if the command later fails.
  Stopping/resetting the tool does not automatically resume uploads.
  Per-screen native targeting and native clock-face selection are deliberately
  not offered because cross-screen effects remain uncertain.
- **Restore layout / resume** requires awake displays, releases the tool pause
  and queues configured managed screens, including unscheduled ones. It does
  not recover unmanaged/native content overwritten by a firmware tool.
- **Play short buzzer** is an explicit, brief sound request.
  **Advanced diagnostics and reboot** contains a one-off settings query and a
  confirmed reboot. Query failure means **status unavailable**, not offline;
  raw device responses/tokens are never exposed. Only allowlisted numeric
  brightness/power fields are displayed when returned. Reboot pauses screen
  uploads until you restore the layout; a device reboot is not an app restart
  and does not trigger startup RGB restoration.

Demo jobs never contact hardware or save physical control acknowledgements.
They do simulate persistent local sleep/tool guards so wake/resume workflows
can be tested. Release those guards before switching the same data to live mode.

### Scheduled night mode

For each device, open **Devices → Scheduled night mode**. Enable it, choose
night/day times and either **Dim LCDs** or **Sleep displays**, set the brightness
levels, and **Save configuration**. It uses the application's IANA timezone,
not the NAS/container timezone. Activation waits for the next selected boundary;
saving does not immediately change the displays.

The day boundary restores day LCD brightness and, in sleep mode, first wakes the
displays so a brightness failure cannot prevent the wake command. An acknowledged
wake clears the sleep guard even if restoring brightness then fails; the failure
remains visible in Activity. RGB lighting is unchanged. Overnight and same-day
windows are both supported; start and end cannot be equal. DST follows the daily-screen scheduling
rules above, with windows compared as absolute instants through the repeated
hour. If spring-forward normalization moves a same-day start beyond its end,
that empty window is skipped rather than sleeping until the next day.
A running controller coalesces missed boundaries into the currently
applicable period, rather than flashing through an outage backlog.
Failed/interrupted automatic commands are not replayed; the next daily boundary
can make its normal attempt. An overdue boundary not yet queued can still run
after recovery. Paused native tools stay paused even if a night wake succeeds.

Manual LCD brightness/power requests supersede waiting night jobs, until the
next boundary. Read-only queries do not cancel night jobs. Changed/disabled
schedules cancel obsolete work before it sends; an already-sent command cannot
be recalled. Night start/end have separate retained RAG rows in Activity.

### RGB lighting

Open **Lighting**, select a saved device, then adjust the colour, brightness,
zone (**All zones**, **Edges**, **Backlight**), effect, power, colour cycling and
key lighting. The twelve named effect cards change with the selected zone.
Colour presets match the widget accents; **Use screen N accent** copies that
saved widget's colour into the lighting preview without changing the widget.
**Solid backlight** selects the rear zone, steady effect and disables cycling.

**Colour swatches** change colour only. **Full lighting presets** include colour,
brightness, zone, effect, power, cycling and key lighting. Built-in Desk, Evening
and Night cards preview complete configurations; save up to 20 named presets,
update a saved preset from the current preview, or delete it. Presets are shared
across devices and persist in private application data. Choosing, saving,
updating or deleting a preset never sends a lighting command or changes
startup-restoration approval: only **Apply lighting** sends the preview.
Draft/matches-last-acknowledgement and restoration enabled/off/hold labels are
shown separately.

The five-screen preview uses saved screen content and illustrates the curved
left/right edge strips separately from the glow behind the device. It is
**not live device feedback**, a simulation of the actual animation, or a
guarantee of secondary-zone colour. Unmanaged screens are placeholders;
unavailable widget previews show their error instead of a fabricated image.
Only clicking **Apply lighting** queues a device command. Moving sliders,
choosing effects/presets or resetting the preview never sends lighting commands.

Lighting uses the authenticated backend and the same single worker/Activity
history as screen sends; it does not require `MEDIA_BASE_URL` or a GIF download.
Last-applied settings are saved only after acknowledgement, persist across
container restarts, and are hidden if the saved device connection changes.
The label means **last acknowledged**, not a readback of current physical state.
Demo mode neither contacts the device nor records settings as physically applied.
There is no lighting interval schedule or immediate retry.
Interrupted jobs are not replayed. Lighting does not change screen configuration,
pause screens or overwrite the scheduled-screen RAG results.

#### Restore lighting on controller startup

After a successful manual **Apply lighting**, enable **Restore lighting when the
controller starts** on the Lighting page. This per-device preference is saved
immediately, defaults to off, and applies to the **next app/container start**.
It restores the last acknowledged configuration, not unsaved lighting previews.
No Compose change or separate cron job is needed.

On startup, old pending/running jobs are marked interrupted before one new
restoration is queued for each eligible, opted-in device. The existing worker
sends it once, independently of screen schedules. Successful acknowledgements
remain visible in Activity even after recent-history cleanup. Device
reconnection polling/restoration is not implemented.

For safety, a failed or interrupted restoration **holds future automatic
attempts**, including after another restart, until a fresh manual Apply succeeds.
Toggling the preference off and on does not clear that hold. Manual success
approves a new lighting configuration but does not count as a successful
automatic restoration; its RAG row awaits the next startup.
Connection changes invalidate the old approval and opt-in until lighting is
manually applied and restoration is enabled for the reviewed connection again.

A newer manual Apply cancels a waiting restoration; turning the preference off
also cancels a waiting job. The worker rechecks approval and preference before
sending. Commands already sent cannot be recalled. Startup jobs are labelled
**Startup restoration** in Activity; manual jobs are labelled **Manual lighting**.
Green means acknowledged, not physically verified. Disabled/unconfigured,
awaiting startup, pending/running, cancelled, interrupted and demo-only states
are amber; failed restorations are red, with a manual-approval explanation.

Power and brightness are global. Zone changes may affect other zones, and
firmware may fix the steady edge colour rather than honouring your chosen RGB
value. Start with **Solid backlight** on one device and verify it physically.
Hardware 400 routing is present but unverified; hardware/firmware-specific RGB
behaviour still requires deployment testing.

The zone/effect catalogue follows the community observations referenced by the
original controller:
[RGB_LIGHTS.md](https://github.com/averhaegen/hacs-divoom-times-gate-dev/blob/e8475a1485e00340646bcd52f39971ad34429990/docs/RGB_LIGHTS.md).
Effect labels describe behaviour; they are not official Divoom names.

### Weather, countdown, news and custom designs

Select the content type in **Screens**, enter its settings and click **Preview**
(which saves first). **Send** and automatic schedules use the same renderer.
Unsaved edits are not fetched as previews of the previous saved content.
Editing one screen does not hide previews on other unchanged screens.

- **Weather:** enter latitude/longitude and select Celsius or Fahrenheit.
  Current temperature, conditions and humidity come from
  [Open-Meteo](https://open-meteo.com/), cached for ten minutes. No API key is
  needed for its free non-commercial service; check its terms for other uses.
  Weather data is provided under
  [CC BY 4.0](https://open-meteo.com/en/terms).
- **Countdown:** choose the target date and time. A target without an offset
  uses the application's IANA timezone; API clients may supply an explicit UTC
  offset. As with daily schedules, DST gaps normalize forward and repeated times
  use the first occurrence. Displays days/hours, hours/minutes or minutes/seconds
  as appropriate, then **FINISHED**. The image only changes on a push, not every
  second; the existing minimum refresh interval is ten seconds.
- **RSS / Atom news:** enter a public HTTP(S) feed URL. Up to 50 headlines are
  cached for five minutes. Set headline rotation time and font size. The headline
  is selected by wall-clock rotation at render time; it does not animate on the
  device. Use an automatic refresh interval no longer than the rotation time;
  delayed pushes can skip headlines.
- **Custom design:** a simple field-based editor, not drag-and-drop. Set the
  background, add up to 20 text/image/bar layers, and edit their X/Y, width and
  height on the 128x128 canvas. **Move up/down** changes list order; later layers
  draw over earlier layers. Content is clipped to the layer and canvas.
  Image layers use the first frame of media uploaded through **Media**.
  Text supports `{time}`, `{date}`, `{cpu}`, `{ram}`, `{disk}`, `{gpu}`,
  `{cpu_temp}`, `{gpu_temp}`, `{download}` and `{upload}`. Use `{{` and `}}` for
  literal braces. Bars use a fixed value or a metric, with a configurable maximum.
  Percent metrics are 0–100; temperatures are Celsius; network rates are bytes/s.
  All fields update when rendered/pushed.

System metrics describe the **server/container**, not the browser's computer.
CPU/RAM/root-disk usage and network rates use psutil. CPU temperature depends on
available OS sensors; GPU usage/temperature require an accessible NVIDIA
`nvidia-smi`. Unsupported metrics show **N/A**; network rates also need a second
sample. Metrics are sampled at most every five seconds. Static designs and
time/date-only designs do not query system metrics.

Weather and feeds require outbound Internet access from the backend. Requests
have size/time limits, validate public destination addresses (including redirects)
and verify HTTPS certificates; localhost/private-network feeds and embedded URL
credentials are not supported. Fetch/parse errors are shown in previews or
Activity, with failed fetches cached for 30 seconds to avoid repeated requests.
Demo mode uses sample weather, headlines and system metrics without contacting
those sources.

### Strava setup

The existing Strava project was inspected read-only. This project carries over
the approach (cycling totals, units, early token refresh and rotation), not live
configuration, tokens, private activity files or logo assets.

1. Register your own application at <https://www.strava.com/settings/api>.
2. Set its **Authorization Callback Domain** to the hostname in `APP_BASE_URL`
   (without scheme, port or path). The UI shows the complete redirect URI:
   `APP_BASE_URL/oauth/strava/callback`.
3. Expand **Plugins** in the sidebar and open **Strava**. Enter the Client ID
   and Client Secret, then save those settings. Plugins starts collapsed and
   keeps its expanded/collapsed state while navigating within the UI.
4. Click **Authorize with Strava**, approve `read` and `activity:read`, and let
   your browser return to this application. The authorization state expires
   after ten minutes and can be used once.
5. The backend polls and caches both periods. Use **Activity** for fetch errors;
   use **Reload** to refresh the displayed connection/data status.
6. Configure a Strava screen and send it after the first successful fetch.

Without cached statistics, Strava previews show dummy totals labelled
**EXAMPLE DATA**, with an explanatory callout in the screen editor. They respect
the selected period and units. Example totals are preview-only: they are never
cached or sent to the device. Once real statistics are cached, previews use those
instead and show their data timestamp.
Both periods emphasize distance in large type, with ride count and whole moving
hours below. Elevation is not displayed. Large totals shrink to fit their column;
metric/imperial settings select kilometres/miles.

Your **browser** must reach the callback URL. The NAS needs outbound HTTPS access
to Strava; Strava does not need inbound access to your NAS for this OAuth flow.
No webhook/public inbound Strava service is required.

Default polling is 30 minutes, minimum 15. Screen refreshes use cached data,
not another Strava API call. Tokens are refreshed five minutes before expiry;
rotated tokens are persisted before fetching totals. HTTP 429 pauses future
polls. Manual refresh respects the next permitted poll time too.

The stats endpoint's totals are Strava's own roll-ups; do not assume they equal
every activity visible in the Strava app. This version does not aggregate private
activities or filter individual sport subtypes. Official Strava logo assets are
not bundled. Check Strava's current API terms and attribution requirements before
publishing or extending beyond personal use.

### Privacy and boundaries

- Administration uses a single username/password account and HttpOnly session
  cookies; keep it LAN-only or behind an HTTPS reverse proxy. Plain HTTP
  administration exposes passwords and session cookies on the network.
  Five failed sign-in attempts within a minute temporarily block further
  attempts. Administrator passwords use salted scrypt hashes. The server stores
  only SHA-256 digests of session tokens; credentials are never placed in media
  links or JavaScript-accessible browser storage.
- Device-facing GIFs are **unauthenticated HTTP**, including rendered Strava
  totals. Unguessable names reduce accidental discovery; they are not access
  control. Restrict the media port by routing/firewall to trusted clients/devices.
- SQLite stores device and Strava credentials **unencrypted**. The private data
  directory must not be SMB/web published. Protect backups the same way.
- State endpoints omit device LocalTokens, Strava secrets and OAuth tokens.
  Access logs are disabled to avoid logging OAuth callback codes.
- Device writes accept only configured private IPv4 targets and generated HTTP
  media URLs. The UI does not provide an arbitrary command or URL proxy.
- Uploaded images are decoded locally, not downloaded from user-supplied URLs.
  Weather/feed requests are backend-only, bounded public-Internet reads; they
  never carry administrator or device credentials.
- Use one controller per device; avoid running multiple controllers against the
  same screens while testing.

## Local development (no Docker)

Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
DEMO=1 APP_BASE_URL=http://localhost:8080 \
  .venv/bin/python -m uvicorn timesgate.app:create_app --factory \
  --host 127.0.0.1 --port 8080 --workers 1 --no-access-log --no-proxy-headers
```

For local testing, standardize on **http://localhost:8080** in the browser and
`APP_BASE_URL=http://localhost:8080`. Do not substitute `127.0.0.1` in the
browser URL: origin protection treats it as a different origin. The server
can still bind to `127.0.0.1` as shown above.

On first start, create the administrator through the setup page with a password
of at least 8 characters; no setup code is required. Demo mode renders previews and processes queued
jobs **without contacting a device, Strava or widget sources**. There is deliberately no built-in
public media route on the admin server. For live URL playback, run the Compose
media service and set `MEDIA_BASE_URL` before starting the application.
Local `.env` files are read by Compose, not automatically by Python/Uvicorn.
After updating dependencies, rerun `.venv/bin/python -m pip install -e '.[test]'`
and restart the backend before refreshing the browser.

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check timesgate/static/app.js
```

No Node packages or frontend build step are needed; the UI is ES modules and CSS.
Authentication and API regression tests can be run separately with
`.venv/bin/python -m unittest tests.test_auth tests.test_app -v`.

## Validation status

Local validation (Python 3.15 on macOS): 66 focused regression tests covering
device controls, night-mode DST edges, lighting/restoration, scheduling, API/auth
and widget rendering, plus JavaScript syntax checks. Earlier checks also covered
dependencies and wheel packaging. Browser demo checks cover sign-in, device/token persistence,
upload normalization, clock preview, manual/scheduled demo jobs and a 390px layout.
The new widgets have API preview/send coverage; browser checks cover all four
previews, custom text/image/bar editing, layer ordering, persistence and disabled
scheduling controls at 390px. Live public weather and RSS sample reads were
also checked without using personal configuration.
The original 69-frame test GIF also preserves every decoded pixel and frame time
through normalization while producing complete opaque frame descriptors.
Device-control demo checks additionally cover focused screen editing without
implicit saves, sleep/wake and tool pause/resume, full lighting preset persistence
without sending, night-setting persistence, restoration-first Activity, failure
filtering and expanded error details. Screens, Tools, Lighting, Devices and
Activity fit desktop and 390px viewports across all four themes.

Docker Engine is not installed in the development environment, so actual
container builds, OMV 8 installation and end-to-end NAS/device/Strava OAuth
verification must be performed on the deployment host. ARM64 compatibility is
designed into the Python/Nginx images; it is not an ARM64 deployment certification.

This source is initialized as an independent local Git repository. No remote,
public repository, image publication or copied credential files are configured.

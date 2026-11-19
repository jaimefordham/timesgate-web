# Timesgate Web

A self-hosted web controller for Divoom Times Gate displays. Manage devices,
design screens and schedule updates from your browser, without a desktop app.

## Features

- Up to ten devices, each with five independently configured screens.
- Images/GIFs, clocks, text, weather, countdowns, RSS/Atom headlines and custom
  layered designs.
- Strava cycling totals with year-to-date/all-time views and metric/imperial units.
- Per-screen previews, automatic schedules and activity history.
- Compact five-screen overview with one focused editor, and grouped/filterable
  Activity with relative and absolute run times.
- Experimental LCD brightness, display sleep/wake, timezone-aware night schedules,
  native preferences, timer/stopwatch/scoreboard/noise tools, buzzer and diagnostics.
- Manual RGB lighting with zone-aware effects, colour presets and a five-screen
  preview, plus opt-in restoration on controller startup. Physical lighting
  behaviour depends on device firmware.
- Full lighting presets saved separately from colour swatches; preview before Apply.
- Four UI themes and eight widget accent colours.
- A single administrator login, with settings persisted server-side in SQLite.

Hardware 402 is supported; hardware 400 routing is included but awaits physical
testing. Widgets are rendered images, not device-native faces.
Native controls are experimental and unverified on physical hardware 402.
Sleep guards and native-tool pauses block screen uploads until wake/resume.

## Getting started

Docker Compose is the recommended deployment:

1. Copy `.env.example` to `.env` and configure application/media URLs, ports and
   persistent storage.
2. Follow the [Docker Compose setup](docs/GUIDE.md#start-with-docker-compose)
   to prepare storage, start the services and create your administrator account.
3. Add a device, preview a screen and send a test image before enabling schedules.

For **OpenMediaVault 8 / ARM64**, use the
[OMV deployment guide](docs/OPENMEDIAVAULT.md).

For a device-free demo, follow the
[local development instructions](docs/GUIDE.md#local-development-no-docker)
and open **http://localhost:8080**.

## Deployment essentials

- Use HTTPS for administration outside localhost.
- Keep device media on its separate **HTTP** service, reachable by the device
  and **not protected by a login page**. Media URLs are unauthenticated; restrict
  access to your trusted network.
- Protect persistent storage and backups: device and Strava credentials are
  stored unencrypted.
- Run one controller process with one worker; multiple replicas are unsupported.

## Documentation

The [detailed guide](docs/GUIDE.md) covers setup, login and account recovery,
scheduling, widgets, Strava, privacy, development and validation limitations.

Container builds, OMV/ARM64 deployment and end-to-end NAS/device/Strava OAuth
verification still need validation on the deployment host.

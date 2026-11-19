# OpenMediaVault 8 / ARM64 deployment

This guide targets the OMV 8 **openmediavault-compose** plugin. Menu names were
checked against the current
[OMV 8 Compose documentation](https://wiki.omv-extras.org/doku.php?id=omv8:omv8_plugins:docker_compose).
Minor labels can change with plugin updates. This procedure has not yet been
executed on a physical OMV host.

## 1. Prepare OMV

1. Back up your NAS configuration before changing installed plugins.
2. Follow [Docker in OMV 8](https://wiki.omv-extras.org/doku.php?id=omv8:docker_in_omv)
   to install OMV-Extras, the Compose plugin and Docker.
3. Keep the `openmediavault-sharerootfs` dependency installed.
4. Under **Services > Compose > Settings**, configure:
   - **Compose Files**: an existing or new shared folder for stack definitions.
   - **Data**: your application-data shared folder.
   - **Backup**: a separate backup destination and suitable maximum volume size.
5. Save and apply pending OMV changes.
6. Reserve a stable NAS IP and Times Gate IP in your router.

Do not replace an existing Docker storage location or reconfigure working stacks
just to install this app. OMV's administration normally owns port 80/443; this
stack uses 8080 and 8081 by default.

## 2. Choose persistent paths

Obtain the real absolute shared-folder path from OMV. The examples below use:

```text
/srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/
    source/     application source (not credentials)
    data/       PRIVATE: SQLite database and credentials
    media/      HTTP-PUBLISHED: normalized GIFs only
```

**Replace `REPLACE` with your actual disk UUID/path in every command.**
Do not use the literal example path on your NAS.

The application and media containers run as numeric UID/GID **10001:10001**;
you do not need a matching interactive NAS account. Verify that choice does not
conflict with your local ownership policy. Create only the scoped application
directories over SSH:

```sh
sudo install -d -m 755 /srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/source
sudo install -d -o 10001 -g 10001 -m 700 /srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/data
sudo install -d -o 10001 -g 10001 -m 755 /srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/media
```

Keep `data` out of SMB shares and HTTP servers. Its parent may be your appdata
shared folder, but do not grant other users recursive access. Do not solve
permissions by applying `chmod 777` or by changing ownership of the entire disk.
Existing ACLs can override Unix modes; review the target directories' OMV ACLs.

## 3. Transfer this source to the NAS

There is no published application image or Git remote yet. Copy **this new
project only**, not the old controller or private Strava project.

From your Mac, in the new project's folder:

```sh
tar --exclude=.git --exclude=.venv --exclude=.env --exclude=data --exclude=media \
  --exclude='*.egg-info' --exclude=__pycache__ --exclude=build --exclude=dist \
  -czf /tmp/timesgate-web-source.tgz .
scp /tmp/timesgate-web-source.tgz YOUR_SSH_USER@NAS_IP:/tmp/
```

On the NAS, extract into the dedicated source directory:

```sh
sudo tar -xzf /tmp/timesgate-web-source.tgz \
  -C /srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/source
```

Verify that this file exists on the NAS:

```text
/srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/source/deploy/nginx.conf
```

Using an absolute `SOURCE_DIR` below avoids a common problem: OMV stores its
Compose YAML in the plugin's stack folder, not in this source directory.

## 4. Create the stack in the OMV GUI

1. Open **Services > Compose > Files**.
2. Choose **Create / Add** and name the stack `timesgate-web`.
3. Paste the complete contents of this project's `compose.yaml` into the YAML
   editor. Do not substitute the Dockerfile; these are different files.
4. Paste the following into the stack's **Environment / .env** editor, replacing
   every example value. Store it per stack rather than changing global variables:

```dotenv
SOURCE_DIR=/srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/source
DATA_DIR=/srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/data
MEDIA_DIR=/srv/dev-disk-by-uuid-REPLACE/appdata/timesgate-web/media
ADMIN_BIND=192.168.1.100
MEDIA_BIND=192.168.1.100
ADMIN_PORT=8080
MEDIA_PORT=8081
APP_BASE_URL=http://192.168.1.100:8080
MEDIA_BASE_URL=http://192.168.1.100:8081
TZ=Europe/Lisbon
```

5. Save the stack.
6. Select it and choose **Check**. Resolve missing-variable or path errors.
7. Choose **Tools > Build** (or **Pull and build**) for this stack.
   This builds `timesgate-web:0.1.0` locally on your ARM64 NAS. The Python base
   image and Pillow support ARM64; no x86 platform override is needed.
8. Choose **Up**.
9. Use **Ps** to inspect both services, and **Tools > Logs / Follow logs** for errors.

**Pull alone cannot obtain this application image:** it has not been published.
Build it locally first. Nginx is a public multi-architecture upstream image and
can be pulled normally. Avoid **Down all**, **Prune volumes** or unrelated stack
actions.

OMV only fully manages stacks created/imported through its GUI. Do not initially
start a duplicate stack with `docker compose up` over SSH.

## 5. Sign in and test network reachability

Open the application URL and create the single administrator username and
password (at least 8 characters). No setup code is required: the first visitor
can create the account, so restrict access to your trusted network and complete
setup immediately. Later visits use the username/password sign-in page.
Do not publish your password in screenshots or paste it into chat. Upgrading from token
authentication also requires this setup, without deleting saved settings.

Use HTTPS for administration when possible, with the exact HTTPS origin in
`APP_BASE_URL`. Keep the separate device media service on HTTP without a login
page or authentication proxy. Logging out does not stop media downloads or
automatic pushes. Saved settings and Strava authorization persist server-side.

For a forgotten password, stop only this stack's **app** service, then run over
SSH from the stack's Compose directory:

```sh
docker compose run --rm --no-deps app python -m timesgate.auth --reset-admin
```

Start **app** again in OMV and create the administrator in the web interface.
Restrict access until setup is complete. This resets the account and its login
sessions only; settings, media and Strava credentials remain.

Open `http://NAS_IP:8080` using the exact origin in `APP_BASE_URL`. You can check
the separate endpoints:

```sh
curl --fail http://NAS_IP:8080/healthz
curl --fail http://NAS_IP:8081/healthz
```

The second check tests the media server, not whether the Times Gate can reach it.
Upload a test image in **Media**, configure **one** screen and send it; inspect
both **Activity** and the physical device before enabling schedules.

### Firewall / VLAN checklist

| Direction | Required traffic |
|---|---|
| Browser to NAS | Admin TCP 8080 (or your chosen HTTPS proxy) |
| NAS app container to Times Gate | TCP 9000 for 402; TCP 80 for 400 |
| Times Gate to NAS | TCP 8081, HTTP GET/HEAD for GIFs |
| NAS app to Strava | Outbound TCP 443 + DNS |
| NAS to image/package registries | Outbound access during builds/updates |

Use the **NAS LAN address**, not `media:8081` or a Docker bridge address, in
`MEDIA_BASE_URL`. A browser-accessible media URL is insufficient if device VLAN
rules block it. Host networking, privileged mode and exposing Docker's socket are
not required.

Do not put the media port behind an HTTP-to-HTTPS redirect or browser login. The
device cannot supply your admin bearer token. Restrict the port by firewall
instead. The media directory may contain Strava totals, so **do not forward this
port to the Internet**.

If the Times Gate is on a different network, arrange explicit routing/firewall
access to this HTTP service. This application does not create a tunnel or upload
files to an external server.

## 6. Connect Strava

Follow the [Strava section](../README.md#strava-setup). Register the hostname of
`APP_BASE_URL` in the Strava developer settings, then authorize from your browser.
You do not need an inbound Internet port for Strava webhooks. Never copy the
existing private project's live config/token JSON into the source or media folder.

For HTTPS administration, terminate TLS in your existing reverse proxy, route
it to app port 8080, and set `APP_BASE_URL` to that exact HTTPS origin. Keep
`MEDIA_BASE_URL` as a separately reachable plain HTTP origin.

## 7. Updates

1. Back up first.
2. Stop the selected stack in **Services > Compose > Files > Stop**.
3. Replace files in its `source` folder, preserving the separate `data` and
   `media` directories and the stack's environment values.
4. If `compose.yaml` changes, update the plugin's stored YAML too.
   Files changed outside OMV can require **Sync changes from file**.
5. Run **Check**, **Tools > Build**, then **Up**.
6. Check health, Activity, schedules and one visible device update.

If your plugin does not offer **Build**, SSH into the NAS, change to the exact
`SOURCE_DIR` directory, and run:

```sh
sudo docker build -t timesgate-web:0.1.0 .
```

After a successful build, choose **Up** for the existing stack in OMV. Restart
alone does not install changed source. Keep `DATA_DIR` and `MEDIA_DIR` unchanged;
existing accounts and settings remain. Refresh the browser after the update.

Restart policy is `unless-stopped`: services return after a NAS restart unless
you deliberately stopped them. Scheduling lives in the app container, so do not
add duplicate cron jobs in OMV.

## 8. Backup and restore

- Use the plugin's backup facility or your existing NAS backup system.
- For a consistent manual backup, **stop both services**, then copy the entire
  private `data` and published `media` directories together.
- Include the stack YAML/environment and the matching source version.
- The SQLite database includes schedules, tokens and Strava totals. Backups are
  sensitive; encrypt or otherwise protect them.
- OMV backup defaults can exclude volumes over 1 GB. Check the configured size
  threshold and verify both directories are actually included; absolute bind
  mounts outside the Compose folder need particular attention.
- Restore to the same dedicated paths, restore UID/GID 10001 ownership, keep
  `data` mode 700 and `media` mode 755, then build/up the stack.
- If an administrator token was supplied through the environment, restore that
  value too; an old generated token file does not override it.

After recovery, the worker coalesces overdue schedules into current updates.
Interrupted jobs are not replayed. A stored Strava refresh token may need
reauthorization if it was revoked or rotated after the backup.

## Troubleshooting

| Symptom | Check |
|---|---|
| Build context or Nginx file missing | Absolute `SOURCE_DIR`, source transfer, stack environment |
| Bind-mount error | Pre-create the exact `DATA_DIR` and `MEDIA_DIR` paths |
| SQLite permission denied | UID/GID 10001, data directory modes and inherited ACLs |
| Browser says unexpected origin | Exact scheme/host/port in `APP_BASE_URL` |
| Playback acknowledged but loading hourglass | Device route to `MEDIA_BASE_URL`, DNS, firewall, HTTP redirect |
| GIF leaves trails | Confirm file was uploaded through this app's normalizer, not manually copied |
| Device command timeout | Check device/cloud connectivity and competing senders; do not assume success or blindly retry |
| Clock stops updating | App container health, enabled screen schedule, Activity errors |
| Strava data stays old | Connection/scopes, next poll time, Activity/API errors; cached timestamp is displayed |

ARM64 container startup and physical OMV/device behavior still require your
deployment validation; the application does not claim that a local unit test
certifies those environments.

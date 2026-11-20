const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

function fixture() {
  const lighting = {
    color: "#55e6b0", brightness: 50, zone: 2, effect: 5,
    on: true, cycle: false, keys: true,
  };
  const devices = ["a", "b"].map((letter, index) => ({
    id: letter.repeat(32), name: `Device ${index}`, host: `192.168.1.${10 + index}`,
    screens: Array.from({length: 5}, () => ({kind: "text", text: "Saved"})),
    night: {enabled: false, start: "22:00", end: "07:00"},
  }));
  const snapshots = devices.map((device, index) => ({
    id: String(index).repeat(32), name: `Profile ${index}`, device_id: device.id,
    screens: structuredClone(device.screens), night: structuredClone(device.night),
    lighting: structuredClone(lighting),
  }));
  const server = {
    config: {devices, timezone: "UTC"}, revision: "initial",
    profiles: snapshots.map(({id, name, device_id}) => ({id, name, device_id})),
    active_profiles: Object.fromEntries(snapshots.map(profile => [profile.device_id, profile])),
    lighting: {}, lighting_defaults: lighting,
  };
  const banners = devices.map(device => ({
    dataset: {loadedProfile: device.id}, textContent: "",
    classList: {toggle() {}},
  }));
  const writes = [];
  let failure = false;
  const context = vm.createContext({
    structuredClone, Blob,
    window: {addEventListener() {}},
    document: {
      querySelector: () => null,
      querySelectorAll: selector => selector === "[data-loaded-profile]" ? banners : [],
      addEventListener() {},
    },
    localStorage: {getItem: () => null},
    setInterval() {},
    fetch: async (url, options) => {
      if (options.method === "PUT") {
        writes.push(JSON.parse(options.body));
        if (failure) return {ok: false, status: 400, json: async () => ({error: "Profile save failed"})};
        const values = writes.at(-1);
        server.config = structuredClone(values.config);
        server.active_profiles = Object.fromEntries(values.profiles.map(profile => [profile.device_id, profile]));
        server.revision = "saved";
        return {ok: true, json: async () => ({revision: server.revision})};
      }
      assert.equal(url, "/api/state");
      return {ok: true, json: async () => structuredClone(server)};
    },
  });
  const source = readFileSync(join(__dirname, "../timesgate/static/app.js"), "utf8");
  vm.runInContext(source.replace(/^await start\(\);$/m, ""), context);
  const app = vm.runInContext(`render = () => {};
    ({
      load, save, updateLoadedProfileStatus,
      screen(device, text) { config.devices[device].screens[0].text = text; },
      lighting(device, brightness) { lightingDrafts.get(config.devices[device].id).brightness = brightness; },
      rename(name) { state.profiles[0].name = name; },
      remove() { state.profiles.shift(); },
    })`, context);
  return {app, server, banners, writes, fail: () => { failure = true; }};
}

test("loaded names track screen and lighting modifications independently per device", async () => {
  const {app, banners} = fixture();
  await app.load();
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0");
  app.screen(0, "Edited");
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0 · Modified");
  assert.equal(banners[1].textContent, "Loaded profile: Profile 1");
  app.screen(0, "Saved");
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0");
  app.lighting(0, 25);
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0 · Modified");
});

test("Save submits config and each loaded profile together, then refreshes saved baselines", async () => {
  const {app, banners, writes} = fixture();
  await app.load();
  app.screen(0, "Edited");
  app.lighting(0, 25);
  app.lighting(1, 75);
  await app.save();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].profiles.length, 2);
  assert.equal(writes[0].profiles[0].screens[0].text, "Edited");
  assert.equal(writes[0].profiles[0].lighting.brightness, 25);
  assert.equal(writes[0].profiles[1].lighting.brightness, 75);
  assert.equal(writes[0].config.devices[0].screens[0].text, "Edited");
  assert.equal(writes[0].profiles[0].host, undefined);
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0");
  assert.equal(banners[1].textContent, "Loaded profile: Profile 1");
});

test("failed Saves retain modified profile drafts for retry", async () => {
  const {app, banners, fail} = fixture();
  await app.load();
  app.screen(0, "Keep this draft");
  fail();
  await assert.rejects(app.save(), /Profile save failed/);
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Profile 0 · Modified");
});

test("renames update the label and deleted profiles cannot be silently recreated by Save", async () => {
  const {app, banners, writes} = fixture();
  await app.load();
  app.rename("Evening");
  app.updateLoadedProfileStatus();
  assert.equal(banners[0].textContent, "Loaded profile: Evening");
  app.remove();
  app.updateLoadedProfileStatus();
  assert.match(banners[0].textContent, /Deleted/);
  await assert.rejects(app.save(), /no longer exists/);
  assert.equal(writes.length, 0);
});

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { test } = require("node:test");
const updateReleaseNotes = require("../.github/scripts/release-notes.cjs");

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), "timesgate-release-notes-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const git = args => execFileSync("git", args, {
    cwd: directory, encoding: "utf8", stdio: "pipe",
  });
  git(["init", "--quiet"]);
  git(["config", "user.name", "Release tests"]);
  git(["config", "user.email", "release-tests@example.invalid"]);
  const commit = message => {
    git(["commit", "--quiet", "--allow-empty", "-m", message]);
    return git(["rev-parse", "HEAD"]).trim();
  };
  return { git, commit };
}

function api(release, releases) {
  const updates = [];
  const github = {
    rest: {
      repos: {
        getRelease: async () => ({ data: release }),
        listReleases: () => {},
        updateRelease: async update => {
          updates.push(update);
          release.body = update.body;
        },
      },
    },
    paginate: async () => releases,
  };
  const context = {
    repo: { owner: "example", repo: "timesgate-web" },
    serverUrl: "https://github.com",
    payload: { release: { id: release.id, body: "Stale event description" } },
  };
  return { github, context, updates };
}

test("includes only commits since the previous published release, with full bodies", async t => {
  const { git, commit } = fixture(t);
  commit("Previously released work");
  git(["tag", "v0.1.0"]);
  const hash = commit("Add widget accents\n\nKeep the numbers white.\n\nCo-authored-by: Helper <helper@example.invalid>");
  git(["tag", "unpublished-tag"]);
  commit("Fix previews\n\nHandle <example> data.");
  git(["tag", "-a", "v0.2.0", "-m", "Release"]);
  const release = {
    id: 3, tag_name: "v0.2.0", published_at: "2026-11-20T12:00:00Z",
    body: "My introduction.\n\n## Upgrade instructions\nKeep your saved settings.",
  };
  const state = api(release, [
    { id: 4, tag_name: "future", published_at: "2026-11-21T12:00:00Z" },
    { id: 2, tag_name: "unpublished-tag", draft: true, published_at: null },
    { id: 0, tag_name: "older-release", published_at: "2026-11-18T12:00:00Z" },
    { id: 1, tag_name: "v0.1.0", published_at: "2026-11-19T12:00:00Z" },
    release,
  ]);

  await updateReleaseNotes(state, git);
  assert.equal(state.updates.length, 1);
  assert.ok(release.body.startsWith("My introduction.\n\n## Upgrade instructions\nKeep your saved settings."));
  assert.match(release.body, /## Changes since v0\.1\.0/);
  assert.match(release.body, /Keep the numbers white\./);
  assert.match(release.body, /Handle &lt;example&gt; data\./);
  assert.ok(release.body.includes(`https://github.com/example/timesgate-web/commit/${hash}`));
  assert.ok(release.body.indexOf("Add widget accents") < release.body.indexOf("Fix previews"));
  assert.doesNotMatch(release.body, /Previously released work|Co-authored-by|Stale event description/);
});

test("first release includes its history, but not commits after its tag", async t => {
  const { git, commit } = fixture(t);
  commit("Initial application");
  git(["tag", "unpublished-tag"]);
  commit("First improvements");
  git(["tag", "v0.1.0"]);
  commit("Future work");
  const release = { id: 1, tag_name: "v0.1.0", published_at: "2026-11-20T12:00:00Z", body: null };
  const state = api(release, [release]);

  await updateReleaseNotes(state, git);
  assert.match(release.body, /Changes in this first release/);
  assert.match(release.body, /Initial application/);
  assert.match(release.body, /First improvements/);
  assert.doesNotMatch(release.body, /Future work/);
});

test("reruns replace generated notes without duplicates and preserve surrounding edits", async t => {
  const { git, commit } = fixture(t);
  commit("Initial application");
  git(["tag", "v0.1.0"]);
  const release = { id: 1, tag_name: "v0.1.0", published_at: "2026-11-20T12:00:00Z", body: "Introduction" };
  const state = api(release, [release]);

  await updateReleaseNotes(state, git);
  release.body += "\n\nManually added footer.";
  const body = release.body;
  await updateReleaseNotes(state, git);
  assert.equal(release.body, body);
  assert.equal(state.updates.length, 1);

  release.body = release.body.replace("Initial application", "Outdated generated text");
  await updateReleaseNotes(state, git);
  assert.equal(state.updates.length, 2);
  assert.equal(release.body, body);
  assert.equal(release.body.split("timesgate-commit-notes:start").length, 2);
});

test("releases at the same commit report no new commits", async t => {
  const { git, commit } = fixture(t);
  commit("Initial application");
  git(["tag", "v0.1.0"]);
  git(["tag", "v0.1.1"]);
  const release = { id: 2, tag_name: "v0.1.1", published_at: "2026-11-20T12:00:00Z", body: "" };
  const state = api(release, [
    { id: 1, tag_name: "v0.1.0", published_at: "2026-11-19T12:00:00Z" },
    release,
  ]);

  await updateReleaseNotes(state, git);
  assert.match(release.body, /_No new commits\._/);
  assert.doesNotMatch(release.body, /Initial application/);
});

test("missing previous tags and broken markers fail without changing the release", async t => {
  const { git, commit } = fixture(t);
  commit("Initial application");
  git(["tag", "v0.1.0"]);
  const release = { id: 2, tag_name: "v0.1.0", published_at: "2026-11-20T12:00:00Z", body: "Introduction" };
  const state = api(release, [
    { id: 1, tag_name: "missing-tag", published_at: "2026-11-19T12:00:00Z" },
    release,
  ]);
  await assert.rejects(updateReleaseNotes(state, git), /rev-parse/);
  assert.equal(state.updates.length, 0);

  state.github.paginate = async () => [release];
  release.body = "Introduction\n<!-- timesgate-commit-notes:start -->";
  await assert.rejects(updateReleaseNotes(state, git), /invalid commit-note markers/);
  assert.equal(state.updates.length, 0);
});

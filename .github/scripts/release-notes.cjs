const { execFileSync } = require("node:child_process");

const START = "<!-- timesgate-commit-notes:start -->";
const END = "<!-- timesgate-commit-notes:end -->";

function runGit(args) {
  return execFileSync("git", args, {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
}

function escapeHtml(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function replaceNotes(body, block) {
  const start = body.indexOf(START);
  const end = body.indexOf(END);
  if (start === -1 && end === -1) {
    return body ? `${body}\n\n${block}` : block;
  }
  if (
    start === -1 || end < start ||
    body.indexOf(START, start + START.length) !== -1 ||
    body.indexOf(END, end + END.length) !== -1
  ) {
    throw new Error("Release description contains invalid commit-note markers");
  }
  return body.slice(0, start) + block + body.slice(end + END.length);
}

module.exports = async function updateReleaseNotes({ github, context }, git = runGit) {
  const repo = context.repo;
  const { data: release } = await github.rest.repos.getRelease({
    ...repo,
    release_id: context.payload.release.id,
  });
  if (!release.published_at || release.draft) {
    throw new Error("Commit notes require a published release");
  }

  const releases = await github.paginate(github.rest.repos.listReleases, {
    ...repo,
    per_page: 100,
  });
  const publishedAt = Date.parse(release.published_at);
  const previous = releases
    .filter(candidate => {
      if (candidate.draft || !candidate.published_at || candidate.id === release.id) {
        return false;
      }
      const date = Date.parse(candidate.published_at);
      return date < publishedAt || (date === publishedAt && candidate.id < release.id);
    })
    .sort((a, b) =>
      Date.parse(b.published_at) - Date.parse(a.published_at) || b.id - a.id
    )[0];

  const currentCommit = git([
    "rev-parse", "--verify", `refs/tags/${release.tag_name}^{commit}`,
  ]).trim();
  const previousCommit = previous && git([
    "rev-parse", "--verify", `refs/tags/${previous.tag_name}^{commit}`,
  ]).trim();
  const range = previousCommit ? `${previousCommit}..${currentCommit}` : currentCommit;
  const output = git(["log", "--reverse", "-z", "--format=%H%x00%B", range, "--"]);
  const fields = output ? output.split("\0") : [];
  if (fields.at(-1) === "") fields.pop();
  if (fields.length % 2 !== 0) {
    throw new Error("Unexpected commit-message output from Git");
  }

  const entries = [];
  const repositoryUrl = `${context.serverUrl}/${repo.owner}/${repo.repo}`;
  for (let index = 0; index < fields.length; index += 2) {
    const hash = fields[index];
    const lines = fields[index + 1]
      .split(/\r?\n/)
      .filter(line => !/^Co-authored-by:/i.test(line.trim()))
      .join("\n")
      .trim()
      .split("\n");
    const subject = escapeHtml(lines.shift());
    const details = lines.map(line => `  ${escapeHtml(line)}`).join("\n").trimEnd();
    const heading = `- ${subject} ([${hash.slice(0, 7)}](${repositoryUrl}/commit/${hash}))`;
    entries.push(details ? `${heading}\n${details}` : heading);
  }

  const heading = previous
    ? `## Changes since ${escapeHtml(previous.tag_name)}`
    : "## Changes in this first release";
  const block = `${START}\n${heading}\n\n${entries.join("\n\n") || "_No new commits._"}\n${END}`;
  const body = replaceNotes(release.body || "", block);
  if (body !== (release.body || "")) {
    await github.rest.repos.updateRelease({ ...repo, release_id: release.id, body });
  }
};

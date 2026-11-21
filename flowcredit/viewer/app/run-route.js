// Pure hash-route helpers for run and audit deep links.
//
// Nothing in this module touches the DOM or history: every export is a pure
// string transform, so it can be exercised directly from node:test.
//
// Route shapes:
//   #/audit            -> { kind: "audit", dir: "" }
//   #/audit/<dir>      -> { kind: "audit", dir: <decoded dir> }
//   #/run/<id>         -> { kind: "run", id: <decoded id> }
//   #/run/ #/run empty -> { kind: "run", id: "", invalid: true }
//   "" / anything else -> { kind: "home" }

const RUN_PREFIX = "#/run/";
const RUN_PATH = "/run";
const AUDIT_PATH = "/audit";

function normalizeHash(hash) {
  if (typeof hash !== "string") return "";
  let raw = hash.trim();
  if (raw.startsWith("#")) raw = raw.slice(1);
  return raw;
}

function decodeId(raw) {
  try {
    return { ok: true, value: decodeURIComponent(raw) };
  } catch {
    return { ok: false, value: raw };
  }
}

function invalidRun() {
  return { kind: "run", id: "", invalid: true };
}

/** Build the canonical deep link for a run id, URL-encoding the id. */
export function runHashFor(runId) {
  const id = runId === null || runId === undefined ? "" : String(runId);
  return RUN_PREFIX + encodeURIComponent(id);
}

/** True only when the hash addresses the audit surface. */
export function isAuditHash(hash) {
  return parseHash(hash).kind === "audit";
}

/** Classify a location.hash into home / audit / run (optionally invalid). */
export function parseHash(hash) {
  const path = normalizeHash(hash);
  if (path === "") return { kind: "home" };

  // Audit wins over everything else, so #/audit cannot be stolen by run logic.
  if (path === AUDIT_PATH) return { kind: "audit", dir: "" };
  if (path.startsWith(AUDIT_PATH + "/")) {
    const rest = path.slice(AUDIT_PATH.length + 1);
    const decoded = decodeId(rest);
    return { kind: "audit", dir: decoded.ok ? decoded.value : rest };
  }

  if (path === RUN_PATH) return invalidRun();
  if (path.startsWith(RUN_PATH + "/")) {
    const rest = path.slice(RUN_PATH.length + 1);
    if (rest.trim() === "") return invalidRun();
    const decoded = decodeId(rest);
    if (!decoded.ok || decoded.value.trim() === "") return invalidRun();
    return { kind: "run", id: decoded.value };
  }

  return { kind: "home" };
}

export default { parseHash, runHashFor, isAuditHash };

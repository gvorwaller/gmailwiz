// drafts_trigger_gmailwiz.js — Drafts (iOS/macOS) action that triggers a
// one-pass gmailwiz run on the M2 box.
//
// Setup:
//   1. Create a new Action in Drafts: Action -> Script (JavaScript).
//   2. Paste this file as the script body.
//   3. In Drafts' Credentials manager, create an entry named
//      "gmailwiz-trigger" with one field: "token" = <GMAILWIZ_TRIGGER_TOKEN>.
//      (Same value as on M2's .env. Stored in the iOS keychain, not in
//      the action source.)
//   4. Optional draft content overrides the default:
//        - Empty draft               -> POST /run with default body
//        - First line is just digits -> uses that as `limit`
//        - First line "no-archive"   -> labels but skips archive
//        - First line "limit=N"      -> uses N as limit
//
// The action runs whether the draft has content or not — content just
// tweaks the request body.

const ENDPOINT = "https://gmailwiz.gaylon.photos/run";

// Pull the trigger token from the Drafts credential store rather than
// inlining it in the action source. Credentials are encrypted in the
// iOS keychain and survive iCloud syncs of Drafts itself.
const creds = Credential.create("gmailwiz-trigger", "gmailwiz one-pass Bearer token");
creds.addPasswordField("token", "GMAILWIZ_TRIGGER_TOKEN");
creds.authorize();
const token = creds.getValue("token");

if (!token || token.length < 8) {
  context.fail("gmailwiz-trigger credential missing or too short");
  // context.fail short-circuits Drafts so the rest never runs.
}

// Parse optional overrides from the first line of the draft.
let limit = 1000;
let archive = true;

const first = (draft.content || "").trim().split("\n")[0].trim();
if (first.length > 0) {
  const m = first.match(/^(?:limit\s*=\s*)?(\d+)$/i);
  if (m) {
    limit = parseInt(m[1], 10);
  } else if (/^no[-_]archive$/i.test(first)) {
    archive = false;
  }
}

// Build the request.
const http = HTTP.create();
const resp = http.request({
  url: ENDPOINT,
  method: "POST",
  headers: {
    "Authorization": "Bearer " + token,
    "Content-Type": "application/json",
  },
  data: { limit: limit, archive: archive },
  encoding: "json",
});

if (resp.statusCode === 202) {
  let body = {};
  try { body = JSON.parse(resp.responseText); } catch (e) { /* ignore */ }
  const jobId = body.job_id ? body.job_id.substring(0, 8) : "?";
  app.displaySuccessMessage("gmailwiz queued: " + jobId);
} else if (resp.statusCode === 409) {
  // Another run already in flight — surface the existing job id so the
  // user knows their Drafts tap was a no-op (the prior run is still
  // working).
  let body = {};
  try { body = JSON.parse(resp.responseText); } catch (e) { /* ignore */ }
  const id = body.in_flight_job_id ? body.in_flight_job_id.substring(0, 8) : "?";
  app.displayWarningMessage("gmailwiz busy (job " + id + ")");
} else if (resp.statusCode === 401) {
  context.fail("Bearer token rejected — check the 'gmailwiz-trigger' credential");
} else if (resp.statusCode === 503) {
  context.fail("Server not ready: " + (resp.responseText || ""));
} else {
  context.fail("HTTP " + resp.statusCode + ": " + (resp.responseText || ""));
}

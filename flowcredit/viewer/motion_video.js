#!/usr/bin/env node
"use strict";

/* Record one full CSS animation loop to a WebM video.
 *
 * Usage: node motion_video.js <index.html> <out-dir> [duration-ms]
 *
 * Unlike motion_frames.js (which pauses and samples deterministic stills),
 * this keeps the page running so the delivery card can show the motion
 * itself. The duration defaults to two 2-second rig cycles. */

import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const htmlPath = path.resolve(process.argv[2] || "index.html");
const outDir = path.resolve(process.argv[3] || "motion-video");
const duration = Number(process.argv[4] || 4200);
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1200, height: 760 },
  recordVideo: { dir: outDir, size: { width: 1200, height: 760 } },
});
const page = await context.newPage();
await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
await page.waitForTimeout(duration);
const video = page.video();
await context.close();
await browser.close();
const saved = await video.path();
const target = path.join(outDir, "motion.webm");
fs.renameSync(saved, target);
process.stdout.write(JSON.stringify({
  ok: true,
  path: target,
  bytes: fs.statSync(target).size,
}));

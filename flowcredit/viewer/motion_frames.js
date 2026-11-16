#!/usr/bin/env node
"use strict";

/* Deterministic motion-frame probe.
 *
 * Usage: node motion_frames.js <index.html> <out-dir> <times-json>
 * Pauses every CSS animation, seeks to each requested millisecond, screenshots
 * the SVG, and reports part geometry. No model call and no network access.
 * Run with NODE_PATH pointing at viewer/node_modules. */

import { chromium } from "playwright";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const htmlPath = path.resolve(process.argv[2] || "index.html");
const outDir = path.resolve(process.argv[3] || "motion-frames");
const times = JSON.parse(process.argv[4] || "[0,300,600,900]");
fs.mkdirSync(outDir, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true });
  const frames = [];
  const geometry = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 600 } });
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
    await page.waitForTimeout(200);
    const scene = await page.locator("#scene").boundingBox();
    for (const time of times) {
      await page.evaluate(ms => {
        for (const animation of document.getAnimations()) {
          animation.pause();
          animation.currentTime = ms;
        }
      }, Number(time));
      await page.waitForTimeout(60);
      const name = `frame-${String(Number(time)).padStart(4, "0")}.png`;
      const target = path.join(outDir, name);
      await page.screenshot({ path: target, clip: scene || undefined });
      const bytes = fs.readFileSync(target);
      frames.push({
        time_ms: Number(time),
        name,
        sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
        width: scene ? Math.round(scene.width) : 0,
        height: scene ? Math.round(scene.height) : 0,
      });
      geometry.push(await page.evaluate(ms => {
        const box = selector => {
          const element = document.querySelector(selector);
          if (!element) return null;
          const rect = element.getBoundingClientRect();
          return { x: rect.x, y: rect.y, w: rect.width, h: rect.height,
                   cx: rect.x + rect.width / 2, cy: rect.y + rect.height / 2 };
        };
        const boxes = selector => [...document.querySelectorAll(selector)].map(element => {
          const rect = element.getBoundingClientRect();
          return { x: rect.x, y: rect.y, w: rect.width, h: rect.height,
                   cx: rect.x + rect.width / 2, cy: rect.y + rect.height / 2 };
        });
        const feet = boxes(".foot");
        // A support is a role: pedals on a bicycle, a deck on a scooter, or a
        // generic .support marker. The legacy field names stay for old runs.
        const pedals = boxes(".pedal, .deck, .support");
        const legs = boxes(".leg");
        const wheels = boxes(".wheel");
        const hips = boxes(".hip");
        const legElements = [...document.querySelectorAll(".leg")];
        const legDetails = legElements.map((element, index) => {
          const tag = element.tagName.toLowerCase();
          const pathElement = tag === "path" || tag === "polyline" || tag === "line"
            ? element : element.querySelector("path, polyline, line");
          if (!pathElement) {
            return { index, has_path: false, length_px: 0, start: null, end: null };
          }
          const ctm = pathElement.getScreenCTM();
          const point = pathElement.ownerSVGElement.createSVGPoint();
          const toScreen = (x, y) => {
            if (!ctm) return null;
            point.x = x; point.y = y;
            const screen = point.matrixTransform(ctm);
            return { x: Math.round(screen.x * 10) / 10, y: Math.round(screen.y * 10) / 10 };
          };
          const scale = ctm ? Math.sqrt(Math.abs(ctm.a * ctm.d - ctm.b * ctm.c)) : 1;
          let startLocal;
          let endLocal;
          let length;
          if (pathElement.tagName.toLowerCase() === "line") {
            const x1 = Number(pathElement.getAttribute("x1") || 0);
            const y1 = Number(pathElement.getAttribute("y1") || 0);
            const x2 = Number(pathElement.getAttribute("x2") || 0);
            const y2 = Number(pathElement.getAttribute("y2") || 0);
            length = Math.hypot(x2 - x1, y2 - y1);
            startLocal = { x: x1, y: y1 };
            endLocal = { x: x2, y: y2 };
          } else {
            if (typeof pathElement.getTotalLength !== "function") {
              return { index, has_path: false, length_px: 0, start: null, end: null };
            }
            length = pathElement.getTotalLength();
            startLocal = pathElement.getPointAtLength(0);
            endLocal = pathElement.getPointAtLength(length);
          }
          return {
            index,
            has_path: true,
            length_px: Math.round(length * scale * 10) / 10,
            start: toScreen(startLocal.x, startLocal.y),
            end: toScreen(endLocal.x, endLocal.y),
          };
        });
        const footCenters = feet.map((foot, index) => ({ index, ...foot }));
        const pedalDetails = pedals.map((pedal, index) => ({ index, ...pedal }));
        const animationNames = selector => [...document.querySelectorAll(selector)]
          .map(element => getComputedStyle(element).animationName || "");
        const animationContract = {
          wheel: animationNames(".wheel"),
          leg: animationNames(".leg"),
          background: animationNames(".background, .scenery"),
        };
        const footDetails = [];
        const distanceToBox = (point, box) => {
          const dx = Math.max(box.x - point.cx, 0, point.cx - (box.x + box.w));
          const dy = Math.max(box.y - point.cy, 0, point.cy - (box.y + box.h));
          return Math.hypot(dx, dy);
        };
        feet.forEach((foot, footIndex) => {
          let best = null;
          pedals.forEach((pedal, pedalIndex) => {
            // Distance to the support rectangle, not just its center: a long
            // scooter deck must count as contact when a foot is anywhere on it.
            const distance = distanceToBox(foot, pedal);
            if (best === null || distance < best.distance_px) {
              best = { pedal_index: pedalIndex, distance_px: Math.round(distance * 10) / 10 };
            }
          });
          const footElement = document.querySelectorAll(".foot")[footIndex];
          const pedalElement = best
            ? document.querySelectorAll(".pedal, .deck, .support")[best.pedal_index]
            : null;
          footDetails.push({
            foot_index: footIndex,
            foot_class: footElement ? (footElement.getAttribute("class") || "") : "",
            pedal_index: best ? best.pedal_index : -1,
            pedal_class: pedalElement ? (pedalElement.getAttribute("class") || "") : "",
            distance_px: best ? best.distance_px : null,
          });
        });
        const footPedal = footDetails.map(detail => detail.distance_px);
        let legWheelOverlap = 0;
        for (const leg of legs) for (const wheel of wheels) {
          const left = Math.max(leg.x, wheel.x);
          const top = Math.max(leg.y, wheel.y);
          const right = Math.min(leg.x + leg.w, wheel.x + wheel.w);
          const bottom = Math.min(leg.y + leg.h, wheel.y + wheel.h);
          legWheelOverlap = Math.max(legWheelOverlap,
            Math.max(0, right - left) * Math.max(0, bottom - top));
        }
        let legLegOverlap = 0;
        for (let i = 0; i < legs.length; i++) {
          for (let j = i + 1; j < legs.length; j++) {
            const left = Math.max(legs[i].x, legs[j].x);
            const top = Math.max(legs[i].y, legs[j].y);
            const right = Math.min(legs[i].x + legs[i].w, legs[j].x + legs[j].w);
            const bottom = Math.min(legs[i].y + legs[i].h, legs[j].y + legs[j].h);
            legLegOverlap = Math.max(legLegOverlap,
              Math.max(0, right - left) * Math.max(0, bottom - top));
          }
        }
        return {
          time_ms: Number(ms),
          feet: feet.length,
          pedals: pedals.length,
          legs: legs.length,
          wheels: wheels.length,
          hips: hips.length,
          scene_box: box("#scene"),
          wheel_boxes: wheels,
          hip_centers: hips,
          leg_details: legDetails,
          foot_centers: footCenters,
          pedal_centers: pedalDetails,
          animation_names: animationContract,
          foot_pedal_px: footPedal,
          foot_pedal_details: footDetails,
          max_foot_pedal_px: footPedal.filter(v => v !== null).length
            ? Math.round(Math.max(...footPedal.filter(v => v !== null)) * 10) / 10 : 0,
          leg_wheel_overlap_px2: Math.round(legWheelOverlap),
          leg_leg_overlap_px2: Math.round(legLegOverlap),
          pelican: box(".pelican"),
          bicycle: box(".bicycle"),
        };
      }, Number(time)));
    }
    process.stdout.write(JSON.stringify({
      ok: true,
      frames,
      geometry,
      contact: "",
    }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(error && error.message || error) }));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();

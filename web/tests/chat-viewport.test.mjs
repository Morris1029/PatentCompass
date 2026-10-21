import test from "node:test";
import assert from "node:assert/strict";
import { chatViewportGeometry } from "../src/lib/chat-viewport.ts";

test("keyboard opening, viewport panning and keyboard closing preserve the visible chat bounds", () => {
  let geometry = chatViewportGeometry({ height: 844, offsetTop: 0, scale: 1 });
  geometry = chatViewportGeometry({ height: 410, offsetTop: 36, scale: 1 }, geometry);
  assert.equal(geometry.offsetTop + geometry.height, 446);
  geometry = chatViewportGeometry({ height: 628, offsetTop: 12, scale: 1 }, geometry);
  assert.deepEqual(geometry, { height: 628, offsetTop: 12 });
  geometry = chatViewportGeometry({ height: 844, offsetTop: 0, scale: 1 }, geometry);
  assert.deepEqual(geometry, { height: 844, offsetTop: 0 });
});

test("pinch zoom and panning freeze the last unzoomed geometry, then resume when zoom returns", () => {
  const normal = chatViewportGeometry({ height: 760, offsetTop: 0, scale: 1 });
  const zoomed = chatViewportGeometry({ height: 380, offsetTop: 130, scale: 2 }, normal);
  assert.equal(zoomed, normal);
  assert.equal(chatViewportGeometry({ height: 380, offsetTop: 250, scale: 2 }, zoomed), normal);
  assert.deepEqual(chatViewportGeometry({ height: 720, offsetTop: 4, scale: 1 }, zoomed), { height: 720, offsetTop: 4 });
});

test("desktop zoom supplies fresh CSS pixel geometry without applying a second scale", () => {
  const initial = chatViewportGeometry({ height: 900, offsetTop: 0, scale: 1 });
  assert.deepEqual(chatViewportGeometry({ height: 450, offsetTop: 0, scale: 1 }, initial), { height: 450, offsetTop: 0 });
  assert.equal(chatViewportGeometry({ height: 300, offsetTop: 10, scale: 1.5 }), undefined);
});

test("transient invalid viewport samples retain usable geometry and overscroll does not create a negative top", () => {
  const previous = { height: 400, offsetTop: 0 };
  assert.equal(chatViewportGeometry({ height: 0, offsetTop: 0, scale: 1 }, previous), previous);
  assert.equal(chatViewportGeometry({ height: NaN, offsetTop: 0, scale: 1 }, previous), previous);
  assert.equal(chatViewportGeometry({ height: 400, offsetTop: -8, scale: 1 }, previous), previous);
});

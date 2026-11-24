// q2t4.js - wireframe/string overlay for the Qgis2threejs preview (three.js r110, ES5)
// Injected by the q2t4 QGIS plugin. Safe to inject repeatedly.
(function () {
	"use strict";

	var VERSION = "0.13.27";
	if (window.Q2T4 && window.Q2T4.version === VERSION) {
		window.Q2T4.attach();
		return;
	}
	// newer module replacing an older one (dev reload): drop the old group + UI
	if (window.Q2T4) {
		var old = window.Q2T4;
		if (old.group && old.group.parent) old.group.parent.remove(old.group);
		var oldUi = document.getElementById("q2t4-toolbar");
		if (oldUi) oldUi.parentNode.removeChild(oldUi);
		if (old.sectionFrame && old.sectionFrame.parent) old.sectionFrame.parent.remove(old.sectionFrame);
		if (old.digObj && old.digObj.parent) old.digObj.parent.remove(old.digObj);
		if (old.queryObj && old.queryObj.parent) old.queryObj.parent.remove(old.queryObj);
		var oldQ = document.getElementById("q2t4-query");
		if (oldQ) oldQ.parentNode.removeChild(oldQ);
		["q2t4-hud", "q2t4-hud-style"].forEach(function (i) { var e = document.getElementById(i); if (e) e.parentNode.removeChild(e); });
		if (window.app && app.renderer) app.renderer.domElement.style.cursor = "";
		if (window.app && app.renderer) app.renderer.clippingPlanes = [];
		// every helper layer the old module put in the scene (edit / selection highlights, triangles,
		// measure, ...) - otherwise its last state stays on screen as a ghost
		if (window.app && app.scene)
			app.scene.children.slice().forEach(function (c) { if (c.name && c.name.indexOf("q2t4") === 0) app.scene.remove(c); });
		["q2t4-labels", "q2t4-selbox"].forEach(function (i) { var e = document.getElementById(i); if (e) e.parentNode.removeChild(e); });
	}

	var G = window.Q2T4 = {
		version: VERSION,
		group: null,            // THREE.Group under app.scene (mirrors Q2T vector layer transform)
		meshes: {},             // id -> {obj, spec}
		edgesVisible: true,
		pickMode: null,         // "orbit" while waiting for an orbit-point click
		section: null,          // {a:[x,y], b:[x,y], width, limitEnds, enabled} in MAP coords
		sectionPlanes: [],      // THREE.Plane[] in world coords (active clip set)
		sectionFrame: null,
		overlays: {},           // name -> {obj, spec, snapWorld: Float32Array|null}
		blocks: {},             // id -> {obj, spec} block models (instanced boxes, one mesh per colour class)
		dig: {active: false, pts: [], kinds: [], events: [], hover: null,
			  snap: {section: true, surface: true, drill: true, strings: true, wire: false, tol: 12},
			  snapOn: true, snapMode: "all", trace: false},
		digObj: null            // preview group (world coords)
	};

	function b64ToBuffer(b64) {
		var s = atob(b64), n = s.length, u = new Uint8Array(n);
		for (var i = 0; i < n; i++) u[i] = s.charCodeAt(i);
		return u.buffer;
	}

	// three.js r110 (Qgis2threejs 2.x) and newer releases (3.x) differ in a few calls
	var VCOLORS = (THREE.VertexColors !== undefined) ? THREE.VertexColors : true;
	function invert4(m) {
		var r = m.clone();
		return r.invert ? r.invert() : new THREE.Matrix4().getInverse(m);
	}

	// page position of an element (Qgis2threejs 2.x had app._offset; 3.x doesn't)
	function offsetOf(el) {
		if (window.app && app._offset) return app._offset(el);
		var r = el.getBoundingClientRect();
		return {left: r.left + (window.pageXOffset || 0), top: r.top + (window.pageYOffset || 0)};
	}

	function render() {
		if (window.app && app.render) app.render();
	}

	function sceneReady() {
		return !!(window.app && app.scene && app.scene.userData && app.scene.userData.origin);
	}

	// ------------------------------------------------------------ GPU
	// What the 3D view is drawn with (WebGL renderer string), for core/gpu.py to choose settings.
	G.gpuDetails = function () {
		if (!window.app || !app.renderer) return "";
		var gl = app.renderer.getContext(), e = gl.getExtension("WEBGL_debug_renderer_info");
		return JSON.stringify({
			renderer: e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
			vendor: e ? gl.getParameter(e.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
			webgl2: (typeof WebGL2RenderingContext != "undefined") && (gl instanceof WebGL2RenderingContext),
			maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE), dpr: window.devicePixelRatio || 1,
			pixelRatio: app.renderer.getPixelRatio()});
	};

	// Drawing resolution: the screen's own (cap null) or at most `cap` device pixels per CSS pixel.
	// Re-applied when the window moves to a screen with other scaling. Image export is untouched.
	G.setPixelRatioCap = function (cap) {
		G.gpuCap = (cap === undefined) ? null : cap;
		var apply = function () {
			if (!window.app || !app.renderer) return;
			var M = window.Q2T4 || G, dpr = window.devicePixelRatio || 1;
			var pr = (M.gpuCap ? Math.min(dpr, M.gpuCap) : dpr);
			if (Math.abs(app.renderer.getPixelRatio() - pr) > 1e-6) {
				app.renderer.setPixelRatio(pr);
				app.renderer.setSize(app.width || app.container.clientWidth, app.height || app.container.clientHeight);
				app.render();
			}
		};
		apply();
		if (!window._q2t4DprHook) {
			window._q2t4DprHook = true;
			window.addEventListener("resize", function () { var M = window.Q2T4; if (M && M._applyPr) M._applyPr(); });
		}
		G._applyPr = apply;
		return app.renderer ? app.renderer.getPixelRatio() : null;
	};

	// Attach our group to the current scene and hook scene property changes
	G.attach = function () {
		if (!window.app || !app.scene) return false;
		var scene = app.scene;

		if (!G.group) {
			G.group = new THREE.Group();
			G.group.name = "q2t4";
		}
		if (G.group.parent !== scene) scene.add(G.group);
		if (G._hookZoomToLayer) G._hookZoomToLayer();
		if (G._hookTools) G._hookTools();
		if (G._hookGrid) { G._hookGrid(); window.setTimeout(function () { if (window.Q2T4 && Q2T4.updateGrid) Q2T4.updateGrid(); }, 50); }

		// v2 hook dispatches to the current module (window.Q2T4), so it survives hot reloads.
		// An older v1 hook (bound to an old module) may sit underneath; ours runs after it.
		if ((scene._q2t4LoadV || 0) < 3) {
			var orig = scene.loadData;
			scene.loadData = function (data) {
				// scene updates (the QGIS map panned / zoomed) move the world origin: remember the old one
				var u0 = this.userData, before = (data && data.type == "scene" && u0 && u0.origin) ?
					{x: u0.origin.x, y: u0.origin.y, z: u0.origin.z, zs: u0.zScale || 1} : null;
				var r = orig.apply(this, arguments);
				var M = window.Q2T4;
				if (M) {
					if (data && data.type == "scene") {
						M.updateTransforms();
						if (M.keepCameraOnMap) M.keepCameraOnMap(before);
					}
					M.patchClipping();          // new layers/blocks may bring thick-line materials
				}
				return r;
			};
			scene._q2t4Hooked = true;
			scene._q2t4LoadV = 3;
		}
		G.hookApp();
		G.buildToolbar();
		if (!G.toolbarVisible) { var tb0 = document.getElementById("q2t4-toolbar"); if (tb0) tb0.style.display = "none"; }
		G.updateTransforms();
		return true;
	};

	// ------------------------------------------------------------ camera tools
	function status(msg, ms) {
		if (window.pyObj && pyObj.showStatusMessage) pyObj.showStatusMessage(msg, ms || 0);
	}

	// Move target to `point`, translating the camera by the same amount
	// (view direction and distance unchanged: the point slides to screen centre).
	G.setOrbitPoint = function (point) {
		if (G.dig && G.dig.active) return;      // old (v1) dblclick listeners call this
		G._setOrbit(point);
	};
	G._setOrbit = function (point) {
		var t = app.controls.target;
		if (!t) return;
		var d = new THREE.Vector3().subVectors(point, t);
		t.add(d);
		app.camera.position.add(d);
		app.updateControlsAndRender();
	};

	function visibleBox(wireframesOnly) {
		var box = new THREE.Box3();
		if (G.group) {
			G.group.updateMatrixWorld(true);
			for (var id in G.meshes) {
				var o = G.meshes[id].obj;
				if (o.visible) box.expandByObject(o.getObjectByName("faces"));
			}
			for (var bid in G.blocks) {
				var bo = G.blocks[bid].obj;
				if (bo.visible) box.expandByObject(bo);
			}
		}
		if (box.isEmpty() || !wireframesOnly) {
			var b2 = app.scene.boundingBox(true);
			if (!b2.isEmpty()) box.union(b2);
		}
		return box;
	}

	// Fit wireframes (or, if none, all visible layers) keeping the view direction;
	// orbit point = centre of the fitted box.
	// Standard views. Orbit point and viewing distance are kept.
	//   plan: looking straight down, north up
	//   N/E/S/W: horizontal view looking towards that direction (map rotation respected)
	var VIEW_DIRS = {N: [0, 1], E: [1, 0], S: [0, -1], W: [-1, 0]};
	G.setView = function (name) {
		var cam = app.camera, t = app.controls.target;
		if (!t || !sceneReady()) return;
		var dist = cam.position.distanceTo(t);
		if (!(dist > 0)) dist = app.scene.userData.baseExtent.width * 0.5;
		var north = dirToWorld(0, 1);
		var pos;
		if (name === "plan") {
			// a hair south of vertical so OrbitControls keeps north at the top of the screen
			pos = t.clone().addScaledVector(north, -dist * 1e-4);
			pos.z = t.z + dist;
		} else {
			var d = VIEW_DIRS[name];
			if (!d) return;
			var look = dirToWorld(d[0], d[1]);
			pos = t.clone().addScaledVector(look, -dist);
		}
		cam.position.copy(pos);
		cam.up.set(0, 0, 1);
		cam.lookAt(t);
		app.updateControlsAndRender();
		G._updateToolbar();
	};

	G.recentre = function (all) {
		G.fitBox(visibleBox(!all));
	};

	// Centre the view on the QGIS map: the orbit point goes to the centre of the area the scene
	// covers (the QGIS map extent), at the height of what is shown there; the whole area fits the
	// window, seen from Qgis2threejs' default direction. once: only when the map area changed since
	// the last time (kept on the page, so re-injecting the module doesn't move the camera).
	G.centreOnMap = function (once) {
		if (!sceneReady() || !app.controls || !app.controls.target) return false;
		var u = app.scene.userData, be = u.baseExtent, zs = u.zScale || 1;
		var key = [be.cx, be.cy, be.width, be.height, be.rotation || 0].map(function (v) { return (+v).toFixed(2); }).join(",");
		if (once && app._q2t4CentredFor === key) return false;
		app._q2t4CentredFor = key;
		var cx = be.cx - u.origin.x, cy = be.cy - u.origin.y, w = be.width, h = be.height || be.width;
		var foot = new THREE.Box3(new THREE.Vector3(cx - w / 2, cy - h / 2, -Infinity),
			new THREE.Vector3(cx + w / 2, cy + h / 2, Infinity));
		var z = new THREE.Box3(), b = new THREE.Box3();
		function take(o) {
			if (!o || !o.visible) return;
			b.setFromObject(o);
			if (b.isEmpty() || !isFinite(b.min.z) || !isFinite(b.max.z)) return;
			if (b.max.x < foot.min.x || b.min.x > foot.max.x || b.max.y < foot.min.y || b.min.y > foot.max.y) return;
			z.union(b);
		}
		if (G.group) {
			G.group.updateMatrixWorld(true);
			for (var id in G.meshes) take(G.meshes[id].obj);
			for (var bid in G.blocks) take(G.blocks[bid].obj);
		}
		for (var lid in app.scene.mapLayers) {
			var L = app.scene.mapLayers[lid];
			if (L.visible && L.objectGroup) take(L.objectGroup);
		}
		var zc = z.isEmpty() ? (u.origin.z ? 0 : 0) : (Math.max(z.min.z, -1e7) + Math.min(z.max.z, 1e7)) / 2;
		var dz = z.isEmpty() ? 0 : Math.min(z.max.z - z.min.z, Math.max(w, h));
		var box = new THREE.Box3(new THREE.Vector3(cx - w / 2, cy - h / 2, zc - dz / 2),
			new THREE.Vector3(cx + w / 2, cy + h / 2, zc + dz / 2));
		G.fitBox(box);                                            // keeps the view direction
		return true;
	};

	// Fit a world box keeping the view direction (perspective or orthographic camera);
	// orbit point = box centre.
	G.fitBox = function (box) {
		if (!box || box.isEmpty()) return false;
		var c = new THREE.Vector3(), s = new THREE.Vector3();
		box.getCenter(c);
		box.getSize(s);
		var r = Math.max(s.length() / 2, 1);

		var cam = app.camera, t = app.controls.target || new THREE.Vector3();
		var dir = new THREE.Vector3().subVectors(cam.position, t);
		if (dir.lengthSq() < 1e-9) dir.copy(app.cameraAction.vecZoom);
		dir.normalize();

		var dist;
		if (cam.isPerspectiveCamera) {
			var vfov = cam.fov * Math.PI / 360, hfov = Math.atan(Math.tan(vfov) * cam.aspect);
			dist = r / Math.sin(Math.min(vfov, hfov)) * 1.05;
		} else {
			dist = Math.max(cam.position.distanceTo(t), r * 3);
			cam.zoom = Math.min(cam.right - cam.left, cam.top - cam.bottom) / (2.1 * r);
			cam.updateProjectionMatrix();
		}
		cam.position.copy(c).addScaledVector(dir, dist);
		if (cam.far < dist + r * 2) { cam.far = (dist + r * 2) * 2; cam.updateProjectionMatrix(); }
		t.copy(c);
		cam.lookAt(c);
		app.updateControlsAndRender();
		return true;
	};

	// Plan view fitted to a world box: the whole footprint on screen, north up (map rotation kept),
	// orbit point = box centre. Used for "Zoom to layer objects" in Qgis2threejs' layer list.
	G.fitPlan = function (box) {
		if (!box || box.isEmpty() || !sceneReady()) return false;
		var c = new THREE.Vector3(), s = new THREE.Vector3();
		box.getCenter(c);
		box.getSize(s);
		var cam = app.camera, t = app.controls.target || new THREE.Vector3();
		// footprint in screen axes (north up): project the 4 plan corners onto the east / north directions
		var east = dirToWorld(1, 0), north = dirToWorld(0, 1);
		var hw = 0, hh = 0;
		[[box.min.x, box.min.y], [box.max.x, box.min.y], [box.min.x, box.max.y], [box.max.x, box.max.y]].forEach(function (p) {
			var dx = p[0] - c.x, dy = p[1] - c.y;
			hw = Math.max(hw, Math.abs(dx * east.x + dy * east.y));
			hh = Math.max(hh, Math.abs(dx * north.x + dy * north.y));
		});
		hw = Math.max(hw, 1) * 1.08;
		hh = Math.max(hh, 1) * 1.08;
		// keep the footprint clear of the scale bar / grid labels at the bottom and the HUD at the top
		var H = app.height || 600, W = app.width || 800;
		var padB = (G.scaleOn === false ? 24 : 96), padT = 24;
		var useH = Math.max(H - padB - padT, H * 0.5);
		var dist, wpp;                                     // world units per pixel at the target
		if (cam.isPerspectiveCamera) {
			var vt = Math.tan(cam.fov * Math.PI / 360), ht = vt * cam.aspect;
			dist = Math.max(hh * H / useH / vt, hw / ht) + s.z / 2;
			wpp = 2 * (dist - s.z / 2) * vt / H;
		} else {
			dist = Math.max(cam.position.distanceTo(t), s.z * 2, Math.max(hw, hh) * 3);
			cam.zoom = Math.min((cam.right - cam.left) / (2 * hw), (cam.top - cam.bottom) * useH / H / (2 * hh));
			cam.updateProjectionMatrix();
			wpp = (cam.top - cam.bottom) / cam.zoom / H;
		}
		// shift the view centre south so the box sits in the clear band
		c.addScaledVector(north, -(padB - padT) / 2 * wpp);
		t.copy(c);
		cam.position.copy(c).addScaledVector(north, -dist * 1e-4);   // a hair south: north stays up
		cam.position.z = c.z + dist;
		cam.up.set(0, 0, 1);
		if (cam.far < dist + s.z * 2) { cam.far = (dist + s.z * 2) * 2; cam.updateProjectionMatrix(); }
		cam.lookAt(c);
		app.updateControlsAndRender();
		if (G._updateToolbar) G._updateToolbar();
		return true;
	};

	G.zoomToLayerPlan = function (layer) {
		if (!layer) return false;
		var b = layer.boundingBox ? layer.boundingBox() : new THREE.Box3().setFromObject(layer.objectGroup || layer);
		if (!b || b.isEmpty() || !isFinite(b.min.x) || !isFinite(b.max.x)) {
			// nothing of this layer in the 3D scene (e.g. outside the exported map extent): leave the view alone
			status("Nothing of " + ((layer.properties && layer.properties.name) || "this layer") +
				   " is in the 3D scene (outside the map extent?)", 4000);
			return true;
		}
		var ok = G.fitPlan(b);
		if (app.cleanView) app.cleanView();
		return ok;
	};

	// Qgis2threejs' "Zoom to layer objects" (layer list right-click) -> whole layer, plan view.
	// Installed once per page; dispatches to the current module so hot reloads keep working.
	G._hookZoomToLayer = function () {
		if (!app.cameraAction || app.cameraAction._q2t4Zoom) return;
		var orig = app.cameraAction.zoomToLayer;
		app.cameraAction.zoomToLayer = function (layer) {
			var M = window.Q2T4;
			if (M && M.zoomToLayerPlan && M.zoomToLayerPlan(layer)) return;
			return orig.apply(this, arguments);
		};
		app.cameraAction._q2t4Zoom = true;
	};

	G.startOrbitPick = function () {
		G.stopDigitise();
		if (G.queryMode) G.setQueryMode(false);
		G.pickMode = "orbit";
		app.renderer.domElement.style.cursor = "crosshair";
		G._updateToolbar();
		status("Click a surface, block, drillhole or string to set the orbit point (Esc to cancel)");
	};

	G.cancelPick = function () {
		G.pickMode = null;
		app.renderer.domElement.style.cursor = G.dig.active ? "crosshair" : "";
		G._updateToolbar();
		status("", 1);
	};

	// Point under the cursor for orbit / double-click / status bar.
	// Surfaces: Q2T layers, our wireframe faces and block-model chunks (raycast).
	// Lines: drillholes / channels / strings, tested in screen space within LINE_TOL px,
	// because Q2T's thick lines (MeshLine) are widened in the shader and can't be raycast.
	var LINE_TOL = 8;

	function linePick(sx, sy, cam) {
		var best = null, bestD = LINE_TOL * LINE_TOL;
		var a = new THREE.Vector3(), b = new THREE.Vector3(), pa = new THREE.Vector3(), pb = new THREE.Vector3();
		var hw = app.width / 2, hh = app.height / 2;
		function testSeg(pos, i, j, mw) {
			a.fromBufferAttribute(pos, i).applyMatrix4(mw);
			b.fromBufferAttribute(pos, j).applyMatrix4(mw);
			pa.copy(a).project(cam); pb.copy(b).project(cam);
			if (pa.z > 1 || pb.z > 1 || pa.z < -1 || pb.z < -1) return;
			var ax = (pa.x + 1) * hw, ay = (1 - pa.y) * hh, bx = (pb.x + 1) * hw, by = (1 - pb.y) * hh;
			var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
			var t = L2 > 0 ? Math.max(0, Math.min(1, ((sx - ax) * dx + (sy - ay) * dy) / L2)) : 0;
			var ex = ax + t * dx - sx, ey = ay + t * dy - sy, d = ex * ex + ey * ey;
			if (d <= bestD) {
				var w = a.clone().lerp(b, t);
				if (!G.insideSection(w)) return;
				var depth = w.distanceTo(cam.position);
				if (best && d > bestD - 1e-6 && depth >= best.depth) return;
				bestD = d; best = {p: w, depth: depth, px: Math.sqrt(d)};
			}
		}
		var skip = [G.sectionFrame, G.queryObj, G.digObj];
		function visibleChain(o) {      // visible, and not a helper (section frame, query box, rubber band)
			if (o.name === "edges") return false;
			for (var q = o; q; q = q.parent) if (!q.visible || skip.indexOf(q) >= 0) return false;
			return true;
		}
		app.scene.traverse(function (o) {
			var g = o.geometry, pos = g && g.attributes && g.attributes.position;
			if (!pos || !o.visible) return;
			var isMeshLine = o.isMesh && o.material && o.material.type === "MeshLineMaterial";
			if (!isMeshLine && !o.isLine) return;
			if (!visibleChain(o)) return;
			var mw = o.matrixWorld, n = pos.count, i;
			if (isMeshLine) {                      // points are duplicated: p0 p0 p1 p1 ...
				for (i = 0; i + 2 < n; i += 2) testSeg(pos, i, i + 2, mw);
			} else if (o.isLineSegments) {
				var idx = g.index;
				if (idx) for (i = 0; i + 1 < idx.count; i += 2) testSeg(pos, idx.getX(i), idx.getX(i + 1), mw);
				else for (i = 0; i + 1 < n; i += 2) testSeg(pos, i, i + 1, mw);
			} else {
				for (i = 0; i + 1 < n; i++) testSeg(pos, i, i + 1, mw);
			}
		});
		return best;
	}

	// ray param interval [t0, t1] (world, ray.direction unit) inside every section plane
	function slabInterval(o, d) {
		var t0 = 0, t1 = Infinity;
		for (var i = 0; i < G.sectionPlanes.length; i++) {
			var pl = G.sectionPlanes[i], a = pl.distanceToPoint(o), b = pl.normal.dot(d);
			if (Math.abs(b) < 1e-12) { if (a < 0) return null; continue; }
			var t = -a / b;
			if (b > 0) t0 = Math.max(t0, t); else t1 = Math.min(t1, t);
		}
		return t0 <= t1 ? [t0, t1] : null;
	}

	// nearest shown block along the ray, counting only the part of each block inside the slab
	function blockPick(ray) {
		var o = ray.origin, d = ray.direction, slab = slabInterval(o, d);
		if (!slab) return null;
		var best = Infinity, bestId = null, bestC = -1, bestK = -1;
		for (var id in (G.blocks || {})) {
			var B = G.blocks[id];
			if (!B.obj.visible || !B.boxes) continue;
			var inv = invert4(B.obj.matrixWorld);
			var lo = o.clone().applyMatrix4(inv), ld = o.clone().add(d).applyMatrix4(inv).sub(lo);   // same t as world
			var ox = lo.x, oy = lo.y, oz = lo.z, ix = 1 / ld.x, iy = 1 / ld.y, iz = 1 / ld.z;
			for (var c = 0; c < B.boxes.length; c++) {
				var P = B.boxes[c].pos, S = B.boxes[c].size, n = P.length / 3;
				for (var k = 0, j = 0; k < n; k++, j += 3) {
					var hx = S[j] / 2, hy = S[j + 1] / 2, hz = S[j + 2] / 2;
					var tx1 = (P[j] - hx - ox) * ix, tx2 = (P[j] + hx - ox) * ix;
					var tmin = Math.min(tx1, tx2), tmax = Math.max(tx1, tx2);
					var ty1 = (P[j + 1] - hy - oy) * iy, ty2 = (P[j + 1] + hy - oy) * iy;
					tmin = Math.max(tmin, Math.min(ty1, ty2)); tmax = Math.min(tmax, Math.max(ty1, ty2));
					if (tmax < tmin) continue;
					var tz1 = (P[j + 2] - hz - oz) * iz, tz2 = (P[j + 2] + hz - oz) * iz;
					tmin = Math.max(tmin, Math.min(tz1, tz2)); tmax = Math.min(tmax, Math.max(tz1, tz2));
					var e = Math.max(tmin, slab[0]);
					if (e <= Math.min(tmax, slab[1]) && e < best) { best = e; bestId = id; bestC = c; bestK = k; }
				}
			}
		}
		return best < Infinity ? {t: best, p: o.clone().addScaledVector(d, best), id: bestId, cls: bestC, instance: bestK} : null;
	}

	G.pickInfo = function (clientX, clientY) {
		var off = offsetOf(app.renderer.domElement), cam = app.camera;
		var sx = clientX - off.left, sy = clientY - off.top;
		var v = new THREE.Vector2((sx / app.width) * 2 - 1, -(sy / app.height) * 2 + 1);
		cam.updateMatrixWorld();
		app.scene.updateMatrixWorld();
		var ray = new THREE.Raycaster();
		ray.setFromCamera(v, cam);
		var targets = app.scene.visibleObjects(false).filter(function (o) {
			return !(o.material && o.material.type === "MeshLineMaterial");
		});
		for (var id in G.meshes) {
			if (G.meshes[id].obj.visible) targets.push(G.meshes[id].obj.getObjectByName("faces"));
		}
		var surf = null, hits = ray.intersectObjects(targets, true);
		for (var i = 0; i < hits.length; i++) {
			if (G.insideSection(hits[i].point)) { surf = hits[i]; break; }   // ignore clipped-away geometry
		}
		var line = linePick(sx, sy, cam);
		// a line wins if it is in front of what is hit, or up to 1 m behind it (channels on drive walls,
		// holes through a block face). Inside a section the block's cut face isn't drawn, so lines
		// in the slab show through and beat blocks outright.
		var sd = surf ? surf.point.distanceTo(cam.position) : Infinity;
		var blk = blockPick(ray.ray);
		var bd = blk ? blk.p.distanceTo(cam.position) : Infinity;
		var lineVs = Math.min(sd, G.sectionPlanes.length ? Infinity : bd);
		if (line && line.depth <= lineVs + 1.0) return {p: line.p, kind: "line"};
		if (blk && bd < sd) return {p: blk.p, kind: "block"};
		if (surf) {
			var so = surf.object, sgt = (so && so.geometry && so.geometry.type) || "";
			if (so && so.isMesh && sgt.indexOf("Cylinder") >= 0) {     // Pipe / Cone interval: onto its axis
				var ax = new THREE.Line3(new THREE.Vector3(0, 0.5, 0).applyMatrix4(so.matrixWorld),
					new THREE.Vector3(0, -0.5, 0).applyMatrix4(so.matrixWorld));
				return {p: ax.closestPointToPoint(surf.point, true, new THREE.Vector3()), kind: "line"};
			}
			var kind = "surface";
			for (var q = surf.object; q; q = q.parent) {
				if (q.userData && q.userData.q2t4Id !== undefined) { kind = "wireframe"; break; }
			}
			return {p: surf.point, kind: kind};
		}
		return null;
	};

	G.pick = function (clientX, clientY) {
		var r = G.pickInfo(clientX, clientY);
		return r ? r.p : null;
	};

	// App hooks are installed once per page and dispatch to the *current* module,
	// so a re-injected newer module takes over without a page reload.
	// mouse-button state for the cursor readout (installed once per page, separately so an
	// already-hooked page gets it without duplicating the other listeners)
	G._hookButtons = function () {
		if (!app._q2t4BtnHook) {
			app._q2t4BtnHook = true;
			app.renderer.domElement.addEventListener("mousedown", function () { var M = window.Q2T4; if (M) M._btnDown = true; });
			window.addEventListener("mouseup", function () { var M = window.Q2T4; if (M) M._btnDown = false; });
		}
		if (!app._q2t4BtnHook2) {      // added after OrbitControls' own listener, so it runs after it
			app._q2t4BtnHook2 = true;
			app.renderer.domElement.addEventListener("mousedown", function (e) {
				var M = window.Q2T4;
				if (M && M.onButtonDown) M.onButtonDown(e);
			});
		}
	};

	// Qgis2threejs' OrbitControls pan across the ground plane on right-drag (screen plane only with
	// Shift), which in a section view moves the view towards / away from you. Swap them:
	// right-drag pans in the screen plane (as in Micromine), Shift+right-drag across the ground.
	G.panScreen = true;
	// While a string-editing / wireframing tool is on, the left button picks, so the right button rotates
	// (Shift / Ctrl + right-drag pans, wheel zooms). Qgis2threejs 2.x's OrbitControls map buttons to
	// handlers by button number, 3.x's map them to actions.
	G.syncToolControls = function () {
		var c = app.controls;
		if (!c || !c.mouseButtons) return;
		var on = !!((G.edit && G.edit.active) || (G.tri && G.tri.active) || (G.tdel && G.tdel.active));
		if (on && !app._q2t4MB) {
			app._q2t4MB = {LEFT: c.mouseButtons.LEFT, MIDDLE: c.mouseButtons.MIDDLE, RIGHT: c.mouseButtons.RIGHT};
			var M = THREE.MOUSE || {};
			if ("_onPointerDown" in c) c.mouseButtons = {LEFT: -1, MIDDLE: app._q2t4MB.MIDDLE, RIGHT: M.ROTATE !== undefined ? M.ROTATE : 0};
			else c.mouseButtons = {LEFT: 2, MIDDLE: app._q2t4MB.MIDDLE, RIGHT: -1};
		} else if (!on && app._q2t4MB) {
			c.mouseButtons = app._q2t4MB;
			app._q2t4MB = null;
		}
	};
	G.onButtonDown = function (e) {
		if (e.button === 2 && app.controls && G.panScreen) app.controls.screenSpacePanning = !e.shiftKey;
	};

	G.hookApp = function () {
		if (app._q2t4HookV >= 2) { G._hookButtons(); return; }
		var first = !app._q2t4Hooked;
		app._q2t4Hooked = true;
		app._q2t4HookV = 2;

		if (first || !app._q2t4OrigClicked) {
			app._q2t4OrigClicked = app._q2t4OrigClicked || app.canvasClicked;
		}
		var origClicked = app._q2t4OrigClicked;
		app.canvasClicked = function (e) {
			var M = window.Q2T4;
			if (M && M.onCanvasClicked && M.onCanvasClicked(e)) return;
			return origClicked.apply(this, arguments);
		};

		var el = app.renderer.domElement;
		el.addEventListener("dblclick", function (e) {
			var M = window.Q2T4;
			if (M && M.onDblClick) M.onDblClick(e);
		});
		G._hookButtons();
		el.addEventListener("mousemove", function (e) {
			var M = window.Q2T4;
			if (M && M.onMouseMove) M.onMouseMove(e);
		});
		window.addEventListener("keydown", function (e) {
			var M = window.Q2T4;
			if (M && M.onKeyDown) M.onKeyDown(e);
		}, true);
	};

	// --- event handlers (return true = handled, Q2T default suppressed)
	G.onCanvasClicked = function (e) {
		if (G.edit && G.edit.active) return true;
		if (G.meas && G.meas.active) {
			if (e.button === 0) G.measClick(e.clientX, e.clientY);
			else if (e.button === 2) G.stopMeasure();
			return true;
		}
		if (G.queryMode && !G.dig.active && G.pickMode !== "orbit") {
			if (e.button === 0) G.queryAt(e.clientX, e.clientY);
			return true;
		}
		if (G.dig.active) {
			if (e.button === 0) G.digAddPoint(e.clientX, e.clientY);
			else if (e.button === 2) G.digFinish();
			return true;
		}
		if (G.pickMode === "orbit") {
			if (e.button === 0) {
				var p = G.pick(e.clientX, e.clientY);
				if (p) { G._setOrbit(p); G.cancelPick(); }
				else status("Nothing under the cursor - click a surface, block or line (Esc to cancel)");
			}
			return true;
		}
		return false;
	};

	G.onDblClick = function (e) {
		if (G.dig.active) { G.digFinish(); return; }
		var p = G.pick(e.clientX, e.clientY);
		if (p) { G._setOrbit(p); app.cleanView(); }
	};

	G.onMouseMove = function (e) {
		if (G.dig.active) G.digHover(e.clientX, e.clientY);
		if (G.meas && G.meas.active && G.meas.pts.length && G.meas.pts.length < 3) {
			var hs = G.snapAt(e.clientX, e.clientY);
			G.meas.hover = hs ? hs.p : null;
			G.measRedraw();
		}
		// QtWebKit has no MouseEvent.buttons (undefined): track the buttons ourselves
		if (!G._btnDown) G._updateCursor(e.clientX, e.clientY);
	};

	G.onKeyDown = function (e) {
		if (e["_q2t_onKeyDown"]) return;
		e["_q2t_onKeyDown"] = true;
		var k = e.keyCode;
		if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === 90 || k === 89)) {    // Ctrl+Z undo, Ctrl+Y / Ctrl+Shift+Z redo
			var redo = k === 89 || e.shiftKey;
			if (!redo && G.dig.active) G.digUndo();                               // the last digitised point
			else if (!redo && G.tri && G.tri.active && G.tri.pts.length) { G.tri.pts.pop(); G.triRedraw(); }
			else if (!redo && G.edit && G.edit.active && G.edit.mode === "link") G.dig.events.push({type: "linkUndo"});
			else G.dig.events.push({type: redo ? "redo" : "undo"});
			e.preventDefault(); e.stopPropagation();
			return;
		}
		if (k === 83 && !e.ctrlKey && !e.altKey && (G.dig.active || (G.meas && G.meas.active) || (G.tri && G.tri.active))) {
			G.toggleSnap(); e.preventDefault(); e.stopPropagation(); return;                    // S
		}
		if (G.tri && G.tri.active) { G._triKey(e); return; }
		if (G.dig.active) {
			if (k === 84 && !e.ctrlKey) { G.setTrace(); e.preventDefault(); e.stopPropagation(); return; }   // T
			if (k === 13) { G.digFinish(); e.preventDefault(); e.stopPropagation(); }          // Enter
			else if (k === 8 || k === 46) { G.digUndo(); e.preventDefault(); e.stopPropagation(); }  // Backspace/Delete
			else if (k === 27) { G.digCancel(); e.preventDefault(); e.stopPropagation(); }     // Esc
			return;
		}
		if (G.meas && G.meas.active && k === 27) { G.stopMeasure(); return; }
		if (G.pickMode && k === 27) G.cancelPick();
		else if (k === 27 && G.queryMode) G.clearQuery();
	};

	// ------------------------------------------------------------- digitising
	var SNAP_COLORS = {drill: "#ff2bd6", strings: "#ffe000", surface: "#00e05a", section: "#ff6a00", free: "#ffffff"};

	function rayAt(clientX, clientY) {
		var off = offsetOf(app.renderer.domElement);
		var v = new THREE.Vector2(((clientX - off.left) / app.width) * 2 - 1, -((clientY - off.top) / app.height) * 2 + 1);
		var ray = new THREE.Raycaster();
		ray.setFromCamera(v, app.camera);
		return ray;
	}

	// best snap for a screen position: {p: Vector3 (world), kind}
	G.snapAt = function (clientX, clientY) {
		var sn = G.dig.snap, off = offsetOf(app.renderer.domElement);
		var sx = clientX - off.left, sy = clientY - off.top;
		app.camera.updateMatrixWorld();
		if (!G.dig.snapOn) return freePoint(clientX, clientY);       // S: snapping off
		var mode = G.dig.snapMode || "all";
		if (mode === "node") {                                         // vertices only
			var nv = vertexSnapAt(sx, sy, sn.tol) || wireVertexAt(sx, sy, sn.tol);
			return nv || freePoint(clientX, clientY);
		}
		if (mode === "string") {                                       // string vertices, else anywhere on the line
			var sv = vertexSnapAt(sx, sy, sn.tol, "strings");
			return sv || stringLineAt(sx, sy, sn.tol) || freePoint(clientX, clientY);
		}
		if (mode === "surface") {                                      // surfaces only
			var sp = G.pick(clientX, clientY);
			if (sp) {
				var sg2 = G.sectionGeom();
				if (sg2) sp = new THREE.Plane().setFromNormalAndCoplanarPoint(sg2.n, sg2.C).projectPoint(sp, new THREE.Vector3());
				return {p: sp, kind: "surface"};
			}
			return freePoint(clientX, clientY);
		}

		// 1. vertex snap (drillhole interval ends, string vertices) within tol px
		var best = null, bestD = sn.tol * sn.tol, v = new THREE.Vector3();
		for (var name in G.overlays) {
			var kind = (name.indexOf("drill") === 0) ? "drill" : (name.indexOf("strings") === 0 ? "strings" : null);
			if (!kind || !sn[kind]) continue;
			var ov = G.overlays[name];
			if (!ov.obj.visible) continue;
			var w = overlaySnapWorld(ov);
			for (var i = 0; i < w.length; i += 3) {
				v.set(w[i], w[i + 1], w[i + 2]);
				if (G.sectionPlanes.length && !G.insideSection(v)) continue;
				var q = v.clone().project(app.camera);
				if (q.z > 1) continue;
				var px = (q.x + 1) / 2 * app.width, py = (1 - q.y) / 2 * app.height;
				var d = (px - sx) * (px - sx) + (py - sy) * (py - sy);
				if (d < bestD) { bestD = d; best = {p: v.clone(), kind: kind}; }
			}
		}
		if (sn.drill) {                                          // Qgis2threejs line / point layers (desurveyed drillholes ...)
			var q2b = nearestOf(q2tLayerSnapWorld(), sx, sy, bestD, "drill");
			if (q2b) best = {p: q2b.p, kind: "drill"};
		}
		if (best) return best;
		if (sn.wire) {                                            // wireframe vertices (triangle mode)
			var wv = wireVertexAt(sx, sy, sn.tol);
			if (wv) return wv;
		}

		var g = G.sectionGeom(), plane = null;
		if (g && sn.section) plane = new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C);

		// 2. surfaces (DEM, layers, wireframes) inside the slab
		if (sn.surface) {
			var p = G.pick(clientX, clientY);
			if (p) {
				if (plane) p = plane.projectPoint(p, new THREE.Vector3());   // onto section plane
				return {p: p, kind: "surface"};
			}
		}
		// 3. section plane
		if (plane) {
			var hit = rayAt(clientX, clientY).ray.intersectPlane(plane, new THREE.Vector3());
			if (hit) return {p: hit, kind: "section"};
		}
		return null;
	};

	// string vertices and drillhole interval ends within tol px (every visible line overlay)
	function vertexSnapAt(sx, sy, tol, only) {
		var best = null, bd = tol * tol, v = new THREE.Vector3();
		for (var name in G.overlays) {
			var ov = G.overlays[name];
			if (!ov.obj.visible || !ov.snap || !ov.snap.length) continue;
			var kind = name.indexOf("drill") === 0 ? "drill" : "strings";
			if (only && kind !== only) continue;
			var w = overlaySnapWorld(ov);
			for (var i = 0; i < w.length; i += 3) {
				v.set(w[i], w[i + 1], w[i + 2]);
				if (G.sectionPlanes.length && !G.insideSection(v)) continue;
				var q = v.clone().project(app.camera);
				if (q.z > 1) continue;
				var px = (q.x + 1) / 2 * app.width, py = (1 - q.y) / 2 * app.height;
				var d = (px - sx) * (px - sx) + (py - sy) * (py - sy);
				if (d < bd) { bd = d; best = {p: v.clone(), kind: kind}; }
			}
		}
		if (!only || only === "drill") {
			var q2 = nearestOf(q2tLayerSnapWorld(), sx, sy, bd, "drill");
			if (q2) best = {p: q2.p, kind: "drill"};
		}
		return best;
	}
	function overlayPosWorld(ov) {
		ov.obj.updateMatrixWorld(true);
		var m = ov.obj.matrixWorld, key = m.elements.join(",");
		if (ov._pw && ov._pwKey === key) return ov._pw;
		var n = ov.pos.length / 3, out = new Float32Array(n * 3), v = new THREE.Vector3();
		for (var i = 0; i < n; i++) {
			v.set(ov.pos[i * 3], ov.pos[i * 3 + 1], ov.pos[i * 3 + 2]).applyMatrix4(m);
			out[i * 3] = v.x; out[i * 3 + 1] = v.y; out[i * 3 + 2] = v.z;
		}
		ov._pw = out; ov._pwKey = key;
		return out;
	}
	// nearest point on any string line (our strings, Surpac / Micromine strings) within tol px
	function stringLineAt(sx, sy, tol) {
		var best = null, bd = tol * tol, a = new THREE.Vector3(), b = new THREE.Vector3(), qa = new THREE.Vector3(), qb = new THREE.Vector3();
		for (var name in G.overlays) {
			if (!(name.indexOf("strings") === 0 || name.indexOf("surpac:") === 0 || name.indexOf("mm:") === 0)) continue;
			var ov = G.overlays[name];
			if (!ov.obj.visible || !ov.seg) continue;
			var w = overlayPosWorld(ov), sg = ov.seg;
			for (var i = 0; i + 1 < sg.length; i += 2) {
				var i0 = sg[i] * 3, i1 = sg[i + 1] * 3;
				a.set(w[i0], w[i0 + 1], w[i0 + 2]); b.set(w[i1], w[i1 + 1], w[i1 + 2]);
				qa.copy(a).project(app.camera); qb.copy(b).project(app.camera);
				if (qa.z > 1 || qb.z > 1) continue;
				var ax = (qa.x + 1) / 2 * app.width, ay = (1 - qa.y) / 2 * app.height;
				var bx = (qb.x + 1) / 2 * app.width, by = (1 - qb.y) / 2 * app.height;
				var dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
				var t = L > 0 ? Math.max(0, Math.min(1, ((sx - ax) * dx + (sy - ay) * dy) / L)) : 0;
				var px = ax + t * dx - sx, py = ay + t * dy - sy, d = px * px + py * py;
				if (d < bd) {
					var p = a.clone().lerp(b, t);
					if (G.sectionPlanes.length && !G.insideSection(p)) continue;
					bd = d; best = {p: p, kind: "strings"};
				}
			}
		}
		return best;
	}
	G.setSnapMode = function (m) {
		if (m === "off") { G.dig.snapOn = false; }
		else { G.dig.snapMode = m || "all"; G.dig.snapOn = true; }
		G.dig.events.push({type: "snapToggled", on: G.dig.snapOn, mode: G.dig.snapMode});
		var names = {all: "all", node: "nodes (vertices)", string: "string lines", surface: "surfaces"};
		status(G.dig.snapOn ? "Snapping to " + names[G.dig.snapMode] + " (S toggles)" : "Snapping off (S toggles)", 2500);
		if (G.dig.active && G._hoverXY) G.digHover(G._hoverXY[0], G._hoverXY[1]);
		return G.dig.snapMode;
	};

	// snapping off: on the section plane, else on the plane through the orbit point facing the camera
	function freePoint(clientX, clientY) {
		var g = G.sectionGeom(), plane;
		if (g) plane = new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C);
		else {
			var dir = new THREE.Vector3(); app.camera.getWorldDirection(dir);
			plane = new THREE.Plane().setFromNormalAndCoplanarPoint(dir, app.controls ? app.controls.target : new THREE.Vector3());
		}
		var hit = rayAt(clientX, clientY).ray.intersectPlane(plane, new THREE.Vector3());
		return hit ? {p: hit, kind: g ? "section" : "free"} : null;
	}

	// nearest wireframe vertex within tol px (world positions cached per mesh transform)
	function wireVertexAt(sx, sy, tol) {
		var best = null, bd = tol * tol, v = new THREE.Vector3();
		for (var id in G.meshes) {
			var m = G.meshes[id], o = m.obj;
			if (!o.visible) continue;
			var f = o.getObjectByName("faces");
			if (!f) continue;
			f.updateMatrixWorld(true);
			var key = f.matrixWorld.elements.join(",");
			if (!m._wv || m._wvKey !== key) {
				var a = f.geometry.getAttribute("position"), n = Math.min(a.count, 300000), w = new Float32Array(n * 3);
				for (var i = 0; i < n; i++) {
					v.set(a.getX(i), a.getY(i), a.getZ(i)).applyMatrix4(f.matrixWorld);
					w[i * 3] = v.x; w[i * 3 + 1] = v.y; w[i * 3 + 2] = v.z;
				}
				m._wv = w; m._wvKey = key;
			}
			var w2 = m._wv;
			for (var k = 0; k < w2.length; k += 3) {
				v.set(w2[k], w2[k + 1], w2[k + 2]);
				if (G.sectionPlanes.length && !G.insideSection(v)) continue;
				var q = v.clone().project(app.camera);
				if (q.z > 1) continue;
				var px = (q.x + 1) / 2 * app.width, py = (1 - q.y) / 2 * app.height;
				var d = (px - sx) * (px - sx) + (py - sy) * (py - sy);
				if (d < bd) { bd = d; best = {p: v.clone(), kind: "wire"}; }
			}
		}
		return best;
	}

	// S: snapping on / off (digitising, editing, measuring, triangles)
	G.toggleSnap = function (on) {
		G.dig.snapOn = (on === undefined) ? !G.dig.snapOn : !!on;
		G.dig.events.push({type: "snapToggled", on: G.dig.snapOn, mode: G.dig.snapMode});
		status(G.dig.snapOn ? "Snapping on: " + (G.dig.snapMode || "all") + " (S toggles)"
			: "Snapping OFF - points go on the section / view plane (S toggles)", 2500);
		if (G.dig.active && G._hoverXY) G.digHover(G._hoverXY[0], G._hoverXY[1]);
		return G.dig.snapOn;
	};
	// T: trace along strings / surfaces between consecutive clicks
	G.setTrace = function (on) {
		G.dig.trace = (on === undefined) ? !G.dig.trace : !!on;
		G.dig.events.push({type: "traceToggled", on: G.dig.trace});
		status(G.dig.trace ? "Trace on: two clicks on one string follow it; two clicks on a surface follow the surface (T toggles)"
			: "Trace off (T toggles)", 3000);
		return G.dig.trace;
	};
	G.traceItems = [];
	G.setTraceItems = function (spec) {
		G.traceItems = (spec.items || []).map(function (it) {
			return {id: it.id, closed: !!it.closed, w: it.pts.map(function (p) { return G.mapToWorld(p[0], p[1], p[2]); })};
		});
	};
	function traceIndex(p) {
		for (var i = 0; i < G.traceItems.length; i++) {
			var w = G.traceItems[i].w;
			for (var k = 0; k < w.length; k++) if (w[k].distanceToSquared(p) < 1e-6) return [i, k];
		}
		return null;
	}
	// points strictly between a and b: along a string they both lie on, or draped on the surface
	function tracePath(a, ak, b, bk) {
		var ia = traceIndex(a), ib = traceIndex(b);
		if (ia && ib && ia[0] === ib[0] && ia[1] !== ib[1]) {
			var it = G.traceItems[ia[0]], w = it.w, n = w.length, i = ia[1], j = ib[1], out = [], k;
			if (it.closed) {
				var m = n - 1;                                   // last vertex repeats the first
				i %= m; j %= m;
				var fwd = (j - i + m) % m, back = (i - j + m) % m;
				if (fwd <= back) for (k = 1; k < fwd; k++) out.push(w[(i + k) % m].clone());
				else for (k = 1; k < back; k++) out.push(w[(i - k + m) % m].clone());
			} else if (j > i) for (k = i + 1; k < j; k++) out.push(w[k].clone());
			else for (k = i - 1; k > j; k--) out.push(w[k].clone());
			return {pts: out, kind: "strings"};
		}
		if (ak === "surface" && bk === "surface") {
			var off = offsetOf(app.renderer.domElement), qa = a.clone().project(app.camera), qb = b.clone().project(app.camera);
			var ax = (qa.x + 1) / 2 * app.width, ay = (1 - qa.y) / 2 * app.height;
			var bx = (qb.x + 1) / 2 * app.width, by = (1 - qb.y) / 2 * app.height;
			var len = Math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)), steps = Math.min(200, Math.floor(len / 8));
			var g = G.sectionGeom(), plane = g ? new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C) : null, res = [];
			for (var s2 = 1; s2 < steps; s2++) {
				var t = s2 / steps, hp = G.pick(off.left + ax + (bx - ax) * t, off.top + ay + (by - ay) * t);
				if (!hp) continue;
				if (plane) hp = plane.projectPoint(hp, new THREE.Vector3());
				res.push(hp);
			}
			return {pts: res, kind: "surface"};
		}
		return null;
	}

	function digEnsureObj() {
		if (G.digObj && G.digObj.parent) return;
		var grp = new THREE.Group();
		grp.name = "q2t4 digitise";
		grp.renderOrder = 999;
		var mk = function (geomCls, mtl, name) { var o = new geomCls(new THREE.BufferGeometry(), mtl); o.name = name; o.renderOrder = 999; grp.add(o); return o; };
		mk(THREE.Line, new THREE.LineBasicMaterial({color: "#ffe000", depthTest: false}), "line");
		mk(THREE.Line, new THREE.LineBasicMaterial({color: "#ffe000", depthTest: false, transparent: true, opacity: 0.6}), "rubber");
		mk(THREE.Points, new THREE.PointsMaterial({color: "#ffe000", size: 7, sizeAttenuation: false, depthTest: false}), "verts");
		mk(THREE.Points, new THREE.PointsMaterial({color: "#ffffff", size: 13, sizeAttenuation: false, depthTest: false}), "marker");
		app.scene.add(grp);
		G.digObj = grp;
	}

	function setGeom(obj, pts) {
		obj.geometry.dispose();
		obj.geometry = new THREE.BufferGeometry().setFromPoints(pts);
		obj.visible = pts.length > 0;
	}

	G.digRedraw = function () {
		digEnsureObj();
		var d = G.dig, o = G.digObj;
		setGeom(o.getObjectByName("line"), d.pts.length > 1 ? d.pts : []);
		setGeom(o.getObjectByName("verts"), d.pts);
		var h = d.hover;
		setGeom(o.getObjectByName("rubber"), (h && d.pts.length) ? [d.pts[d.pts.length - 1], h.p] : []);
		var mk = o.getObjectByName("marker");
		setGeom(mk, h ? [h.p] : []);
		if (h) mk.material.color.set(SNAP_COLORS[h.kind] || "#ffffff");
		o.visible = d.active;
		render();
	};

	G.startDigitise = function (snapOpts) {
		if (G.tdel && G.tdel.active) G.stopTriDelete();
		if (snapOpts) for (var k in snapOpts) G.dig.snap[k] = snapOpts[k];
		if (G.queryMode) G.setQueryMode(false);
		G.cancelPick();
		G.dig.active = true;
		G.dig.pts = []; G.dig.kinds = []; G.dig.hover = null;
		app.renderer.domElement.style.cursor = "crosshair";
		G.dig.events.push({type: "mode", active: true});
		G._updateToolbar();
		G.digRedraw();
		status("Digitise: click to add points · Backspace undo · Enter/right-click/double-click finish · Esc cancel");
	};

	G.stopDigitise = function () {
		if (!G.dig.active) return;
		G.dig.active = false;
		G.dig.pts = []; G.dig.kinds = []; G.dig.hover = null;
		app.renderer.domElement.style.cursor = "";
		G.dig.events.push({type: "mode", active: false});
		G._updateToolbar();
		G.digRedraw();
		status("", 1);
	};

	G.setSnap = function (opts) { for (var k in opts) G.dig.snap[k] = opts[k]; };

	G.digHover = function (cx, cy) {
		var now = Date.now();
		G._hoverXY = [cx, cy];
		if (G._hoverT && now - G._hoverT < 40) {          // throttle (~25 Hz); catch up once
			if (!G._hoverPending) {
				G._hoverPending = setTimeout(function () { G._hoverPending = null; G.digHover(G._hoverXY[0], G._hoverXY[1]); }, 45);
			}
			return;
		}
		G._hoverT = now;
		G.dig.hover = G.snapAt(cx, cy);
		G.digRedraw();
	};

	G.digAddPoint = function (cx, cy) {
		var s = G.snapAt(cx, cy);
		if (!s) { status("No snap target here (turn on a section, or click a surface)", 3000); return; }
		var d = G.dig, last = d.pts[d.pts.length - 1];
		if (last && last.distanceTo(s.p) < 1e-6) return;      // ignore duplicate (double-click)
		if (d.trace && last) {
			var tp = tracePath(last, d.kinds[d.kinds.length - 1], s.p, s.kind);
			if (tp) for (var ti = 0; ti < tp.pts.length; ti++) { d.pts.push(tp.pts[ti]); d.kinds.push(tp.kind); }
		}
		d.pts.push(s.p.clone());
		d.kinds.push(s.kind);
		G.digRedraw();
	};

	G.digUndo = function () { G.dig.pts.pop(); G.dig.kinds.pop(); G.digRedraw(); };

	G.digCancel = function () {
		if (G.dig.pts.length) { G.dig.pts = []; G.dig.kinds = []; G.digRedraw(); status("String cancelled", 2000); }
		else G.stopDigitise();
	};

	G.digFinish = function () {
		var d = G.dig;
		if (d.pts.length >= 2) {
			var pts = d.pts.map(function (p) { var m = G.worldToMap(p); return [m.x, m.y, m.z]; });
			d.events.push({type: "string", pts: pts, kinds: d.kinds.slice()});
			status("String saved (" + pts.length + " points)", 2500);
		} else if (d.pts.length) {
			status("A string needs at least 2 points", 2500);
			return;
		}
		d.pts = []; d.kinds = [];
		G.digRedraw();
	};

	// Python polls this: returns queued events as JSON and clears the queue
	G.takeEvents = function () {
		var ev = G.dig.events;
		G.dig.events = [];
		return JSON.stringify(ev);
	};

	// ------------------------------------------------------------------ query
	G.queryMode = false;
	G.queryObj = null;

	G.setQueryMode = function (on) {
		G.queryMode = !!on;
		if (on) { G.stopDigitise(); G.cancelPick(); }
		app.renderer.domElement.style.cursor = on ? "help" : (G.dig.active ? "crosshair" : "");
		if (!on) G.clearQuery();
		G._updateToolbar();
		status(on ? "Query: click an object to see its attributes (Esc clears)" : "", on ? 0 : 1);
	};

	// owner of a raycast hit: our wireframe id, or a Qgis2threejs layer id
	function hitOwner(obj) {
		var o = obj;
		while (o) {
			if (o.userData && o.userData.q2t4Id !== undefined) return {mesh: o.userData.q2t4Id};
			if (o.userData && o.userData.layerId !== undefined) return {layerId: o.userData.layerId};
			o = o.parent;
		}
		return {};
	}

	G.queryAt = function (cx, cy) {
		var off = offsetOf(app.renderer.domElement), cam = app.camera;
		var v = new THREE.Vector2(((cx - off.left) / app.width) * 2 - 1, -((cy - off.top) / app.height) * 2 + 1);
		var ray = new THREE.Raycaster();
		ray.setFromCamera(v, cam);
		var k = cam.isPerspectiveCamera ? 2 * Math.tan(cam.fov * Math.PI / 360) / app.height
										: (cam.top - cam.bottom) / cam.zoom / app.height;
		// surfaces only (meshes): lines/points are tested in Python with a pixel tolerance
		var targets = [];
		app.scene.visibleObjects(false).forEach(function (o) { if (o.isMesh && !(o.material && o.material.isMeshLineMaterial)) targets.push(o); });
		for (var id in G.meshes) if (G.meshes[id].obj.visible) targets.push(G.meshes[id].obj.getObjectByName("faces"));
		var hits = ray.intersectObjects(targets), hit = null;
		for (var i = 0; i < hits.length; i++) {
			if (G.insideSection(hits[i].point)) {
				var ow = hitOwner(hits[i].object);
				hit = {p: [hits[i].point.x, hits[i].point.y, hits[i].point.z], t: hits[i].distance,
					   mesh: ow.mesh === undefined ? null : ow.mesh, jsLayerId: ow.layerId === undefined ? null : ow.layerId};
				break;
			}
		}
		var blk = blockPick(ray.ray);
		if (blk && (!hit || blk.t < hit.t)) {
			hit = {p: [blk.p.x, blk.p.y, blk.p.z], t: blk.t, mesh: null, jsLayerId: null,
				   block: blk.id, cls: blk.cls, instance: blk.instance};
		}
		var u = app.scene.userData;
		G.dig.events.push({type: "query", o: [ray.ray.origin.x, ray.ray.origin.y, ray.ray.origin.z],
			d: [ray.ray.direction.x, ray.ray.direction.y, ray.ray.direction.z], k: k, ortho: !!cam.isOrthographicCamera,
			hit: hit, scene: {origin: u.origin, be: u.baseExtent, zScale: u.zScale}});
		G._queryMarker(hit ? new THREE.Vector3().fromArray(hit.p) : null);
		G._showQueryPanel("<i>Querying…</i>");
	};

	function ensureQueryObj() {
		if (G.queryObj && G.queryObj.parent) return G.queryObj;
		var grp = new THREE.Group();
		grp.name = "q2t4 query";
		var line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#00ffff", depthTest: false, linewidth: 3}));
		line.name = "hl"; line.renderOrder = 1000;
		var pts = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#00ffff", size: 9, sizeAttenuation: false, depthTest: false}));
		pts.name = "pt"; pts.renderOrder = 1001;
		grp.add(line); grp.add(pts);
		app.scene.add(grp);
		G.queryObj = grp;
		return grp;
	}

	G._queryMarker = function (p) {
		var g = ensureQueryObj();
		setGeom(g.getObjectByName("pt"), p ? [p] : []);
		render();
	};

	// called from Python: html for the panel; highlight = [[x,y,z] map coords] or null; point = [x,y,z] map
	G.showQueryResult = function (htmlStr, highlight, point) {
		var g = ensureQueryObj();
		setGeom(g.getObjectByName("hl"), highlight ? highlight.map(function (q) { return G.mapToWorld(q[0], q[1], q[2]); }) : []);
		if (point) setGeom(g.getObjectByName("pt"), [G.mapToWorld(point[0], point[1], point[2])]);
		G._showQueryPanel(htmlStr);
		render();
	};

	G.clearQuery = function () {
		if (G.queryObj) {
			setGeom(G.queryObj.getObjectByName("hl"), []);
			setGeom(G.queryObj.getObjectByName("pt"), []);
		}
		var el = document.getElementById("q2t4-query");
		if (el) el.style.display = "none";
		render();
	};

	G._showQueryPanel = function (htmlStr) {
		var el = document.getElementById("q2t4-query");
		if (el && el._v !== VERSION) { el.parentNode.removeChild(el); el = null; }
		if (!el) {
			var oldSt = document.getElementById("q2t4-query-style");
			if (oldSt) oldSt.parentNode.removeChild(oldSt);
			el = document.createElement("div");
			el._v = VERSION;
			el.id = "q2t4-query";
			var st = document.createElement("style");
			st.id = "q2t4-query-style";
			st.textContent =
				"#q2t4-query{position:absolute;left:auto !important;right:12px;top:12px;z-index:21;width:260px;max-height:70%;overflow:auto;" +
				"background:rgba(22,33,43,.96);color:#E6EBEF;border:1px solid #3FC4D4;border-radius:8px;padding:8px 12px;font:12px sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.4)}" +
				"#q2t4-query .q2t4-q-big{font:700 24px Consolas,'JetBrains Mono',monospace;color:#E2A72E}#q2t4-query .q2t4-q-unit{color:#9FB0BC;margin-left:6px}" +
				"#q2t4-query .q2t4-q-title{font-weight:bold;margin-right:18px}" +
				"#q2t4-query .q2t4-q-xyz{color:#9FB0BC;margin:2px 0 4px}" +
				"#q2t4-query table{border-collapse:collapse}#q2t4-query td{padding:1px 6px 1px 0;vertical-align:top;word-break:break-word}" +
				"#q2t4-query td:last-child{max-width:220px}#q2t4-query{overflow-x:hidden}" +
				"#q2t4-query td:first-child{color:#9FB0BC}#q2t4-query .q2t4-q-note{color:#9FB0BC;margin-top:2px}" +
				"#q2t4-query .q2t4-q-x{position:absolute;right:6px;top:2px;cursor:pointer;font-size:15px}";
			document.head.appendChild(st);
			(document.getElementById("view") || document.body).appendChild(el);
			el.addEventListener("mousedown", function (e) { e.stopPropagation(); });
		}
		el.innerHTML = "<span class='q2t4-q-x' title='Close'>&times;</span>" + htmlStr;
		el.querySelector(".q2t4-q-x").addEventListener("click", function (e) { e.stopPropagation(); G.clearQuery(); });
		el.style.display = "block";
	};

	// ------------------------------------------------------------------- HUD
	// Section chip (top left) and plan inset (bottom left). Python sends both:
	// {chip: "Section 270.3° · …" | null, inset: {items:[{bbox:[x0,y0,x1,y1], color}], slab:[[x,y]..], line:[[x,y],[x,y]], dir:[dx,dy]} | null}
	G.hud = {chip: null, inset: null};
	G.chipOn = true;           // section label (chip) top left
	G.setChip = function (on) { G.chipOn = !!on; G.setHud(); };
	G.insetOn = true;          // plan inset of the section ("section window")
	G.insetPos = "bl";         // "bl" bottom left | "tl" top left (under the section chip)
	G.setInset = function (on, pos) {
		if (on !== undefined && on !== null) G.insetOn = !!on;
		if (pos === "tl" || pos === "bl") G.insetPos = pos;
		G.setHud();
	};
	G.toolbarVisible = false;          // the in-view bar is only a fallback for when the native toolbar fails

	function hudRoot() {
		var el = document.getElementById("q2t4-hud");
		if (el) return el;
		if (!document.getElementById("q2t4-hud-style")) {
			var st = document.createElement("style");
			st.id = "q2t4-hud-style";
			st.textContent =
				"#q2t4-hud .chip{position:absolute;left:8px;display:inline-flex;align-items:center;gap:8px;padding:5px 12px;" +
				"background:rgba(22,33,43,.92);border:1px solid #F0472E;border-radius:999px;color:#E6EBEF;font:12px Consolas,'JetBrains Mono',monospace;z-index:20}" +
				"#q2t4-hud .chip i{width:8px;height:8px;border-radius:50%;background:#F0472E;display:inline-block}" +
				"#q2t4-hud .inset{position:absolute;left:8px;z-index:20;border:1px solid #2A3742;border-radius:8px;overflow:hidden;background:#16212B}";
			document.head.appendChild(st);
		}
		el = document.createElement("div");
		el.id = "q2t4-hud";
		(document.getElementById("view") || document.body).appendChild(el);
		return el;
	}

	G.setHud = function (h) {
		if (h) { if ("chip" in h) G.hud.chip = h.chip; if ("inset" in h) G.hud.inset = h.inset;
				 if ("scale" in h) { G.hud.scale = h.scale; if (G.updateScale) G.updateScale(); } }
		var el = hudRoot(), out = "";
		var top = G.toolbarVisible ? 46 : 8;
		var chip = G.chipOn && G.hud.chip;
		if (chip) out += "<div class='chip' style='top:" + top + "px'><i></i>" + G.hud.chip.replace(/</g, "&lt;") + "</div>";
		var ins = G.hud.inset;
		if (G.insetOn && ins && ((ins.items && ins.items.length) || ins.line)) {
			var xs = [], ys = [], items = ins.items || [];
			(ins.slab || []).forEach(function (p) { xs.push(p[0]); ys.push(p[1]); });
			if (xs.length) {
				// with a section: fit the section plus the objects near it, not every object loaded
				var sx0 = Math.min.apply(null, xs), sx1 = Math.max.apply(null, xs), sy0 = Math.min.apply(null, ys), sy1 = Math.max.apply(null, ys);
				var m = 0.5 * Math.max(sx1 - sx0, sy1 - sy0);
				items = items.filter(function (it) {
					var b = it.bbox;
					return b[2] >= sx0 - m && b[0] <= sx1 + m && b[3] >= sy0 - m && b[1] <= sy1 + m;
				});
			}
			items.forEach(function (it) { xs.push(it.bbox[0], it.bbox[2]); ys.push(it.bbox[1], it.bbox[3]); });
			var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
			var W = 168, H = 118, pad = 10, sc = Math.min((W - 2 * pad) / Math.max(x1 - x0, 1e-6), (H - 2 * pad) / Math.max(y1 - y0, 1e-6));
			var ox = (W - (x1 - x0) * sc) / 2, oy = (H - (y1 - y0) * sc) / 2;
			var X = function (x) { return (ox + (x - x0) * sc).toFixed(1); }, Y = function (y) { return (H - oy - (y - y0) * sc).toFixed(1); };
			var svg = "<svg width='" + W + "' height='" + H + "' viewBox='0 0 " + W + " " + H + "' role='img' aria-label='Plan inset'>";
			items.forEach(function (it) {
				var b = it.bbox;
				svg += "<rect x='" + X(b[0]) + "' y='" + Y(b[3]) + "' width='" + ((b[2] - b[0]) * sc).toFixed(1) + "' height='" + ((b[3] - b[1]) * sc).toFixed(1) +
					"' fill='" + it.color + "' fill-opacity='0.25' stroke='" + it.color + "' stroke-width='1'></rect>";
			});
			if (ins.slab) svg += "<polygon points='" + ins.slab.map(function (p) { return X(p[0]) + "," + Y(p[1]); }).join(" ") + "' fill='#F0472E' fill-opacity='0.3'></polygon>";
			if (ins.line) {
				var a = ins.line[0], b2 = ins.line[1];
				svg += "<line x1='" + X(a[0]) + "' y1='" + Y(a[1]) + "' x2='" + X(b2[0]) + "' y2='" + Y(b2[1]) + "' stroke='#F0472E' stroke-width='2'></line>";
				if (ins.dir) {
					var mx = (a[0] + b2[0]) / 2, my = (a[1] + b2[1]) / 2, L = 14 / sc;
					svg += "<line x1='" + X(mx) + "' y1='" + Y(my) + "' x2='" + X(mx + ins.dir[0] * L) + "' y2='" + Y(my + ins.dir[1] * L) +
						"' stroke='#F0472E' stroke-width='2' marker-end=''></line><circle cx='" + X(mx + ins.dir[0] * L) + "' cy='" + Y(my + ins.dir[1] * L) + "' r='2.5' fill='#F0472E'></circle>";
				}
			}
			svg += "<text x='8' y='14' fill='#9FB0BC' font-size='10' font-family='sans-serif'>N &#8593;</text></svg>";
			var pos = G.insetPos === "tl" ? "top:" + (top + (chip ? 34 : 0)) + "px" : "bottom:8px";
			out += "<div class='inset' style='" + pos + "'>" + svg + "</div>";
		}
		el.innerHTML = out;
	};

	G.setToolbarVisible = function (on) {
		G.toolbarVisible = !!on;
		var tb = document.getElementById("q2t4-toolbar");
		if (tb) tb.style.display = on ? "" : "none";
		var q = document.getElementById("q2t4-query");
		if (q) q.style.top = on ? "44px" : "12px";
		G.setHud();
	};

	// cursor position / mode for the status bar (polled by Python)
	G.cursor = null;
	G._updateCursor = function (cx, cy) {
		var now = Date.now();
		if (G._curT && now - G._curT < 90) return;
		G._curT = now;
		var p = null, src = null;
		if (G.dig.active && G.dig.hover) { p = G.dig.hover.p; src = G.dig.hover.kind; }
		else {
			var g = G.sectionGeom();
			if (g && G.section && G.section.enabled !== false) {
				var plane = new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C);
				p = rayAt(cx, cy).ray.intersectPlane(plane, new THREE.Vector3()); src = "section";
			} else { var pi = G.pickInfo(cx, cy); p = pi ? pi.p : null; src = pi ? pi.kind : null; }
		}
		G.cursor = p ? {m: G.worldToMap(p), src: src} : null;
	};
	G.statusInfo = function () {
		var mode = G.dig.active ? "String" : (G.pickMode === "orbit" ? "Orbit point" : (G.queryMode ? "Query" : "Navigate"));
		var c = G.cursor;
		return JSON.stringify({c: c ? [c.m.x, c.m.y, c.m.z] : null, src: c ? c.src : null, mode: mode,
			tris: app.renderer.info.render.triangles, events: G.takeEvents()});
	};
	G.gpuInfo = function () {
		var gl = app.renderer.getContext(), ext = gl.getExtension("WEBGL_debug_renderer_info");
		return (ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) + " · " +
			(app.renderer.capabilities.isWebGL2 ? "WebGL 2" : "WebGL 1");
	};

	// ------------------------------------------------------------- toolbar
	G.buildToolbar = function () {
		if (document.getElementById("q2t4-toolbar")) return;
		var view = document.getElementById("view") || document.body;

		if (!document.getElementById("q2t4-style")) {
			var st = document.createElement("style");
			st.id = "q2t4-style";
			st.textContent =
				"#q2t4-toolbar{position:absolute;top:8px;left:8px;z-index:20;font:12px sans-serif}" +
				"#q2t4-toolbar button{margin-right:4px;padding:4px 8px;border:1px solid #888;border-radius:4px;background:rgba(255,255,255,.85);cursor:pointer}" +
				"#q2t4-toolbar button:hover{background:#fff}" +
				"#q2t4-toolbar button.active{background:#ffd54f;border-color:#b8860b}";
			document.head.appendChild(st);
		}
		var bar = document.createElement("div");
		bar.id = "q2t4-toolbar";

		function btn(id, text, title, fn) {
			var b = document.createElement("button");
			b.id = id; b.innerHTML = text; b.title = title;
			b.addEventListener("click", function (e) { e.stopPropagation(); fn(e); });
			b.addEventListener("mousedown", function (e) { e.stopPropagation(); });
			bar.appendChild(b);
			return b;
		}
		btn("q2t4-recentre", "&#8982; Recentre",
			"Fit wireframes (Shift: everything visible) and orbit around their centre",
			function (e) { G.recentre(e.shiftKey); });
		btn("q2t4-dig", "&#9998; String",
			"Digitise a 3D string: click points (snaps to drillholes, strings, surfaces, section plane)",
			function () { if (G.dig.active) G.stopDigitise(); else G.startDigitise(); });
		var sep = document.createElement("span");
		sep.style.marginRight = "6px";
		bar.appendChild(sep);
		btn("q2t4-view-plan", "Plan", "Plan view: looking down, north up", function () { G.setView("plan"); });
		btn("q2t4-view-N", "N", "Facing north (looking towards north)", function () { G.setView("N"); });
		btn("q2t4-view-E", "E", "Facing east", function () { G.setView("E"); });
		btn("q2t4-view-S", "S", "Facing south", function () { G.setView("S"); });
		btn("q2t4-view-W", "W", "Facing west", function () { G.setView("W"); });
		var sep2 = document.createElement("span");
		sep2.style.marginRight = "6px";
		bar.appendChild(sep2);
		btn("q2t4-query-btn", "&#9432; Query",
			"Query mode: click an object to see its attributes (e.g. Au ppm). Respects the section.",
			function () { G.setQueryMode(!G.queryMode); });
		btn("q2t4-orbit", "&#9678; Orbit point",
			"Click a surface to make it the orbit point / screen centre (or double-click anywhere)",
			function () { if (G.pickMode) G.cancelPick(); else G.startOrbitPick(); });

		view.appendChild(bar);
		G._updateToolbar();
	};

	G._updateToolbar = function () {
		var b = document.getElementById("q2t4-orbit");
		if (b) b.className = (G.pickMode === "orbit") ? "active" : "";
		var d = document.getElementById("q2t4-dig");
		if (d) d.className = G.dig.active ? "active" : "";
		var q = document.getElementById("q2t4-query-btn");
		if (q) q.className = G.queryMode ? "active" : "";
	};

	// Same transform Q2T uses for (non-clipped) vector layers: outer group at the
	// base-extent pivot, rotated by map rotation; data relative to extent centre.
	G.updateTransforms = function () {
		if (!sceneReady() || !G.group) return;
		var u = app.scene.userData, be = u.baseExtent, o = u.origin, zs = u.zScale;
		var rot = (be.rotation || 0) * Math.PI / 180;

		G.group.position.set(be.cx - o.x, be.cy - o.y, 0);
		G.group.rotation.set(0, 0, rot);

		for (var id in G.meshes) {
			var m = G.meshes[id], off = m.spec.offset;
			m.obj.position.set(off[0] - be.cx, off[1] - be.cy, (off[2] - o.z) * zs);
			m.obj.scale.set(1, 1, zs);
		}
		for (var bid in G.blocks) {
			var bm = G.blocks[bid], bo = bm.spec.offset, r = bm.spec.rot;
			if (r) {        // rotated model (Datamine / Vulcan): world = offset + R . model, then z exaggeration
				var M = new THREE.Matrix4().set(r[0], r[1], r[2], 0, r[3], r[4], r[5], 0, r[6], r[7], r[8], 0, 0, 0, 0, 1);
				bm.obj.matrixAutoUpdate = false;
				bm.obj.matrix.makeTranslation(bo[0] - be.cx, bo[1] - be.cy, (bo[2] - o.z) * zs)
					.multiply(new THREE.Matrix4().makeScale(1, 1, zs)).multiply(M);
				bm.obj.matrixWorldNeedsUpdate = true;
			} else {
				bm.obj.position.set(bo[0] - be.cx, bo[1] - be.cy, (bo[2] - o.z) * zs);
				bm.obj.scale.set(1, 1, zs);
			}
		}
		for (var name in G.overlays) {
			var ov = G.overlays[name], of = ov.spec.offset;
			ov.obj.position.set(of[0] - be.cx, of[1] - be.cy, (of[2] - o.z) * zs);
			ov.obj.scale.set(1, 1, zs);
		}
		if (G._placeTubes) G._placeTubes();
		G.group.updateMatrixWorld(true);
		for (var nm in G.overlays) G.overlays[nm].snapWorld = null;      // recomputed lazily
		for (var ls in (G.labelSets || {})) {           // labels hold world positions: re-place them
			var L = G.labelSets[ls];
			for (var li = 0; li < L.items.length; li++) { var mp = L.map[li].p; L.items[li].w = G.mapToWorld(mp[0], mp[1], mp[2]); }
		}
		G.updateSection();
		if (G.updateLabels) G.updateLabels();
	};

	// ------------------------------------------------------------- overlays
	// Polyline overlays (drillholes, strings). spec: {offset:[x,y,z], pos:b64 f32 xyz,
	// seg:b64 u32 index pairs, snap:b64 u32 vertex indices, color, pointSize, visible}
	G.setOverlay = function (name, spec) {
		if (!G.attach()) return false;
		G.removeOverlay(name, true);
		if (!spec) { render(); return true; }

		var pos = new Float32Array(b64ToBuffer(spec.pos));
		var seg = new Uint32Array(b64ToBuffer(spec.seg));
		var attr = new THREE.BufferAttribute(pos, 3);
		var obj = new THREE.Group();
		obj.name = "q2t4 overlay " + name;

		// Lines are drawn non-indexed (each segment's two vertices copied out): indexed LINES sharing
		// the points' position buffer intermittently drew nothing in QtWebKit (QGIS 3 / Qgis2threejs 2.x),
		// leaving only the vertex dots.
		var lpos = new Float32Array(seg.length * 3), si, sk;
		for (si = 0; si < seg.length; si++) for (sk = 0; sk < 3; sk++) lpos[si * 3 + sk] = pos[seg[si] * 3 + sk];
		var lg = new THREE.BufferGeometry();
		lg.setAttribute("position", new THREE.BufferAttribute(lpos, 3));
		lg.computeBoundingSphere();
		var lmat;
		if (spec.colors) {            // per-vertex colours (block outlines by grade class)
			var vc = new Uint8Array(b64ToBuffer(spec.colors)), lcol = new Uint8Array(seg.length * 3);
			for (si = 0; si < seg.length; si++) for (sk = 0; sk < 3; sk++) lcol[si * 3 + sk] = vc[seg[si] * 3 + sk];
			lg.setAttribute("color", new THREE.BufferAttribute(lcol, 3, true));
			lmat = new THREE.LineBasicMaterial({vertexColors: VCOLORS});
		} else {
			lmat = new THREE.LineBasicMaterial({color: spec.color || "#00c8ff"});
		}
		if (spec.onTop) { lmat.depthTest = false; lmat.transparent = true; }
		var lines = new THREE.LineSegments(lg, lmat);
		if (spec.onTop) lines.renderOrder = 900;
		lines.name = "lines";
		obj.add(lines);

		var snap = spec.snap ? new Uint32Array(b64ToBuffer(spec.snap)) : new Uint32Array(0);
		if (spec.pointSize) {
			var pg = new THREE.BufferGeometry();
			pg.setAttribute("position", attr);
			pg.setIndex(new THREE.BufferAttribute(snap, 1));
			pg.computeBoundingSphere();
			var pts = new THREE.Points(pg, new THREE.PointsMaterial({color: spec.pointColor || spec.color || "#00c8ff",
				size: spec.pointSize, sizeAttenuation: false}));
			pts.name = "points";
			obj.add(pts);
		}
		obj.visible = spec.visible !== false;
		var meta = {offset: spec.offset, color: spec.color};
		G.overlays[name] = {obj: obj, spec: meta, pos: pos, seg: seg, snap: snap, snapWorld: null};
		G.group.add(obj);
		G.updateTransforms();
		render();
		return true;
	};

	// ------------------------------------------------------------- block models
	// spec: {name, offset, opacity, classes: [{label, color, n, pos: b64 f32 xyz centres, size: b64 f32 xyz}]}
	// Box faces as ordinary geometry (QtWebKit's WebGL has no instanced drawing).
	// Per class: centres, sizes and a 6-bit mask of faces to draw (shared walls culled in Python).
	var FACE_DEF = [   // [normal, 4 corner sign vectors]
		[[1, 0, 0], [[1, -1, -1], [1, 1, -1], [1, 1, 1], [1, -1, 1]]],
		[[-1, 0, 0], [[-1, 1, -1], [-1, -1, -1], [-1, -1, 1], [-1, 1, 1]]],
		[[0, 1, 0], [[1, 1, -1], [-1, 1, -1], [-1, 1, 1], [1, 1, 1]]],
		[[0, -1, 0], [[-1, -1, -1], [1, -1, -1], [1, -1, 1], [-1, -1, 1]]],
		[[0, 0, 1], [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]]],
		[[0, 0, -1], [[-1, 1, -1], [1, 1, -1], [1, -1, -1], [-1, -1, -1]]]
	];

	// returns [{geom, faceBlock}] chunks of <= 16000 faces (64000 vertices: 16-bit indices,
	// since WebGL1 may lack OES_element_index_uint)
	function buildBoxFaces(pos, siz, mask) {
		var MAXF = 16000, n = mask.length, i, b, chunks = [];
		var faces = [];
		for (i = 0; i < n; i++) for (b = 0; b < 6; b++) if (mask[i] & (1 << b)) faces.push(i * 8 + b);
		for (var start = 0; start < faces.length; start += MAXF) {
			var nf = Math.min(MAXF, faces.length - start);
			var P = new Float32Array(nf * 12), N = new Float32Array(nf * 12), I = new Uint16Array(nf * 6), faceBlock = new Uint32Array(nf);
			for (var f = 0; f < nf; f++) {
				var code = faces[start + f]; i = code >> 3; b = code & 7;
				var cx = pos[i * 3], cy = pos[i * 3 + 1], cz = pos[i * 3 + 2];
				var hx = siz[i * 3] * 0.4995, hy = siz[i * 3 + 1] * 0.4995, hz = siz[i * 3 + 2] * 0.4995;
				var fd = FACE_DEF[b], nrm = fd[0], cs = fd[1];
				for (var k = 0; k < 4; k++) {
					var o = (f * 4 + k) * 3;
					P[o] = cx + cs[k][0] * hx; P[o + 1] = cy + cs[k][1] * hy; P[o + 2] = cz + cs[k][2] * hz;
					N[o] = nrm[0]; N[o + 1] = nrm[1]; N[o + 2] = nrm[2];
				}
				var vv = f * 4, j = f * 6;
				I[j] = vv; I[j + 1] = vv + 1; I[j + 2] = vv + 2; I[j + 3] = vv; I[j + 4] = vv + 2; I[j + 5] = vv + 3;
				faceBlock[f] = i;
			}
			var g = new THREE.BufferGeometry();
			g.setAttribute("position", new THREE.BufferAttribute(P, 3));
			g.setAttribute("normal", new THREE.BufferAttribute(N, 3));
			g.setIndex(new THREE.BufferAttribute(I, 1));
			g.computeBoundingSphere();
			g.computeBoundingBox();
			chunks.push({geom: g, faceBlock: faceBlock});
		}
		return chunks;
	}

	G.setBlocks = function (id, spec) {
		if (!G.attach()) return false;
		G.removeBlocks(id, true);
		var obj = new THREE.Group();
		obj.name = "q2t4 blocks " + id;
		var op = spec.opacity === undefined ? 1 : spec.opacity, boxes = [];
		spec.classes.forEach(function (c, ci) {
			var bpos = new Float32Array(b64ToBuffer(c.pos)), bsize = new Float32Array(b64ToBuffer(c.size));
			boxes.push({pos: bpos, size: bsize});
			var chunks = buildBoxFaces(bpos, bsize, new Uint8Array(b64ToBuffer(c.mask)));
			var mtl = new THREE.MeshLambertMaterial({color: c.color, side: THREE.DoubleSide, transparent: op < 1,
													 opacity: op, depthWrite: op >= 1});
			chunks.forEach(function (ch, k) {
				var mesh = new THREE.Mesh(ch.geom, mtl);
				mesh.name = "class " + ci + " part " + k;
				mesh.userData = {q2t4Block: id, cls: ci, faceBlock: ch.faceBlock};
				obj.add(mesh);
			});
		});
		obj.userData = {q2t4BlockModel: id};
		obj.visible = spec.visible !== false;
		G.blocks[id] = {obj: obj, spec: {name: spec.name, offset: spec.offset, rot: spec.rot || null}, boxes: boxes};
		G.group.add(obj);
		G.updateTransforms();
		render();
		return true;
	};

	G.removeBlocks = function (id, noRender) {
		var b = G.blocks[id];
		if (!b) return;
		G.group.remove(b.obj);
		b.obj.traverse(function (o) { if (o.material) o.material.dispose(); if (o.geometry) o.geometry.dispose(); });   // shared material: dispose is idempotent
		delete G.blocks[id];
		if (!noRender) render();
	};

	G.setBlocksVisible = function (id, visible) {
		if (G.blocks[id]) { G.blocks[id].obj.visible = visible; render(); }
	};

	G.removeOverlay = function (name, noRender) {
		var ov = G.overlays[name];
		if (!ov) return;
		G.group.remove(ov.obj);
		ov.obj.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
		delete G.overlays[name];
		if (!noRender) render();
	};

	G.setOverlayVisible = function (name, visible) {
		if (G.overlays[name]) { G.overlays[name].obj.visible = visible; render(); }
	};

	function overlaySnapWorld(ov) {
		if (ov.snapWorld) return ov.snapWorld;
		ov.obj.updateMatrixWorld(true);
		var m = ov.obj.matrixWorld, n = ov.snap.length, out = new Float32Array(n * 3), v = new THREE.Vector3();
		for (var i = 0; i < n; i++) {
			var k = ov.snap[i] * 3;
			v.set(ov.pos[k], ov.pos[k + 1], ov.pos[k + 2]).applyMatrix4(m);
			out[i * 3] = v.x; out[i * 3 + 1] = v.y; out[i * 3 + 2] = v.z;
		}
		ov.snapWorld = out;
		return out;
	}

	// Vertices of the Qgis2threejs line / point layers ticked in the layer list (e.g. desurveyed
	// drillhole traces and intervals, collars) in world coordinates - they snap like our drillholes.
	// Cached per layer; rebuilt when its objects or their placement change.
	var Q2T_SNAP_MAX = 400000;
	var Q2T_CYL_ENDS = [[0, 0.5, 0], [0, -0.5, 0]], Q2T_CENTRE = [[0, 0, 0]];
	// local points to snap to for a Q2T object: null = every vertex (lines/points),
	// cylinder ends for Pipe/Cone segments and Cylinder/Cone symbols, centre for joints/spheres
	function q2tSnapLocal(o) {
		if (!o.visible || !o.geometry) return undefined;
		var g = o.geometry, gt = g.type || "";
		var ml = g.isMeshLine || (o.material && (o.material.isMeshLineMaterial || o.material.type === "MeshLineMaterial"));
		if (o.isLine || o.isLineSegments || o.isPoints || ml)
			return (g.attributes && g.attributes.position) ? null : undefined;
		if (o.isMesh && gt.indexOf("Cylinder") >= 0) return Q2T_CYL_ENDS;
		if (o.isMesh && (gt.indexOf("Sphere") >= 0 || gt.indexOf("Icosahedron") >= 0)) return Q2T_CENTRE;
		return undefined;
	}
	function q2tLayerSnapWorld() {
		var out = [];
		if (!app.scene || !app.scene.mapLayers) return out;
		for (var id in app.scene.mapLayers) {
			var L = app.scene.mapLayers[id];
			if (!L || !L.visible || (L.type !== "line" && L.type !== "point") || !L.objectGroup) continue;
			L.objectGroup.updateMatrixWorld(true);
			var objs = [], n = 0, nMesh = 0;
			L.objectGroup.traverse(function (o) {
				var loc = q2tSnapLocal(o);
				if (loc === undefined) return;
				objs.push([o, loc]);
				if (loc) { n += loc.length; nMesh++; } else n += o.geometry.attributes.position.count;
			});
			var key = objs.length + ":" + nMesh + ":" + n + ":" + (objs[0] ? objs[0][0].matrixWorld.elements.join(",") : "") +
				(objs.length > 1 ? ":" + objs[objs.length - 1][0].matrixWorld.elements.join(",") : "");
			if (L._q2t4Snap && L._q2t4SnapKey === key) { out.push(L._q2t4Snap); continue; }
			if (n > Q2T_SNAP_MAX) { L._q2t4Snap = new Float32Array(0); L._q2t4SnapKey = key; continue; }
			var w = new Float32Array(n * 3), v = new THREE.Vector3(), k = 0;
			objs.forEach(function (e) {
				var o = e[0], loc = e[1], m = o.matrixWorld, i;
				if (loc) {
					for (i = 0; i < loc.length; i++) {
						v.set(loc[i][0], loc[i][1], loc[i][2]).applyMatrix4(m);
						w[k++] = v.x; w[k++] = v.y; w[k++] = v.z;
					}
					return;
				}
				var a = o.geometry.attributes.position;
				for (i = 0; i < a.count; i++) {
					v.set(a.getX(i), a.getY(i), a.getZ(i)).applyMatrix4(m);
					w[k++] = v.x; w[k++] = v.y; w[k++] = v.z;
				}
			});
			L._q2t4Snap = w; L._q2t4SnapKey = key;
			out.push(w);
		}
		return out;
	}
	function nearestOf(arrays, sx, sy, bd, kind) {
		var best = null, v = new THREE.Vector3();
		for (var j = 0; j < arrays.length; j++) {
			var w = arrays[j];
			for (var i = 0; i < w.length; i += 3) {
				v.set(w[i], w[i + 1], w[i + 2]);
				if (G.sectionPlanes.length && !G.insideSection(v)) continue;
				var q = v.clone().project(app.camera);
				if (q.z > 1 || q.z < -1) continue;
				var px = (q.x + 1) / 2 * app.width, py = (1 - q.y) / 2 * app.height;
				var d = (px - sx) * (px - sx) + (py - sy) * (py - sy);
				if (d < bd) { bd = d; best = {p: v.clone(), kind: kind, d: d}; }
			}
		}
		return best;
	}

	// ------------------------------------------------------------ coordinates
	// map (x, y, z) -> world, using the same transform as Q2T vector layers
	G.mapToWorld = function (x, y, z) {
		var u = app.scene.userData, be = u.baseExtent;
		var v = new THREE.Vector3(x - be.cx, y - be.cy, ((z || 0) - u.origin.z) * u.zScale);
		G.group.updateMatrixWorld(true);
		return v.applyMatrix4(G.group.matrixWorld);
	};

	G.worldToMap = function (v) {
		var u = app.scene.userData, be = u.baseExtent;
		G.group.updateMatrixWorld(true);
		var inv = invert4(G.group.matrixWorld);
		var l = v.clone().applyMatrix4(inv);
		return {x: l.x + be.cx, y: l.y + be.cy, z: l.z / u.zScale + u.origin.z};
	};

	// horizontal map direction -> world direction (map rotation only)
	function dirToWorld(dx, dy) {
		var rot = (app.scene.userData.baseExtent.rotation || 0) * Math.PI / 180;
		return new THREE.Vector3(dx, dy, 0).applyAxisAngle(new THREE.Vector3(0, 0, 1), rot).normalize();
	}

	// ---------------------------------------------------------------- section
	// spec: {a:[x,y], b:[x,y], width, limitEnds, enabled, showFrame}
	G.setSection = function (spec) {
		G.section = spec;
		G.updateSection();
		render();
	};

	G.clearSection = function () {
		G.section = null;
		G.updateSection();
		render();
	};

	// geometry of the current section in world coords (null if none)
	G.sectionGeom = function () {
		var s = G.section;
		if (!s || !sceneReady() || !G.group) return null;
		var dx = s.b[0] - s.a[0], dy = s.b[1] - s.a[1], len = Math.sqrt(dx * dx + dy * dy);
		if (len <= 0) return null;
		var u = dirToWorld(dx, dy);                                      // along section A->B
		var n = new THREE.Vector3(0, 0, 1).cross(u).normalize();         // view direction (into screen)
		var A = G.mapToWorld(s.a[0], s.a[1], 0), B = G.mapToWorld(s.b[0], s.b[1], 0);
		A.z = B.z = 0;
		var C = A.clone().add(B).multiplyScalar(0.5);
		return {u: u, n: n, A: A, B: B, C: C, len: len, half: s.width / 2};
	};

	G.updateSection = function () {
		if (!window.app || !app.renderer) return;
		var g = G.sectionGeom(), s = G.section;
		var planes = [];
		if (g && s.enabled !== false) {
			planes.push(new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C.clone().addScaledVector(g.n, -g.half)));
			planes.push(new THREE.Plane().setFromNormalAndCoplanarPoint(g.n.clone().negate(), g.C.clone().addScaledVector(g.n, g.half)));
			if (s.limitEnds) {
				planes.push(new THREE.Plane().setFromNormalAndCoplanarPoint(g.u, g.A));
				planes.push(new THREE.Plane().setFromNormalAndCoplanarPoint(g.u.clone().negate(), g.B));
			}
		}
		G.sectionPlanes = planes;
		app.renderer.clippingPlanes = planes;
		G.patchClipping();

		// frame on the section's centre plane, spanning the visible z range
		if (G.sectionFrame) {
			G.sectionFrame.parent && G.sectionFrame.parent.remove(G.sectionFrame);
			G.sectionFrame.geometry.dispose();
			G.sectionFrame = null;
		}
		if (g && s.showFrame !== false) {
			var zr = sectionZRange(g), z0 = zr[0], z1 = zr[1];
			var pts = [
				new THREE.Vector3(g.A.x, g.A.y, z0), new THREE.Vector3(g.B.x, g.B.y, z0),
				new THREE.Vector3(g.B.x, g.B.y, z1), new THREE.Vector3(g.A.x, g.A.y, z1)
			];
			var geom = new THREE.BufferGeometry().setFromPoints(pts);
			G.sectionFrame = new THREE.LineLoop(geom, new THREE.LineBasicMaterial({color: "#e8341c"}));
			G.sectionFrame.name = "q2t4 section frame";
			app.scene.add(G.sectionFrame);
		}
	};

	// vertical extent for section frame/view (world z): wireframes, up to the top of
	// visible layers (DEM surface). Q2T layer boxes include DEM bases at z=0, so their
	// bottom is ignored; without wireframes use a square window below the top.
	// Our objects (wireframes, block models, overlays) whose boxes reach the section slab.
	// Block models count while their outlines are on the section even if the solid is hidden.
	function sectionContentBox(g) {
		var out = new THREE.Box3(), cands = [];
		G.group.updateMatrixWorld(true);
		for (var id in G.meshes) if (G.meshes[id].obj.visible) cands.push(G.meshes[id].obj.getObjectByName("faces"));
		for (var bid in G.blocks) if (G.blocks[bid].obj.visible || G.overlays.blocksec) cands.push(G.blocks[bid].obj);
		for (var nm in G.overlays) if (G.overlays[nm].obj.visible) cands.push(G.overlays[nm].obj);
		var corner = new THREE.Vector3();
		cands.forEach(function (o) {
			var b = new THREE.Box3().setFromObject(o);
			if (b.isEmpty()) return;
			var dn0 = Infinity, dn1 = -Infinity, du0 = Infinity, du1 = -Infinity;
			for (var i = 0; i < 8; i++) {
				corner.set(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, 0).sub(g.C);
				var dn = corner.dot(g.n), du = corner.dot(g.u);
				dn0 = Math.min(dn0, dn); dn1 = Math.max(dn1, dn); du0 = Math.min(du0, du); du1 = Math.max(du1, du);
			}
			if (dn0 <= g.half && dn1 >= -g.half && du0 <= g.len && du1 >= -g.len) out.union(b);
		});
		return out;
	}

	function sectionZRange(g) {
		var u = app.scene.userData, s = G.section || {};
		var cb = sectionContentBox(g);
		if (!cb.isEmpty()) {                    // fit what is actually on the section
			var t = cb.max.z, bt = cb.min.z;
			if (s.surfMax !== undefined) {
				t = Math.max(t, (s.surfMax - u.origin.z) * u.zScale);
				bt = Math.min(bt, (s.surfMin - u.origin.z) * u.zScale);
			}
			var pd = Math.max((t - bt) * 0.08, 2);
			return [bt - pd, t + pd];
		}
		var wf = visibleBox(true), all = app.scene.boundingBox(true);
		var hasWf = false;
		for (var id in G.meshes) if (G.meshes[id].obj.visible) { hasWf = true; break; }
		var top, bottom;
		if (s.surfMax !== undefined) {          // DEM profile sampled along the slab (map z)
			var sMax = (s.surfMax - u.origin.z) * u.zScale, sMin = (s.surfMin - u.origin.z) * u.zScale;
			top = hasWf ? Math.max(sMax, wf.max.z) : sMax;
			bottom = hasWf ? Math.min(sMin, wf.min.z) : sMin - g.len / 2;
		} else {
			top = all.isEmpty() ? (hasWf ? wf.max.z : 0) : Math.max(all.max.z, hasWf ? wf.max.z : -Infinity);
			bottom = hasWf ? wf.min.z : top - g.len;
		}
		var pad = Math.max((top - bottom) * 0.08, 2);
		return [bottom - pad, top + pad];
	}

	// Qgis2threejs "Thick Line" layers use MeshLineMaterial, a ShaderMaterial without the
	// clipping chunks, so renderer.clippingPlanes don't affect them. Add the chunks.
	function clipVert(src) {
		if (src.indexOf("clipping_planes_pars_vertex") >= 0) return src;
		src = "#include <clipping_planes_pars_vertex>\n" + src;
		// use three.js's own chunk (the varying is vViewPosition in r110, vClipPosition later);
		// block scope keeps this mvPosition apart from the one the fog chunk declares
		return src.replace("gl_Position = finalPosition;",
			"gl_Position = finalPosition;\n    {\n        vec4 mvPosition = modelViewMatrix * vec4( position, 1.0 );\n#include <clipping_planes_vertex>\n    }");
	}
	function clipFrag(src) {
		if (src.indexOf("clipping_planes_pars_fragment") >= 0) return src;
		src = "#include <clipping_planes_pars_fragment>\n" + src;
		return src.replace("void main() {", "void main() {\n#include <clipping_planes_fragment>");
	}
	// MeshLine widens lines in clip space by lineWidth/2 without dividing by w, which only looks
	// right with a perspective camera (w = distance). With the orthographic camera (plan / N E S W
	// views, Ortho) w = 1, so every Thick Line segment becomes a triangle half the screen wide.
	// Scale the offset by the ortho projection so the line is as wide as it is in perspective.
	var ORTHO_FIX = "\n    if (projectionMatrix[3][3] == 1.0 && sizeAttenuation != 0.) normal.xy *= projectionMatrix[1][1] * 0.41421356;\n";
	// MeshLine also tells a line's end vertex by exact equality (prevP == currentP), but it scales
	// the current point by the aspect ratio first, so rounding makes them differ by ~1e-7 and the
	// end gets a random direction: Thick Line intervals draw as wedges / arrowheads. Use a tolerance.
	function endVert(src) {
		src = src.replace("if (nextP == currentP)", "if (length(nextP - currentP) < 1e-5)")
			.replace("else if (prevP == currentP)", "else if (length(prevP - currentP) < 1e-5)");
		// a segment seen end-on (a vertical drillhole in plan view, a hole pointing at the camera)
		// has no screen direction: normalize(0) gives NaN and the vertex flies off as a huge spike.
		// Give it any direction - it is a dot on screen anyway.
		if (src.indexOf("q2t4 end-on") < 0) {
			src = src.replace(/(\n\s*)(vec4 normal\s*=)/, "$1if (!(length(dir) > 0.5)) dir = vec2(1.0, 0.0);   // q2t4 end-on$1$2");
		}
		return src;
	}
	function orthoVert(src) {
		src = endVert(src);
		if (src.indexOf("projectionMatrix[3][3] == 1.0") >= 0) return src;
		// the older MeshLine (Qgis2threejs 2.x) already scales the offset by the projection
		// ("normal *= projectionMatrix"), so it is right with an orthographic camera: leave it alone
		// (3.x keeps that line commented out: "//normal *= projectionMatrix;" - only a live line counts)
		if (/^[ \t'"]*normal\s*\*=\s*projectionMatrix/m.test(src)) return src;
		var k = src.indexOf("normal.xy *= .5 * w;");
		if (k < 0) return src;
		k += "normal.xy *= .5 * w;".length;
		return src.slice(0, k) + ORTHO_FIX + src.slice(k);
	}
	function isMeshLine(m) { return !!m && (m.isMeshLineMaterial || m.type === "MeshLineMaterial"); }
	function patchMaterial(m) {
		if (!isMeshLine(m) || m._q2t4Clip === 7) return;
		if (!m._q2t4Orig) m._q2t4Orig = m._q2t4Clip && THREE.ShaderChunk.meshline_vert ?
			[THREE.ShaderChunk.meshline_vert, THREE.ShaderChunk.meshline_frag] : [m.vertexShader, m.fragmentShader];
		m.vertexShader = orthoVert(clipVert(m._q2t4Orig[0]));
		m.fragmentShader = clipFrag(m._q2t4Orig[1]);
		m.clipping = true;
		m._q2t4Clip = 7;
		m.needsUpdate = true;
	}
	G.patchClipping = function () {
		if (!window.app || !app.scene) return 0;
		var n = 0;
		app.scene.traverse(function (o) {
			if (!o.material) return;
			var ms = Array.isArray(o.material) ? o.material : [o.material];
			for (var i = 0; i < ms.length; i++) {
				if (isMeshLine(ms[i]) && ms[i]._q2t4Clip !== 7) { patchMaterial(ms[i]); n++; }
			}
		});
		// future materials: Qgis2threejs 2.x (window.MeshLineMaterial) and 3.x (modules.meshline)
		if (window.MeshLineMaterial && window.MeshLineMaterial._q2t4Wrapped !== 7) {
			var Orig = window.MeshLineMaterial._orig || window.MeshLineMaterial;
			var Wrapped = function (params) { var m = new Orig(params); patchMaterial(m); return m; };
			Wrapped.prototype = Orig.prototype;
			Wrapped._q2t4Wrapped = 7;
			Wrapped._orig = Orig;
			window.MeshLineMaterial = Wrapped;
		}
		var mods = window.__qgis2threejs && window.__qgis2threejs.modules;
		if (mods && mods.meshline && mods.meshline.MeshLineMaterial && mods.meshline._q2t4Wrapped !== 7) {
			try {
				var ns = mods.meshline, Base = ns._q2t4Orig || ns.MeshLineMaterial;
				var W3 = class extends Base { constructor(p) { super(p); patchMaterial(this); } };
				var copy = Object.assign({}, ns);
				copy.MeshLineMaterial = W3;
				copy._q2t4Orig = Base;
				copy._q2t4Wrapped = 7;
				mods.meshline = copy;
			} catch (e) { /* leave Qgis2threejs' own class */ }
		}
		if (n) render();
		return n;
	};

	G.insideSection = function (p) {
		for (var i = 0; i < G.sectionPlanes.length; i++) {
			if (G.sectionPlanes[i].distanceToPoint(p) < -1e-6) return false;
		}
		return true;
	};

	// look along the section (A on the left, B on the right), fitting its length
	G.viewSection = function () {
		var g = G.sectionGeom();
		if (!g) return;
		var cam = app.camera, zrange = sectionZRange(g);
		var zc = (zrange[0] + zrange[1]) / 2, zr = zrange[1] - zrange[0];
		var target = new THREE.Vector3(g.C.x, g.C.y, zc);
		var w = g.len * 1.1, h = zr * 1.1, dist;
		if (cam.isPerspectiveCamera) {
			var vfov = cam.fov * Math.PI / 360;
			dist = Math.max(h / 2 / Math.tan(vfov), w / 2 / (Math.tan(vfov) * cam.aspect)) + g.half;
		} else {
			dist = Math.max(w, h) * 2;
			cam.zoom = Math.min((cam.right - cam.left) / w, (cam.top - cam.bottom) / h);
			cam.updateProjectionMatrix();
		}
		cam.position.copy(target).addScaledVector(g.n, -dist);
		if (cam.far < dist * 3) { cam.far = dist * 3; cam.updateProjectionMatrix(); }
		app.controls.target.copy(target);
		cam.lookAt(target);
		app.updateControlsAndRender();
	};

	// slide camera + target by d (map units) along the section view direction
	// Qgis2threejs puts the world origin at the centre of the QGIS map and keeps the camera's world
	// position when the map pans or zooms - so the 3D view jumped and zoomed with every QGIS pan/zoom.
	// Move the camera and orbit point with the origin so the view stays where it was on the ground.
	G.keepCameraOnMap = function (before) {
		if (!before || !app.camera || !app.controls || !app.controls.target) return;
		var u = app.scene.userData;
		if (!u || !u.origin) return;
		var zs = u.zScale || 1;
		var dx = before.x - u.origin.x, dy = before.y - u.origin.y;
		function z(w) { return ((w / before.zs + before.z) - u.origin.z) * zs; }
		if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9 && Math.abs(before.z - u.origin.z) < 1e-9 && before.zs === zs) return;
		var c = app.camera.position, t = app.controls.target;
		c.set(c.x + dx, c.y + dy, z(c.z));
		t.set(t.x + dx, t.y + dy, z(t.z));
		app.camera.lookAt(t);
		app.camera.updateMatrixWorld();
		if (G.updateGrid) G.updateGrid();
		app.updateControlsAndRender();
	};

	G.shiftView = function (d) {
		var g = G.sectionGeom();
		if (!g) return;
		var v = g.n.clone().multiplyScalar(d);
		app.camera.position.add(v);
		app.controls.target.add(v);
		app.updateControlsAndRender();
	};

	G.sectionInfo = function () {
		var g = G.sectionGeom();
		return JSON.stringify({section: G.section, planes: G.sectionPlanes.length,
			frame: !!G.sectionFrame, n: g && g.n, C: g && g.C});
	};

	G.has = function (id) { return !!G.meshes[id]; };
	G.ids = function () { return Object.keys(G.meshes); };

	// spec: {name, offset:[x,y,z], pos:b64(Float32 xyz), idx:b64(Uint32), edges:b64(Uint32),
	//        color, edgeColor, opacity, visible}
	G.addMesh = function (id, spec) {
		if (!G.attach()) return false;
		G.remove(id, true);

		var pos = new Float32Array(b64ToBuffer(spec.pos));
		var idx = new Uint32Array(b64ToBuffer(spec.idx));
		var edg = new Uint32Array(b64ToBuffer(spec.edges));

		var posAttr = new THREE.BufferAttribute(pos, 3);

		var geom = new THREE.BufferGeometry();
		geom.setAttribute("position", posAttr);
		geom.setIndex(new THREE.BufferAttribute(idx, 1));
		geom.computeVertexNormals();
		geom.computeBoundingSphere();

		var opacity = (spec.opacity === undefined) ? 1 : spec.opacity;
		var mtl = new THREE.MeshPhongMaterial({
			color: spec.color || "#d9a441",
			flatShading: true,
			side: THREE.DoubleSide,
			shininess: 15,
			transparent: opacity < 1,
			opacity: opacity,
			depthWrite: opacity >= 1,
			polygonOffset: true,        // push faces back so edges draw cleanly on top
			polygonOffsetFactor: 1,
			polygonOffsetUnits: 1
		});
		var mesh = new THREE.Mesh(geom, mtl);
		mesh.name = "faces";

		var egeom = new THREE.BufferGeometry();
		egeom.setAttribute("position", posAttr);
		egeom.setIndex(new THREE.BufferAttribute(edg, 1));
		egeom.computeBoundingSphere();
		var lines = new THREE.LineSegments(egeom, new THREE.LineBasicMaterial({color: spec.edgeColor || "#1a1a1a"}));
		lines.name = "edges";
		lines.visible = G.edgesVisible && spec.edges !== "";

		var obj = new THREE.Group();
		obj.name = spec.name || id;
		obj.userData = {q2t4Id: id, type: "wireframe"};
		obj.add(mesh);
		obj.add(lines);
		obj.visible = (spec.visible === undefined) ? true : spec.visible;

		// keep the spec without the big payloads
		var meta = {name: spec.name, offset: spec.offset, color: spec.color, edgeColor: spec.edgeColor, opacity: opacity};
		G.meshes[id] = {obj: obj, spec: meta};
		G.group.add(obj);
		G.updateTransforms();
		render();
		return true;
	};

	G.remove = function (id, noRender) {
		var m = G.meshes[id];
		if (!m) return;
		G.group.remove(m.obj);
		m.obj.traverse(function (o) {
			if (o.geometry) o.geometry.dispose();
			if (o.material) o.material.dispose();
		});
		delete G.meshes[id];
		if (!noRender) render();
	};

	G.clear = function () {
		for (var id in G.meshes) G.remove(id, true);
		render();
	};

	G.setEdgesVisible = function (visible) {
		G.edgesVisible = visible;
		for (var id in G.meshes) G.meshes[id].obj.getObjectByName("edges").visible = visible;
		render();
	};

	G.setVisible = function (id, visible) {
		if (G.meshes[id]) { G.meshes[id].obj.visible = visible; render(); }
	};

	G.setStyle = function (id, style) {
		var m = G.meshes[id];
		if (!m) return;
		var faces = m.obj.getObjectByName("faces"), edges = m.obj.getObjectByName("edges");
		if (style.color) faces.material.color.set(style.color);
		if (style.edgeColor) edges.material.color.set(style.edgeColor);
		if (style.opacity !== undefined) {
			faces.material.opacity = style.opacity;
			faces.material.transparent = style.opacity < 1;
			faces.material.depthWrite = style.opacity >= 1;
			faces.material.needsUpdate = true;
		}
		render();
	};

	// Zoom to one wireframe, block model or overlay (or everything of ours), keeping the view direction.
	G.zoomTo = function (id) {
		var obj = null;
		if (!id) obj = G.group;
		else if (G.meshes[id]) obj = G.meshes[id].obj.getObjectByName("faces");
		else if (G.blocks[id]) obj = G.blocks[id].obj;
		else if (G.overlays[id]) obj = G.overlays[id].obj;
		if (!obj) return false;
		G.group.updateMatrixWorld(true);
		return G.fitBox(new THREE.Box3().setFromObject(obj));
	};

	// zoom to a box round a map point (centre [x, y, z], half-size r in map units)
	G.zoomToBox = function (c, r) {
		var a = G.mapToWorld(c[0] - r, c[1] - r, c[2] - r), b = G.mapToWorld(c[0] + r, c[1] + r, c[2] + r);
		var box = new THREE.Box3(); box.expandByPoint(a); box.expandByPoint(b);
		return G.fitBox(box);
	};

	G.info = function () {
		var out = {ready: sceneReady(), attached: !!(G.group && G.group.parent), meshes: {}};
		for (var id in G.meshes) {
			var f = G.meshes[id].obj.getObjectByName("faces");
			out.meshes[id] = {name: G.meshes[id].spec.name, triangles: f.geometry.index.count / 3, visible: G.meshes[id].obj.visible};
		}
		return JSON.stringify(out);
	};



	// ------------------------------------------------------------- scale bar
	// Bottom-centre box: scale bar at the orbit point (or section plane), CRS short name and the
	// section's coordinate. Redrawn with the grid whenever the camera moves.
	function niceLen(m) {
		var p = Math.pow(10, Math.floor(Math.log(m) / Math.LN10)), r = m / p;
		return (r >= 5 ? 5 : r >= 2 ? 2 : 1) * p;
	}
	G.updateScale = function () {
		if (!window.app || !app.renderer || !sceneReady()) return;
		var el = document.getElementById("q2t4-scale");
		if (!el) {
			el = document.createElement("div");
			el.id = "q2t4-scale";
			el.style.cssText = "position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:21;pointer-events:none;" +
				"background:rgba(22,33,43,.88);color:#E6EBEF;border-radius:6px;padding:5px 10px 4px;font:11px Consolas,'JetBrains Mono',monospace;text-align:center;white-space:nowrap";
			(document.getElementById("view") || document.body).appendChild(el);
		}
		if (G.scaleOn === false) { el.style.display = "none"; return; }
		var cam = app.camera, t = app.controls.target, H = app.height;
		var mpp = cam.isOrthographicCamera ? (cam.top - cam.bottom) / cam.zoom / H
										   : 2 * cam.position.distanceTo(t) * Math.tan(cam.fov * Math.PI / 360) / H;
		if (!(mpp > 0) || !isFinite(mpp)) { el.style.display = "none"; return; }
		var len = niceLen(mpp * 120), px = len / mpp, segs = 4, sw = px / segs;
		var bar = "<svg width='" + (px + 2).toFixed(0) + "' height='9' style='display:block;margin:3px auto 1px'>";
		for (var i = 0; i < segs; i++)
			bar += "<rect x='" + (1 + i * sw).toFixed(1) + "' y='1' width='" + sw.toFixed(1) + "' height='7' fill='" + (i % 2 ? "#E6EBEF" : "#16212B") + "' stroke='#E6EBEF' stroke-width='1'/>";
		bar += "</svg>";
		var u = unit(), lab = (u === "m" && len >= 1000) ? (len / 1000) + " km" : (len < 1 ? len.toFixed(2) : len) + " " + u;
		var sc = G.hud.scale || {};
		var html = "<div style='display:flex;justify-content:space-between;width:" + (px + 2).toFixed(0) + "px;margin:0 auto'><span>0</span><span>" + lab + "</span></div>" + bar;
		var zs = app.scene.userData.zScale;
		var line2 = (sc.crs || "") + (zs && Math.abs(zs - 1) > 1e-6 ? "  ·  V.E. ×" + (+zs.toFixed(2)) : "");
		if (line2) html += "<div style='color:#9FB0BC' title='" + (sc.crsFull || "").replace(/'/g, "") + "'>" + line2 + "</div>";
		// view-plane coordinate: northing facing N/S, easting facing E/W, RL in plan; the section's
		// description when looking along an oblique section
		if (G.viewCoordOn) {
			var f = null;
			try { f = gridFrame(); } catch (e) { f = null; }
			var vc = f ? (f.coord || (f.kind === "section" ? sc.section : null)) : null;
			if (vc) html += "<div style='color:#F0472E'>" + String(vc).replace(/</g, "&lt;") + "</div>";
		}
		el.innerHTML = html;
		el.style.display = "";
	};

	// ------------------------------------------------------------------- grid
	// Coordinate grid (screen overlay) shown while the view is aligned: plan (E/N), facing
	// N/S (E/RL), facing E/W (N/RL), or looking along the section (chainage/RL). Lines lie in the
	// plane through the orbit point (or the section plane); labels give map coordinates.
	G.gridOn = true;
	G.viewCoordOn = true;
	G.setViewCoord = function (on) { G.viewCoordOn = !!on; G.updateScale(); };
	G.scaleOn = true;          // the whole scale bar box (bar, CRS name, view coordinate)
	G.setScaleBar = function (on) { G.scaleOn = !!on; G.updateScale(); };
	var GRID_TOL = Math.cos(1.0 * Math.PI / 180);     // within 1 degree counts as aligned

	G.setGrid = function (on) { G.gridOn = !!on; G.updateGrid(); };

	function niceStep(range, n) {
		var raw = range / Math.max(n, 1), p = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), m = raw / p;
		return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
	}

	function gridRoot() {
		var el = document.getElementById("q2t4-grid");
		if (el) return el;
		el = document.createElement("div");
		el.id = "q2t4-grid";
		el.style.cssText = "position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:10;";
		(document.getElementById("view") || document.body).appendChild(el);
		return el;
	}

	// which aligned view, and how plane coordinates (a, b) map to world
	// thousands separators by hand: QtWebKit ignores toLocaleString options
	function thou(v, dec) {
		var t = Math.abs(v).toFixed(dec).split("."), i = t[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
		return (v < 0 ? "-" : "") + i + (t.length > 1 ? "." + t[1] : "");
	}
	function fmtc(v) { return thou(v, 1); }
	// map unit of the project CRS ("m" or "ft"; Python sends it with the scale bar spec)
	function unit() { return (G.hud && G.hud.scale && G.hud.scale.unit) || "m"; }
	function ax(letter) { return unit() + letter; }          // mE, mN, mRL / ftE, ftN, ftRL

	function gridFrame() {
		var cam = app.camera, t = app.controls && app.controls.target;
		if (!t || !sceneReady() || !G.group) return null;
		var d = new THREE.Vector3().subVectors(t, cam.position).normalize();
		var tm = G.worldToMap(t);
		var rot = (app.scene.userData.baseExtent.rotation || 0) * Math.PI / 180;
		// view direction in map axes
		var mx = d.x * Math.cos(-rot) - d.y * Math.sin(-rot), my = d.x * Math.sin(-rot) + d.y * Math.cos(-rot);
		if (-d.z >= GRID_TOL) {
			return {kind: "plan", al: "E", bl: "N", n: d, coord: fmtc(tm.z) + " " + ax("RL"),
					w: function (a, b) { return G.mapToWorld(a, b, tm.z); },
					ab: function (m) { return [m.x, m.y]; }};
		}
		var h = Math.sqrt(mx * mx + my * my);
		if (Math.abs(d.z) > Math.sin(1.0 * Math.PI / 180) || h < 1e-9) return null;
		mx /= h; my /= h;
		var g = G.sectionGeom && G.section ? G.sectionGeom() : null;
		if (g) {                                       // looking along the section (either way)
			var sx = (G.section.b[0] - G.section.a[0]) / g.len, sy = (G.section.b[1] - G.section.a[1]) / g.len;
			var nx = -sy, ny = sx;
			if (Math.abs(mx * nx + my * ny) >= GRID_TOL && !(Math.abs(sx) > 0.9998 || Math.abs(sy) > 0.9998)) {
				var A = G.section.a;
				return {kind: "section", al: "ch", bl: "RL", n: d, coord: null,
						w: function (a, b) { return G.mapToWorld(A[0] + a * sx, A[1] + a * sy, b); },
						ab: function (m) { return [(m.x - A[0]) * sx + (m.y - A[1]) * sy, m.z]; }};
			}
		}
		if (Math.abs(my) >= GRID_TOL) {                // facing N or S
			var yN = g && Math.abs(G.section.a[1] - G.section.b[1]) < 1e-6 ? G.section.a[1] : tm.y;   // E-W section line
			return {kind: my > 0 ? "N" : "S", al: "E", bl: "RL", n: d, coord: fmtc(yN) + " " + ax("N"),
					w: function (a, b) { return G.mapToWorld(a, tm.y, b); },
					ab: function (m) { return [m.x, m.z]; }};
		}
		if (Math.abs(mx) >= GRID_TOL) {                // facing E or W
			var xE = g && Math.abs(G.section.a[0] - G.section.b[0]) < 1e-6 ? G.section.a[0] : tm.x;   // N-S section line
			return {kind: mx > 0 ? "E" : "W", al: "N", bl: "RL", n: d, coord: fmtc(xE) + " " + ax("E"),
					w: function (a, b) { return G.mapToWorld(tm.x, a, b); },
					ab: function (m) { return [m.y, m.z]; }};
		}
		return null;
	}

	function fmt(v, step) {
		var dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log(step) / Math.LN10));
		return thou(v, dec);
	}

	G.updateGrid = function () {
		if (!window.app || !app.renderer) return;
		try { G.updateScale(); } catch (e) { /* never block the grid */ }
		var el = gridRoot();
		var f = G.gridOn ? gridFrame() : null;
		if (!f) { if (el.innerHTML) el.innerHTML = ""; G._gridKind = null; return; }
		var cam = app.camera, W = app.width, H = app.height, t = app.controls.target;
		// the grid plane: through the orbit point (or the section line), facing the camera
		var p0 = f.w.apply(null, f.ab(G.worldToMap(t)));
		var plane = new THREE.Plane().setFromNormalAndCoplanarPoint(f.n.clone().negate(), p0);
		var amin = Infinity, amax = -Infinity, bmin = Infinity, bmax = -Infinity, hit = new THREE.Vector3();
		var rc = new THREE.Raycaster();
		var corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]];
		for (var i = 0; i < 4; i++) {
			rc.setFromCamera(new THREE.Vector2(corners[i][0], corners[i][1]), cam);
			if (!rc.ray.intersectPlane(plane, hit)) continue;
			var ab = f.ab(G.worldToMap(hit));
			amin = Math.min(amin, ab[0]); amax = Math.max(amax, ab[0]); bmin = Math.min(bmin, ab[1]); bmax = Math.max(bmax, ab[1]);
		}
		if (!isFinite(amin) || !isFinite(bmin) || amax - amin <= 0 || bmax - bmin <= 0) { el.innerHTML = ""; return; }
		var sa = niceStep(amax - amin, W / 110), sb = niceStep(bmax - bmin, H / 80);
		function px(v) { var q = v.clone().project(cam); return [(q.x + 1) * W / 2, (1 - q.y) * H / 2]; }
		var svg = "<svg width='" + W + "' height='" + H + "' style='position:absolute;left:0;top:0'>";
		var line = "stroke='#1f2d38' stroke-opacity='0.28' stroke-width='1'";
		var lab = "font-family='Consolas,monospace' font-size='11' fill='#1f2d38' stroke='#ffffff' stroke-width='3' paint-order='stroke' stroke-opacity='0.85'";
		var LAB_EVERY = 3;
		var lbl = function (axis, v, step) {
			return axis === "ch" ? "ch " + fmt(v, step) + " " + unit() : fmt(v, step) + " " + ax(axis);
		};
		var count = 0;
		for (var a = Math.ceil(amin / sa) * sa; a <= amax && count < 200; a += sa, count++) {
			var p1 = px(f.w(a, bmin)), p2 = px(f.w(a, bmax));
			svg += "<line x1='" + p1[0].toFixed(1) + "' y1='" + p1[1].toFixed(1) + "' x2='" + p2[0].toFixed(1) + "' y2='" + p2[1].toFixed(1) + "' " + line + "/>";
			var lx = (p1[0] + p2[0]) / 2;
			if (lx > 40 && lx < W - 40 && Math.round(a / sa) % LAB_EVERY === 0)
				svg += "<text x='" + lx.toFixed(1) + "' y='" + (H - 6) + "' text-anchor='middle' " + lab + ">" + lbl(f.al, a, sa) + "</text>";
		}
		count = 0;
		for (var b = Math.ceil(bmin / sb) * sb; b <= bmax && count < 200; b += sb, count++) {
			var q1 = px(f.w(amin, b)), q2 = px(f.w(amax, b));
			svg += "<line x1='" + q1[0].toFixed(1) + "' y1='" + q1[1].toFixed(1) + "' x2='" + q2[0].toFixed(1) + "' y2='" + q2[1].toFixed(1) + "' " + line + "/>";
			var ly = (q1[1] + q2[1]) / 2;
			if (ly > 14 && ly < H - 20 && Math.round(b / sb) % LAB_EVERY === 0)
				svg += "<text x='" + (W - 6) + "' y='" + (ly - 3).toFixed(1) + "' text-anchor='end' " + lab + ">" + lbl(f.bl, b, sb) + "</text>";
		}
		svg += "</svg>";
		el.innerHTML = svg;
		G._gridKind = f.kind;
	};

	// redraw after camera moves (once per page; dispatches to the current module)
	G._hookGrid = function () {
		if (!app.controls || app._q2t4GridHook) return;
		app._q2t4GridHook = true;
		var pending = false;
		var go = function () {
			if (pending) return;
			pending = true;
			window.setTimeout(function () { pending = false; var M = window.Q2T4; if (M && M.updateGrid) M.updateGrid(); }, 30);
		};
		app.controls.addEventListener("change", go);
		window.addEventListener("resize", go);
	};

	// ================================================================ 0.9.3 tools
	// ------------------------------------------------------------- labels
	// G.setLabels(name, {items: [{p: [x, y, z] (map), text, color}], size, onSection}) - HTML text
	// placed over the 3D view, re-placed when the camera moves. Overlapping labels are skipped.
	G.labelSets = G.labelSets || {};
	G.setLabels = function (name, spec) {
		if (!G.attach()) return false;
		if (!spec || !spec.items || !spec.items.length) { delete G.labelSets[name]; G.updateLabels(); return true; }
		var items = [];
		for (var i = 0; i < spec.items.length; i++) {
			var it = spec.items[i];
			items.push({w: G.mapToWorld(it.p[0], it.p[1], it.p[2]), text: String(it.text), color: it.color || "#ffffff"});
		}
		G.labelSets[name] = {items: items, size: spec.size || 11, visible: spec.visible !== false, map: spec.items};
		G.updateLabels();
		return true;
	};
	G.setLabelsVisible = function (name, on) { var s = G.labelSets[name]; if (s) { s.visible = !!on; G.updateLabels(); } };
	G.updateLabels = function () {
		if (!window.app || !app.renderer) return;
		var el = document.getElementById("q2t4-labels");
		if (!el) {
			el = document.createElement("div");
			el.id = "q2t4-labels";
			el.style.cssText = "position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;overflow:hidden;z-index:5;" +
				"font-family:Consolas,monospace;";
			(app.container || document.body).appendChild(el);
		}
		var html = [], taken = [], cam = app.camera, W = app.width, H = app.height, v = new THREE.Vector3();
		cam.updateMatrixWorld();
		for (var name in G.labelSets) {
			var s = G.labelSets[name];
			if (!s.visible) continue;
			for (var i = 0; i < s.items.length && html.length < 3000; i++) {
				var it = s.items[i];
				if (G.sectionPlanes && G.sectionPlanes.length && !G.insideSection(it.w)) continue;
				v.copy(it.w).project(cam);
				if (v.z > 1 || v.x < -1.1 || v.x > 1.1 || v.y < -1.1 || v.y > 1.1) continue;
				var x = (v.x + 1) / 2 * W, y = (1 - v.y) / 2 * H, w = it.text.length * s.size * 0.62, h = s.size + 3;
				var clash = false;
				for (var k = 0; k < taken.length && !clash; k++) {
					var t = taken[k];
					clash = x < t[0] + t[2] && x + w > t[0] && y - h < t[1] && y > t[1] - t[3];
				}
				if (clash) continue;
				taken.push([x, y, w, h]);
				html.push("<div style='position:absolute;left:" + (x + 4).toFixed(0) + "px;top:" + (y - h).toFixed(0) +
					"px;color:" + it.color + ";font-size:" + s.size + "px;white-space:nowrap;text-shadow:0 0 3px #000,0 0 2px #000'>" +
					it.text.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</div>");
			}
		}
		el.innerHTML = html.join("");
	};

	// ------------------------------------------------------------- tubes (drillhole intervals by grade)
	// G.setTubes(name, {offset: [x,y,z] map, chunks: [{pos: b64 f32, nrm: b64 f32, col: b64 u8 rgb, idx: b64 u16}]})
	G.tubes = G.tubes || {};
	G.setTubes = function (name, spec) {
		if (!G.attach()) return false;
		G.removeTubes(name);
		if (!spec) { render(); return true; }
		var grp = new THREE.Group();
		grp.name = "q2t4 tubes " + name;
		var mat = new THREE.MeshPhongMaterial({vertexColors: VCOLORS, shininess: 20, side: THREE.DoubleSide});
		for (var c = 0; c < spec.chunks.length; c++) {
			var ch = spec.chunks[c], g = new THREE.BufferGeometry();
			g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(b64ToBuffer(ch.pos)), 3));
			g.setAttribute("normal", new THREE.BufferAttribute(new Float32Array(b64ToBuffer(ch.nrm)), 3));
			g.setAttribute("color", new THREE.BufferAttribute(new Uint8Array(b64ToBuffer(ch.col)), 3, true));
			g.setIndex(new THREE.BufferAttribute(new Uint16Array(b64ToBuffer(ch.idx)), 1));
			g.computeBoundingSphere();
			grp.add(new THREE.Mesh(g, mat));
		}
		grp.visible = spec.visible !== false;
		G.tubes[name] = {obj: grp, offset: spec.offset};
		G.group.add(grp);
		G._placeTubes();
		render();
		return true;
	};
	G._placeTubes = function () {
		if (!app.scene || !app.scene.userData || !app.scene.userData.baseExtent) return;
		var u = app.scene.userData, be = u.baseExtent;
		for (var n in G.tubes) {
			var t = G.tubes[n], o = t.offset;
			t.obj.position.set(o[0] - be.cx, o[1] - be.cy, (o[2] - u.origin.z) * u.zScale);
			t.obj.scale.set(1, 1, u.zScale);
		}
	};
	G.removeTubes = function (name) {
		var t = G.tubes[name];
		if (!t) return;
		if (t.obj.parent) t.obj.parent.remove(t.obj);
		t.obj.traverse(function (o) { if (o.geometry) o.geometry.dispose(); });
		delete G.tubes[name];
		render();
	};
	G.setTubesVisible = function (name, on) { var t = G.tubes[name]; if (t) { t.obj.visible = !!on; render(); } };

	// ------------------------------------------------------------- measure
	G.meas = G.meas || {active: false, pts: [], obj: null};
	function measEnsure() {
		if (G.meas.obj && G.meas.obj.parent) return;
		var grp = new THREE.Group();
		grp.name = "q2t4 measure";
		var l = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#00e5ff", depthTest: false}));
		l.name = "line"; l.renderOrder = 999; grp.add(l);
		var p = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#00e5ff", size: 9, sizeAttenuation: false, depthTest: false}));
		p.name = "pts"; p.renderOrder = 999; grp.add(p);
		app.scene.add(grp);
		G.meas.obj = grp;
	}
	function measBox(html) {
		var el = document.getElementById("q2t4-measure");
		if (!el) {
			el = document.createElement("div");
			el.id = "q2t4-measure";
			el.style.cssText = "position:absolute;right:10px;top:46px;z-index:20;background:rgba(20,24,28,.88);color:#e8eef2;" +
				"font:12px Consolas,monospace;padding:8px 10px;border-radius:4px;border:1px solid #00e5ff;pointer-events:none;";
			(app.container || document.body).appendChild(el);
		}
		el.style.display = html ? "block" : "none";
		el.innerHTML = html || "";
	}
	G.measureResult = function (pts) {
		// pts in map coordinates
		var r = {n: pts.length};
		if (pts.length >= 2) {
			var a = pts[0], b = pts[1], dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
			var hz = Math.sqrt(dx * dx + dy * dy);
			r.length = Math.sqrt(hz * hz + dz * dz); r.horizontal = hz; r.dz = dz;
			r.bearing = (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360;
			r.plunge = Math.atan2(-dz, hz) * 180 / Math.PI;    // + = down from A to B
			if (pts.length >= 3) {
				var c = pts[2];
				var u = [b.x - a.x, b.y - a.y, b.z - a.z], w = [c.x - a.x, c.y - a.y, c.z - a.z];
				var nx = u[1] * w[2] - u[2] * w[1], ny = u[2] * w[0] - u[0] * w[2], nz = u[0] * w[1] - u[1] * w[0];
				var nn = Math.sqrt(nx * nx + ny * ny + nz * nz);
				if (nn > 1e-9) {
					if (nz < 0) { nx = -nx; ny = -ny; nz = -nz; }
					r.dip = Math.acos(nz / nn) * 180 / Math.PI;
					r.dipDirection = (Math.atan2(nx, ny) * 180 / Math.PI + 360) % 360;
				}
			}
		}
		return r;
	};
	G.measRedraw = function () {
		measEnsure();
		var o = G.meas.obj, pts = G.meas.pts;
		var line = o.getObjectByName("line"), pp = o.getObjectByName("pts");
		var all = pts.slice();
		if (G.meas.hover && pts.length && pts.length < 3) all.push(G.meas.hover);
		line.geometry.dispose(); line.geometry = new THREE.BufferGeometry().setFromPoints(all.length >= 2 ? all : []);
		pp.geometry.dispose(); pp.geometry = new THREE.BufferGeometry().setFromPoints(all);
		o.visible = G.meas.active;
		var mp = all.map(function (p) { return G.worldToMap(p); });
		var r = G.measureResult(mp), h = "";
		if (r.n >= 2) {
			h = "<b>A → B</b><br>length " + r.length.toFixed(2) + " m<br>horizontal " + r.horizontal.toFixed(2) +
				" m<br>ΔRL " + (r.dz >= 0 ? "+" : "") + r.dz.toFixed(2) + " m<br>bearing " + r.bearing.toFixed(1) +
				"°<br>plunge " + r.plunge.toFixed(1) + "°" + (r.plunge >= 0 ? " down" : " up");
			if (r.dip !== undefined) h += "<br><b>plane A B C</b><br>dip " + r.dip.toFixed(1) + "° → " + r.dipDirection.toFixed(1) + "°" +
				"<br>(" + r.dip.toFixed(0) + "/" + ("00" + r.dipDirection.toFixed(0)).slice(-3) + ")";
		} else h = "<b>Measure</b><br>click A (snaps like digitising)";
		measBox(G.meas.active ? h : "");
		G.meas.last = r;
		render();
	};
	G.startMeasure = function () {
		if (G.dig.active) G.stopDigitise();
		if (G.queryMode) G.setQueryMode(false);
		if (G.edit && G.edit.active) G.stopEdit();
		G.meas.active = true; G.meas.pts = []; G.meas.hover = null;
		app.renderer.domElement.style.cursor = "crosshair";
		G.dig.events.push({type: "measureMode", active: true});
		G.measRedraw();
		status("Measure: click A, B (distance, bearing, plunge), C (plane dip) · 4th click starts again · Esc ends");
	};
	G.stopMeasure = function () {
		if (!G.meas.active) return;
		G.meas.active = false; G.meas.pts = []; G.meas.hover = null;
		app.renderer.domElement.style.cursor = "";
		G.dig.events.push({type: "measureMode", active: false});
		G.measRedraw();
		status("", 1);
	};
	G.measClick = function (cx, cy) {
		var s = G.snapAt(cx, cy) || (function () { var p = G.pick(cx, cy); return p ? {p: p} : null; })();
		if (!s) { status("Nothing to measure to here (click a surface, a snap point or turn on a section)", 3000); return; }
		if (G.meas.pts.length >= 3) G.meas.pts = [];
		G.meas.pts.push(s.p.clone());
		G.measRedraw();
		if (G.meas.pts.length >= 2) {
			var mp = G.meas.pts.map(function (p) { var m = G.worldToMap(p); return [m.x, m.y, m.z]; });
			G.dig.events.push({type: "measure", pts: mp, result: G.meas.last});
		}
	};

	// ------------------------------------------------------------- vertex editing
	// G.startEdit({items: [{id, pts: [[x,y,z], ...] (map), closed}]}) - drag a vertex (it snaps like
	// digitising, else stays on the plane through it facing the camera / the section plane);
	// Delete removes the vertex under the cursor; events {type: 'vertexMoved' | 'vertexDeleted', id, i, p}.
	G.edit = G.edit || {active: false};
	function editEnsure() {
		if (G.edit.obj && G.edit.obj.parent) return;
		var grp = new THREE.Group();
		grp.name = "q2t4 edit";
		var p = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#ff3b3b", size: 8, sizeAttenuation: false, depthTest: false}));
		p.name = "verts"; p.renderOrder = 999; grp.add(p);
		var h = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#ffffff", size: 14, sizeAttenuation: false, depthTest: false}));
		h.name = "hot"; h.renderOrder = 1000; grp.add(h);
		var l = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#ff3b3b", depthTest: false, transparent: true, opacity: 0.7}));
		l.name = "drag"; l.renderOrder = 998; grp.add(l);
		var sl = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#00e5ff", depthTest: false}));
		sl.name = "sel"; sl.renderOrder = 997; grp.add(sl);
		var sv = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#00e5ff", size: 7, sizeAttenuation: false, depthTest: false}));
		sv.name = "selv"; sv.renderOrder = 997; grp.add(sv);
		var sp2 = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#ff2a2a", size: 13, sizeAttenuation: false, depthTest: false}));
		sp2.name = "selp"; sp2.renderOrder = 1001; grp.add(sp2);
		app.scene.add(grp);
		G.edit.obj = grp;
	}
	G.editRedraw = function () {
		editEnsure();
		var e = G.edit, o = e.obj, all = [], seg = [];
		for (var i = 0; i < (e.items || []).length; i++) for (var k = 0; k < e.items[i].w.length; k++) all.push(e.items[i].w[k]);
		var v = o.getObjectByName("verts"); v.geometry.dispose(); v.geometry = new THREE.BufferGeometry().setFromPoints(all);
		var h = o.getObjectByName("hot"); h.geometry.dispose(); h.geometry = new THREE.BufferGeometry().setFromPoints(e.hot ? [e.items[e.hot[0]].w[e.hot[1]]] : []);
		if (e.drag) {
			var it = e.items[e.drag[0]], j = e.drag[1], n = it.w.length;
			if (j > 0) seg.push(it.w[j - 1], it.w[j]);
			if (j < n - 1) seg.push(it.w[j], it.w[j + 1]);
			if (it.closed && n > 2 && (j === 0 || j === n - 1)) seg.push(it.w[j === 0 ? n - 1 : 0], it.w[j]);
		}
		if (e.hotSeg && e.mode !== "move") {
			var hs = e.items[e.hotSeg[0]].w;
			if (e.mode === "copy" || e.mode === "select") { for (var q = 0; q < hs.length - 1; q++) seg.push(hs[q], hs[q + 1]); }
			else seg.push(hs[e.hotSeg[1]], hs[e.hotSeg[1] + 1]);
		}
		var d = o.getObjectByName("drag"); d.geometry.dispose(); d.geometry = new THREE.BufferGeometry().setFromPoints(seg);
		h.geometry.dispose();
		h.geometry = new THREE.BufferGeometry().setFromPoints(e.hot && e.mode !== "select" ? [e.items[e.hot[0]].w[e.hot[1]]]
			: (e.hotSeg && e.mode === "insert" ? [e.hotSeg[2]]
			: (e.mode === "select" && e.hotPt ? [e.items[e.hotPt[0]].w[e.hotPt[1]]] : [])));
		var ss = [], sp = [], sel = e.sel || {};
		for (var si = 0; si < (e.items || []).length; si++) {
			if (!sel[e.items[si].id]) continue;
			var sw = e.items[si].w;
			for (var sk = 0; sk < sw.length; sk++) { sp.push(sw[sk]); if (sk < sw.length - 1) ss.push(sw[sk], sw[sk + 1]); }
		}
		var so = o.getObjectByName("sel");
		if (so) { so.geometry.dispose(); so.geometry = new THREE.BufferGeometry().setFromPoints(ss); }
		var svo = o.getObjectByName("selv");
		if (svo) { svo.geometry.dispose(); svo.geometry = new THREE.BufferGeometry().setFromPoints(sp); }
		var spts = [], sps = e.selPts || {};
		for (var pi = 0; pi < (e.items || []).length; pi++) {
			var pm = sps[e.items[pi].id];
			if (!pm) continue;
			for (var pk in pm) if (pm[pk] && e.items[pi].w[+pk]) spts.push(e.items[pi].w[+pk]);
		}
		var spo = o.getObjectByName("selp");
		if (spo) { spo.geometry.dispose(); spo.geometry = new THREE.BufferGeometry().setFromPoints(spts); }
		v.visible = e.mode !== "select";
		o.visible = e.active;
		render();
	};
	function editNearest(cx, cy) {
		var e = G.edit, off = offsetOf(app.renderer.domElement), sx = cx - off.left, sy = cy - off.top;
		var best = null, bd = 10 * 10, q = new THREE.Vector3();
		for (var i = 0; i < e.items.length; i++) for (var k = 0; k < e.items[i].w.length; k++) {
			var p = e.items[i].w[k];
			if (G.sectionPlanes.length && !G.insideSection(p)) continue;
			q.copy(p).project(app.camera);
			if (q.z > 1) continue;
			var px = (q.x + 1) / 2 * app.width, py = (1 - q.y) / 2 * app.height, d = (px - sx) * (px - sx) + (py - sy) * (py - sy);
			if (d < bd) { bd = d; best = [i, k]; }
		}
		return best;
	}
	function editDragPos(cx, cy) {
		var e = G.edit, cur = e.items[e.drag[0]].w[e.drag[1]];
		var s = G.snapAt(cx, cy);
		if (s && s.kind !== "section") return s.p;
		var g = G.sectionGeom(), plane;
		if (g) plane = new THREE.Plane().setFromNormalAndCoplanarPoint(g.n, g.C);
		else {
			var dir = new THREE.Vector3(); app.camera.getWorldDirection(dir);
			plane = new THREE.Plane().setFromNormalAndCoplanarPoint(dir, cur);
		}
		var off = offsetOf(app.renderer.domElement);
		var v = new THREE.Vector2(((cx - off.left) / app.width) * 2 - 1, -((cy - off.top) / app.height) * 2 + 1);
		var ray = new THREE.Raycaster(); ray.setFromCamera(v, app.camera);
		return ray.ray.intersectPlane(plane, new THREE.Vector3()) || cur;
	}
	// nearest segment within 10 px: [item, seg, world point on it]
	function editNearestSeg(cx, cy) {
		var e = G.edit, off = offsetOf(app.renderer.domElement), sx = cx - off.left, sy = cy - off.top;
		var best = null, bd = 10 * 10, qa = new THREE.Vector3(), qb = new THREE.Vector3();
		for (var i = 0; i < e.items.length; i++) {
			if (String(e.items[i].id).indexOf("s:") !== 0) continue;          // strings only
			var w = e.items[i].w;
			for (var k = 0; k < w.length - 1; k++) {
				if (G.sectionPlanes.length && !G.insideSection(w[k]) && !G.insideSection(w[k + 1])) continue;
				qa.copy(w[k]).project(app.camera); qb.copy(w[k + 1]).project(app.camera);
				if (qa.z > 1 || qb.z > 1) continue;
				var ax = (qa.x + 1) / 2 * app.width, ay = (1 - qa.y) / 2 * app.height;
				var bx = (qb.x + 1) / 2 * app.width, by = (1 - qb.y) / 2 * app.height;
				var dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
				var t = L > 0 ? Math.max(0, Math.min(1, ((sx - ax) * dx + (sy - ay) * dy) / L)) : 0;
				var px = ax + t * dx - sx, py = ay + t * dy - sy, d = px * px + py * py;
				if (d < bd) { bd = d; best = [i, k, w[k].clone().lerp(w[k + 1], t)]; }
			}
		}
		return best;
	}
	G.setEditMode = function (m) {
		G.edit.mode = m || "move";
		G.edit.hot = null; G.edit.hotSeg = null;
		var tips = {move: "Edit: drag a red vertex · Delete removes the vertex under the cursor",
			insert: "Insert point: click on a string segment (snaps to strings, drillholes, surfaces)",
			cut: "Cut: click a vertex or segment - an open string splits in two, a closed outline opens (that segment is removed)",
			copy: "Copy: click a string to duplicate it",
			link: "Link: click strings one after another - each is linked to the one before · Backspace undoes the last · Enter / Esc finishes · right-drag rotates",
			cap: "Close end: click a closed outline to cap the wireframe there (tie lines across it guide the triangles; click again to redo) · Backspace undoes · Esc ends · right-drag rotates",
			select: "Select: click a segment = string, click a vertex = point · Ctrl+click adds / removes · drag a box on empty space · F2 renames · Delete deletes the selected points (else strings)"};
		status((tips[G.edit.mode] || "") + " · S snapping · Esc ends");
		if (G.edit.active) G.editRedraw();
		return G.edit.mode;
	};
	function selPointList() {
		var out = [], sp = G.edit.selPts || {};
		for (var id in sp) {
			var idx = [];
			for (var k in sp[id]) if (sp[id][k]) idx.push(+k);
			if (idx.length) out.push({id: id, idx: idx});
		}
		return out;
	}
	function selChanged() {
		var ids = [];
		for (var k in G.edit.sel) if (G.edit.sel[k]) ids.push(k);
		G.dig.events.push({type: "stringsSelected", ids: ids, points: selPointList()});
		G.editRedraw();
	}
	G.setSelectedStrings = function (ids) {           // from the panel's list
		G.edit.sel = {}; G.edit.selPts = {};
		(ids || []).forEach(function (id) { G.edit.sel[id] = true; });
		if (G.edit.active) G.editRedraw();
	};
	function boxDiv() {
		var el = document.getElementById("q2t4-selbox");
		if (!el) {
			el = document.createElement("div");
			el.id = "q2t4-selbox";
			el.style.cssText = "position:fixed;border:1px dashed #ffd400;background:rgba(255,212,0,0.08);pointer-events:none;z-index:20;display:none";
			document.body.appendChild(el);
		}
		return el;
	}
	function boxSelect(b, add) {
		var e = G.edit, x0 = Math.min(b.x0, b.x1), x1 = Math.max(b.x0, b.x1), y0 = Math.min(b.y0, b.y1), y1 = Math.max(b.y0, b.y1);
		var off = offsetOf(app.renderer.domElement), q = new THREE.Vector3();
		if (!add) { e.sel = {}; e.selPts = {}; }
		for (var i = 0; i < e.items.length; i++) {
			var it = e.items[i];
			if (String(it.id).indexOf("s:") !== 0) continue;
			for (var k = 0; k < it.w.length; k++) {
				if (G.sectionPlanes.length && !G.insideSection(it.w[k])) continue;
				q.copy(it.w[k]).project(app.camera);
				if (q.z > 1) continue;
				var px = off.left + (q.x + 1) / 2 * app.width, py = off.top + (1 - q.y) / 2 * app.height;
				if (px >= x0 && px <= x1 && py >= y0 && py <= y1) { e.sel[it.id] = true; break; }
			}
		}
		selChanged();
	}

	G.setEditItems = function (spec) {
		G.edit.items = (spec.items || []).map(function (it) {
			return {id: it.id, closed: !!it.closed, w: it.pts.map(function (p) { return G.mapToWorld(p[0], p[1], p[2]); })};
		});
		G.edit.hot = null; G.edit.hotSeg = null; G.edit.drag = null;
		if (G.edit.active) G.editRedraw();
	};

	G.startEdit = function (spec) {
		if (!G.attach()) return false;
		if (G.tdel && G.tdel.active) G.stopTriDelete();
		if (G.dig.active) G.stopDigitise();
		if (G.meas.active) G.stopMeasure();
		if (G.queryMode) G.setQueryMode(false);
		var e = G.edit;
		e.items = (spec.items || []).map(function (it) {
			return {id: it.id, closed: !!it.closed, w: it.pts.map(function (p) { return G.mapToWorld(p[0], p[1], p[2]); })};
		});
		e.active = true; e.hot = null; e.drag = null; e.hotSeg = null;
		e.mode = spec.mode || e.mode || "move";
		if (!e.hooked && !app._q2t4EditHook) {
			e.hooked = app._q2t4EditHook = true;
			var el = app.renderer.domElement;
			el.addEventListener("mousedown", function (ev) { var M = window.Q2T4; if (M && M._editDown) M._editDown(ev); }, true);
			window.addEventListener("mousemove", function (ev) { var M = window.Q2T4; if (M && M._editMove) M._editMove(ev); }, true);
			window.addEventListener("mouseup", function (ev) { var M = window.Q2T4; if (M && M._editUp) M._editUp(ev); }, true);
			window.addEventListener("keydown", function (ev) { var M = window.Q2T4; if (M && M._editKey) M._editKey(ev); }, true);
		}
		G.editRedraw();
		G.syncToolControls();
		G.dig.events.push({type: "editMode", active: true});
		status("Edit: drag a red vertex (snaps to drillholes, strings, surfaces) · Delete removes the vertex under the cursor · Esc ends");
		return true;
	};
	G.stopEdit = function () {
		var e = G.edit;
		if (!e.active) return;
		e.active = false; e.drag = null; e.hot = null;
		if (app.controls) app.controls.enabled = true;
		G.editRedraw();
		G.syncToolControls();
		G.dig.events.push({type: "editMode", active: false});
		status("", 1);
	};
	G._editDown = function (ev) {
		if (ev["_q2t_editDown"]) return;
		ev["_q2t_editDown"] = true;
		var e = G.edit;
		if (!e.active || ev.button !== 0) return;
		if (e.mode === "select") {
			var hv0 = editNearest(ev.clientX, ev.clientY), hs0 = editNearestSeg(ev.clientX, ev.clientY);
			if (hv0 && String(e.items[hv0[0]].id).indexOf("s:") !== 0) hv0 = null;
			var hit = hv0 || hs0, ctrl = ev.ctrlKey || ev.metaKey;
			e.sel = e.sel || {}; e.selPts = e.selPts || {};
			if (hv0) {                                     // a vertex: select that point (and show its string)
				var pid = e.items[hv0[0]].id, pk = hv0[1];
				if (!ctrl) { e.sel = {}; e.selPts = {}; }
				e.selPts[pid] = e.selPts[pid] || {};
				e.selPts[pid][pk] = ctrl ? !e.selPts[pid][pk] : true;
				var anyp = false;
				for (var q2 in e.selPts[pid]) if (e.selPts[pid][q2]) anyp = true;
				e.sel[pid] = anyp || !!e.sel[pid];
				selChanged();
			} else if (hit) {
				var sid = e.items[hit[0]].id;
				if (ctrl) { e.sel[sid] = !e.sel[sid]; if (!e.sel[sid]) delete e.selPts[sid]; }
				else { e.sel = {}; e.selPts = {}; e.sel[sid] = true; }
				selChanged();
			} else {
				e.box = {x0: ev.clientX, y0: ev.clientY, x1: ev.clientX, y1: ev.clientY, add: ev.ctrlKey || ev.metaKey};
				if (app.controls) app.controls.enabled = false;
			}
			ev.stopPropagation(); ev.preventDefault();
			return;
		}
		if (e.mode === "link" || e.mode === "cap") {      // link strings one by one / close an end: report the clicked string
			var lv = editNearest(ev.clientX, ev.clientY), ls = lv ? null : editNearestSeg(ev.clientX, ev.clientY);
			var lh = lv || ls;
			if (lh && String(e.items[lh[0]].id).indexOf("s:") === 0)
				G.dig.events.push({type: e.mode === "cap" ? "capPick" : "linkPick", id: e.items[lh[0]].id});
			else status("Click on a string (its line or a vertex)", 2500);
			ev.stopPropagation(); ev.preventDefault();
			return;
		}
		if (e.mode && e.mode !== "move") {
			var hv = editNearest(ev.clientX, ev.clientY), hs = editNearestSeg(ev.clientX, ev.clientY), it, m;
			if (hv && String(e.items[hv[0]].id).indexOf("s:") !== 0) hv = null;
			if (e.mode === "insert" && hs) {
				it = e.items[hs[0]];
				var sp = G.dig.snapOn ? G.snapAt(ev.clientX, ev.clientY) : null;
				var p = (sp && sp.kind !== "section" && sp.kind !== "free") ? sp.p : hs[2];
				m = G.worldToMap(p);
				it.w.splice(hs[1] + 1, 0, p.clone());
				G.dig.events.push({type: "vertexInserted", id: it.id, seg: hs[1], p: [m.x, m.y, m.z]});
			} else if (e.mode === "cut" && (hv || hs)) {
				if (hv) G.dig.events.push({type: "stringCut", id: e.items[hv[0]].id, i: hv[1]});
				else { m = G.worldToMap(hs[2]); G.dig.events.push({type: "stringCut", id: e.items[hs[0]].id, seg: hs[1], p: [m.x, m.y, m.z]}); }
			} else if (e.mode === "copy" && (hv || hs)) {
				G.dig.events.push({type: "stringCopy", id: e.items[(hv || hs)[0]].id});
			} else return;
			e.hot = null; e.hotSeg = null;
			G.editRedraw();
			ev.stopPropagation(); ev.preventDefault();
			return;
		}
		var n = editNearest(ev.clientX, ev.clientY);
		if (!n) return;
		e.drag = n; e.moved = false;
		if (app.controls) app.controls.enabled = false;
		ev.stopPropagation(); ev.preventDefault();
		G.editRedraw();
	};
	G._editMove = function (ev) {
		var e = G.edit;
		if (!e.active) return;
		if (e.drag) {
			var p = editDragPos(ev.clientX, ev.clientY), dit = e.items[e.drag[0]], dn = dit.w.length;
			dit.w[e.drag[1]] = p.clone();
			if (dit.closed && dn > 3 && (e.drag[1] === 0 || e.drag[1] === dn - 1))       // both ends of an outline
				dit.w[e.drag[1] === 0 ? dn - 1 : 0] = p.clone();
			e.moved = true;
			G.editRedraw();
			ev.stopPropagation();
			return;
		}
		if (e.box) {
			e.box.x1 = ev.clientX; e.box.y1 = ev.clientY;
			var bd = boxDiv(), b = e.box;
			bd.style.left = Math.min(b.x0, b.x1) + "px"; bd.style.top = Math.min(b.y0, b.y1) + "px";
			bd.style.width = Math.abs(b.x1 - b.x0) + "px"; bd.style.height = Math.abs(b.y1 - b.y0) + "px";
			bd.style.display = "block";
			ev.stopPropagation();
			return;
		}
		if (e.mode === "select") {
			var hv3 = editNearest(ev.clientX, ev.clientY);
			if (hv3 && String(e.items[hv3[0]].id).indexOf("s:") !== 0) hv3 = null;
			e.hot = null; e.hotSeg = hv3 ? null : editNearestSeg(ev.clientX, ev.clientY);
			e.hotPt = hv3;
			G.editRedraw();
			return;
		}
		if (e.mode && e.mode !== "move") {
			var hv2 = e.mode === "insert" ? null : editNearest(ev.clientX, ev.clientY);
			if (hv2 && String(e.items[hv2[0]].id).indexOf("s:") !== 0) hv2 = null;
			var hs2 = hv2 ? null : editNearestSeg(ev.clientX, ev.clientY);
			e.hot = hv2; e.hotSeg = hs2;
			G.editRedraw();
			return;
		}
		var n = editNearest(ev.clientX, ev.clientY);
		if (String(n) !== String(e.hot)) { e.hot = n; G.editRedraw(); }
	};
	G._editUp = function (ev) {
		if (ev["_q2t_editUp"]) return;
		ev["_q2t_editUp"] = true;
		var e = G.edit;
		if (e.active && e.box) {
			var b = e.box;
			e.box = null;
			boxDiv().style.display = "none";
			if (app.controls) app.controls.enabled = true;
			if (Math.abs(b.x1 - b.x0) > 3 || Math.abs(b.y1 - b.y0) > 3) boxSelect(b, b.add);
			else if (!b.add) { e.sel = {}; e.selPts = {}; selChanged(); }      // click on empty space clears
			ev.stopPropagation();
			return;
		}
		if (!e.active || !e.drag) return;
		if (app.controls) app.controls.enabled = true;
		if (e.moved) {
			var it = e.items[e.drag[0]], m = G.worldToMap(it.w[e.drag[1]]);
			G.dig.events.push({type: "vertexMoved", id: it.id, i: e.drag[1], p: [m.x, m.y, m.z]});
		}
		e.drag = null;
		G.editRedraw();
		ev.stopPropagation();
	};
	G._editKey = function (ev) {
		if (ev["_q2t_editKey"]) return;
		ev["_q2t_editKey"] = true;
		var e = G.edit;
		if (!e.active) return;
		var ae = document.activeElement;
		if (ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return;     // typing in a field: not a shortcut
		if (ev.keyCode === 27) { G.stopEdit(); ev.stopPropagation(); ev.preventDefault(); }
		else if (ev.keyCode === 113 && e.mode === "select") {                  // F2: rename the selected string
			G.dig.events.push({type: "renameSelected"}); ev.stopPropagation(); ev.preventDefault();
		}
		else if ((e.mode === "link" || e.mode === "cap") && (ev.keyCode === 8 || ev.keyCode === 46)) {
			G.dig.events.push({type: e.mode === "cap" ? "capUndo" : "linkUndo"}); ev.stopPropagation(); ev.preventDefault();
		}
		else if (e.mode === "link" && ev.keyCode === 13) { G.stopEdit(); ev.stopPropagation(); ev.preventDefault(); }
		else if (ev.keyCode === 83 && !ev.ctrlKey && !ev.altKey) { G.toggleSnap(); ev.stopPropagation(); ev.preventDefault(); }
		else if ((ev.keyCode === 46 || ev.keyCode === 8) && e.mode === "select") {
			var pts = selPointList(), ids = [];
			if (pts.length) { G.dig.events.push({type: "pointsDelete", points: pts}); e.selPts = {}; }
			else {
				for (var k in (e.sel || {})) if (e.sel[k]) ids.push(k);
				if (ids.length) G.dig.events.push({type: "stringsDelete", ids: ids});
			}
			ev.stopPropagation(); ev.preventDefault();
		}
		else if ((ev.keyCode === 46 || ev.keyCode === 8) && e.hot && (!e.mode || e.mode === "move")) {
			var it = e.items[e.hot[0]];
			if (it.w.length > 2) {
				G.dig.events.push({type: "vertexDeleted", id: it.id, i: e.hot[1]});
				it.w.splice(e.hot[1], 1);
				e.hot = null;
				G.editRedraw();
			}
			ev.stopPropagation(); ev.preventDefault();
		}
	};

	// ------------------------------------------------------------- manual triangles
	// G.startTriangles(): click three corners (snaps to wireframe vertices, strings, drillholes, surfaces);
	// each triangle goes to Python as {type: 'triangle', pts}. Backspace removes the last corner, Esc ends.
	G.tri = G.tri || {active: false, pts: []};
	function triEnsure() {
		if (G.tri.obj && G.tri.obj.parent) return;
		var grp = new THREE.Group(); grp.name = "q2t4 triangles";
		var l = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#00e0ff", depthTest: false}));
		l.name = "line"; l.renderOrder = 999; grp.add(l);
		var p = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#00e0ff", size: 9, sizeAttenuation: false, depthTest: false}));
		p.name = "pts"; p.renderOrder = 1000; grp.add(p);
		var mk = new THREE.Points(new THREE.BufferGeometry(), new THREE.PointsMaterial({color: "#ffffff", size: 13, sizeAttenuation: false, depthTest: false}));
		mk.name = "marker"; mk.renderOrder = 1001; grp.add(mk);
		app.scene.add(grp);
		G.tri.obj = grp;
	}
	G.triRedraw = function () {
		triEnsure();
		var t = G.tri, o = t.obj, pts = t.pts.slice();
		if (t.hover && pts.length) pts.push(t.hover.p);
		if (pts.length === 3) pts.push(pts[0]);
		setGeom(o.getObjectByName("line"), pts.length > 1 ? pts : []);
		setGeom(o.getObjectByName("pts"), t.pts);
		var mk = o.getObjectByName("marker");
		setGeom(mk, t.hover ? [t.hover.p] : []);
		if (t.hover) mk.material.color.set(SNAP_COLORS[t.hover.kind] || (t.hover.kind === "wire" ? "#00e0ff" : "#ffffff"));
		o.visible = t.active;
		render();
	};
	G.startTriangles = function () {
		if (!G.attach()) return false;
		if (G.tdel && G.tdel.active) G.stopTriDelete();
		if (G.dig.active) G.stopDigitise();
		if (G.edit && G.edit.active) G.stopEdit();
		if (G.meas && G.meas.active) G.stopMeasure();
		if (G.queryMode) G.setQueryMode(false);
		var t = G.tri;
		t.active = true; t.pts = []; t.hover = null;
		t.wasWire = G.dig.snap.wire; G.dig.snap.wire = true;
		app.renderer.domElement.style.cursor = "crosshair";
		if (!t.hooked && !app._q2t4TriHook) {
			t.hooked = app._q2t4TriHook = true;
			var el = app.renderer.domElement;
			el.addEventListener("mousedown", function (ev) { var M = window.Q2T4; if (M && M._triDown) M._triDown(ev); }, true);
			window.addEventListener("mousemove", function (ev) { var M = window.Q2T4; if (M && M._triMove) M._triMove(ev); }, true);
		}
		G.syncToolControls();
		G.dig.events.push({type: "triMode", active: true});
		G.triRedraw();
		status("Triangles: click 3 corners (snaps to wireframe and string vertices) · Backspace undo · S snapping · Esc ends");
		return true;
	};
	G.stopTriangles = function () {
		var t = G.tri;
		if (!t.active) return;
		t.active = false; t.pts = []; t.hover = null;
		G.dig.snap.wire = !!t.wasWire;
		app.renderer.domElement.style.cursor = "";
		G.syncToolControls();
		G.dig.events.push({type: "triMode", active: false});
		G.triRedraw();
		status("", 1);
	};
	G._triDown = function (ev) {
		if (ev["_q2t_triDown"]) return;
		ev["_q2t_triDown"] = true;
		var t = G.tri;
		if (!t.active || ev.button !== 0) return;
		var s = G.snapAt(ev.clientX, ev.clientY);
		if (!s) { status("No point here - snap to a vertex or surface, or turn on a section", 2500); return; }
		for (var i = 0; i < t.pts.length; i++) if (t.pts[i].distanceToSquared(s.p) < 1e-8) return;   // same corner twice
		t.pts.push(s.p.clone());
		if (t.pts.length === 3) {
			var mp = t.pts.map(function (p) { var m = G.worldToMap(p); return [m.x, m.y, m.z]; });
			G.dig.events.push({type: "triangle", pts: mp});
			t.pts = [];
		}
		G.triRedraw();
		ev.stopPropagation(); ev.preventDefault();
	};
	G._triMove = function (ev) {
		var t = G.tri;
		if (!t.active) return;
		var now = Date.now();
		if (t._t && now - t._t < 40) return;
		t._t = now;
		t.hover = G.snapAt(ev.clientX, ev.clientY);
		G.triRedraw();
	};
	G._triKey = function (e) {
		var k = e.keyCode;
		if (k === 27) { G.stopTriangles(); e.preventDefault(); e.stopPropagation(); }
		else if (k === 8 || k === 46) { G.tri.pts.pop(); G.triRedraw(); e.preventDefault(); e.stopPropagation(); }
	};

	// ------------------------------------------------------------- delete triangles
	// G.startTriDelete(): click a triangle of a wireframe to delete it (event {type: 'triangleDelete', id, face});
	// the triangle under the cursor is drawn red. Esc ends.
	G.tdel = G.tdel || {active: false};
	function tdelHit(cx, cy) {
		var targets = [];
		for (var id in G.meshes) {
			if (String(id).indexOf("bm:") === 0) continue;
			var o = G.meshes[id].obj;
			if (o.visible) { var f = o.getObjectByName("faces"); if (f) targets.push(f); }
		}
		if (!targets.length) return null;
		var hits = rayAt(cx, cy).intersectObjects(targets, false);
		for (var i = 0; i < hits.length; i++) {
			var h = hits[i];
			if (G.sectionPlanes.length && !G.insideSection(h.point)) continue;
			var o2 = h.object, mid = null;
			while (o2) { if (o2.userData && o2.userData.q2t4Id !== undefined) { mid = o2.userData.q2t4Id; break; } o2 = o2.parent; }
			if (mid === null || h.faceIndex === undefined) continue;
			var g = h.object.geometry, ix = g.getIndex(), a = g.getAttribute("position"), k = h.faceIndex * 3, w = [];
			h.object.updateMatrixWorld(true);
			for (var j = 0; j < 3; j++) {
				var vi = ix ? ix.getX(k + j) : k + j;
				w.push(new THREE.Vector3(a.getX(vi), a.getY(vi), a.getZ(vi)).applyMatrix4(h.object.matrixWorld));
			}
			return {id: mid, face: h.faceIndex, w: w};
		}
		return null;
	}
	function tdelEnsure() {
		if (G.tdel.obj && G.tdel.obj.parent) return;
		var m = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({color: "#ff2a2a", side: THREE.DoubleSide,
			depthTest: false, transparent: true, opacity: 0.6}));
		m.name = "q2t4 tri delete"; m.renderOrder = 1000;
		var l = new THREE.LineLoop(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#ffffff", depthTest: false}));
		l.renderOrder = 1001; m.add(l);
		app.scene.add(m);
		G.tdel.obj = m;
	}
	function tdelDraw(hit) {
		tdelEnsure();
		var m = G.tdel.obj;
		m.geometry.dispose();
		m.geometry = new THREE.BufferGeometry().setFromPoints(hit ? hit.w : []);
		m.children[0].geometry.dispose();
		m.children[0].geometry = new THREE.BufferGeometry().setFromPoints(hit ? hit.w : []);
		m.visible = !!hit && G.tdel.active;
		render();
	}
	G.startTriDelete = function () {
		if (!G.attach()) return false;
		if (G.dig.active) G.stopDigitise();
		if (G.edit && G.edit.active) G.stopEdit();
		if (G.tri && G.tri.active) G.stopTriangles();
		if (G.meas && G.meas.active) G.stopMeasure();
		if (G.queryMode) G.setQueryMode(false);
		var t = G.tdel;
		t.active = true;
		if (!t.hooked && !app._q2t4TdelHook) {
			t.hooked = app._q2t4TdelHook = true;
			var el = app.renderer.domElement;
			el.addEventListener("mousedown", function (ev) { var M = window.Q2T4; if (M && M._tdelDown) M._tdelDown(ev); }, true);
			window.addEventListener("mousemove", function (ev) { var M = window.Q2T4; if (M && M._tdelMove) M._tdelMove(ev); }, true);
			window.addEventListener("keydown", function (ev) { var M = window.Q2T4; if (M && M._tdelKey) M._tdelKey(ev); }, true);
		}
		app.renderer.domElement.style.cursor = "crosshair";
		G.syncToolControls();
		G.dig.events.push({type: "triDelMode", active: true});
		status("Delete triangles: click a triangle of a wireframe (it shows red under the cursor) · Esc ends");
		return true;
	};
	G.stopTriDelete = function () {
		var t = G.tdel;
		if (!t.active) return;
		t.active = false;
		app.renderer.domElement.style.cursor = "";
		tdelDraw(null);
		G.syncToolControls();
		G.dig.events.push({type: "triDelMode", active: false});
		status("", 1);
	};
	G._tdelDown = function (ev) {
		if (ev["_q2t_tdelDown"]) return;
		ev["_q2t_tdelDown"] = true;
		var t = G.tdel;
		if (!t.active || ev.button !== 0) return;
		var h = tdelHit(ev.clientX, ev.clientY);
		if (!h) return;                               // empty space: let the view orbit
		var mp = h.w.map(function (p) { var m = G.worldToMap(p); return [m.x, m.y, m.z]; });
		G.dig.events.push({type: "triangleDelete", id: h.id, face: h.face, pts: mp});
		tdelDraw(null);
		ev.stopPropagation(); ev.preventDefault();
	};
	G._tdelMove = function (ev) {
		var t = G.tdel;
		if (!t.active) return;
		var now = Date.now();
		if (t._t && now - t._t < 40) return;
		t._t = now;
		tdelDraw(tdelHit(ev.clientX, ev.clientY));
	};
	G._tdelKey = function (ev) {
		if (ev["_q2t_tdelKey"]) return;
		ev["_q2t_tdelKey"] = true;
		if (G.tdel.active && ev.keyCode === 27) { G.stopTriDelete(); ev.stopPropagation(); ev.preventDefault(); }
	};

	// ------------------------------------------------------------- saved views
	G.getView = function () {
		if (!window.app || !app.camera || !app.controls) return "";
		var c = G.worldToMap(app.camera.position), t = G.worldToMap(app.controls.target);
		return JSON.stringify({cam: [c.x, c.y, c.z], target: [t.x, t.y, t.z], zoom: app.camera.zoom || 1,
			ortho: !!app.camera.isOrthographicCamera, section: G.section || null});
	};
	G.setViewState = function (v) {
		if (!window.app || !app.camera || !app.controls || !v) return false;
		var c = G.mapToWorld(v.cam[0], v.cam[1], v.cam[2]), t = G.mapToWorld(v.target[0], v.target[1], v.target[2]);
		app.camera.position.copy(c);
		app.controls.target.copy(t);
		if (v.zoom) { app.camera.zoom = v.zoom; app.camera.updateProjectionMatrix(); }
		app.camera.lookAt(t);
		app.controls.update();
		render();
		return true;
	};

	// one dispatcher for the new modes (installed once per page; calls the current module)
	G._hookTools = function () {
		if (!app.controls || app._q2t4ToolsHook) return;
		app._q2t4ToolsHook = true;
		var pending = false;
		app.controls.addEventListener("change", function () {
			if (pending) return;
			pending = true;
			window.setTimeout(function () { pending = false; var M = window.Q2T4; if (M && M.updateLabels) M.updateLabels(); }, 40);
		});
		window.addEventListener("resize", function () { var M = window.Q2T4; if (M && M.updateLabels) M.updateLabels(); });
	};

	G.attach();
})();

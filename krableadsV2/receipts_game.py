"""The /receipts board's ambient "game mode" sprite layer — OPTIONAL.

Served at /receipts/game.js by receipts_page.py. Delete this file and the
board runs identically (the route serves an empty stub); the layer talks to
the page over ONE window event bus ('krab' CustomEvents) and its own
localStorage key ('krab_game', default off) — nothing else is shared.

Canvas overlay drawn only in the gutter rails beside the content column;
fixed-timestep 60Hz logic with interpolated rendering; procedurally generated
pixel-art atlas (no image assets); auto-degrading quality tiers; reduced-motion
and sub-1100px viewports get nothing. The JS here IS the source — edit it
directly. window.krabGame = {setMode, mode, celebrate}.
"""

GAME_JS = r'''
/* game_layer.js — "Video Game Mode": an ambient pixel-sprite layer for the
 * /receipts board.  Served as /receipts/game.js, included with <script defer>.
 *
 * CONTRACT (the page must run identically if this file is deleted):
 *   - talks to the page ONLY via the window 'krab' CustomEvent bus, its own
 *     localStorage key 'krab_game', and ONE body class — 'krab-tetris-on',
 *     set while a tetris game is up so the page can style its toasts out of
 *     the panel's way, and always removed again on game over / close /
 *     teardown (the page needs no CSS for it; nothing here reads it back);
 *   - defines exactly ONE global: window.krabGame =
 *     {setMode, mode, celebrate, playTetris, say};
 *   - sprites live ONLY in the side gutter rails mirrored around <main>'s
 *     content box (or the bottom strip) — a canvas clip path enforces this
 *     structurally, so a sprite can never be drawn over content.  Two spec'd
 *     exceptions: chat bubbles (viewport-clamped, never inside the mic dead
 *     zone or over the tetris HUD band — the panel-card top down to the board
 *     top) and, while the OPTIONAL
 *     window.krabTetris panel is open, the panel's own rect — sprites
 *     deliberately play ON the panel (canvas z raised 22 -> 23 for the game,
 *     restored on stop; pointer-events stays none so play is unaffected);
 *   - rail < 120px hides that rail; both too narrow, viewport < 1100px, OR a
 *     coarse-pointer device -> the BOTTOM STRIP (64px; 72px coarse pointers),
 *     sprites at their normal x3 scale.  A mic dead zone (centerX ±90px, the
 *     page's floating voice button) excludes waypoints, the car, and bubbles;
 *   - ONE fixed, full-viewport canvas: pointer-events none, aria-hidden,
 *     role=presentation, tabindex -1, z-index var(--z-sprites, 20) (kept below
 *     the page's .overlay z:50 / #toasts z:60); the element itself never moves
 *     or resizes per frame (zero CLS) — only draw commands animate.  The one
 *     interactive DOM this layer may own is the tiny tetris-invite chip
 *     (44px targets, auto-dismissed, removed on teardown);
 *   - window pointerdown/pointermove listeners are PASSIVE and never call
 *     preventDefault — taps and swipes always reach the page untouched;
 *   - default mode 'off' on first load; 'subtle' = 2 sprites (1 on mobile),
 *     major reactions only; 'full' = up to 6 (frame-budget tiers may lower);
 *   - prefers-reduced-motion: static poses with slow cross-fades only.
 */
(function () {
  'use strict';
  if (window.krabGame) return;                 // double-include guard

  /* ── tuning constants (all timing in ms, distances in CSS px) ──────────── */
  const STEP = 1000 / 60;        // fixed simulation timestep
  const MAX_DT = 50;             // clamp a janky frame; never fast-forward
  const MAX_STEPS = 4;           // spiral-of-death guard
  const SCALE = 3;               // integer sprite scale only — pixel art law
  const CW = 24, CH = 24;        // atlas cell size (characters are 12-16px tall)
  const RAIL_MIN = 120;          // a rail narrower than this hides
  const STRIP_H = 64;            // bottom-strip height (fine pointers)
  const STRIP_H_COARSE = 72;     // …taller on coarse pointers (thumb room)
  const VIEW_MIN = 1100;         // below this: bottom-strip mode, always
  const DEAD_HALF = 90;          // mic dead zone: centerX ± this — waypoints,
                                 //   the car path and bubbles NEVER enter it
  const TAP_CD = 2000;           // per-sprite tap-reaction cooldown
  const DODGE_CD = 3000;         // per-sprite dodge cooldown
  const DODGE_SPEED = 0.6;       // px/ms pointer speed that reads as a lunge
  const IDLE_QUIP_MIN = 45000, IDLE_QUIP_MAX = 120000;  // per-sprite quips
  const INVITE_IDLE = 90000;     // idle since boot/last game before an invite
  const INVITE_TTL = 8000;       // invite chip auto-dismiss
  const INVITE_DECLINE = 300000; // NO -> 5min cooldown
  const CONTENT_GAP = 10;        // breathing room between a rail and content
  const WALK_SPEED = 26;         // px/s base; personality scales ±15%
  const GRAV = 760;              // px/s² for procedural jumps
  const ATT_R2 = 180 * 180;      // cursor attention radius, squared
  const WAVE_COOLDOWN = 45000;   // per-sprite cursor-wave cooldown
  const BIG_GAP = 4000;          // min gap between big reactions
  const COALESCE_MS = 800;       // event debounce window (merge same-type)
  const P_MAX = 200;             // particle pool hard cap
  const DEGRADE_MS = 12;         // avg frame cost that forces a tier down…
  const DEGRADE_AFTER = 2000;    // …after this long over budget
  const RECOVER_MS = 8;          // avg frame cost that earns a tier back…
  const RECOVER_AFTER = 10000;   // …after this long under it
  const TIERS = [                // High -> Medium -> Low -> Off
    { name: 'High',   sprites: 6, pmul: 1   },
    { name: 'Medium', sprites: 4, pmul: 0.5 },
    { name: 'Low',    sprites: 2, pmul: 0   },
    { name: 'Off',    sprites: 0, pmul: 0   },
  ];
  const DEBUG = (location.search.indexOf('gamedebug=1') >= 0);

  /* ── easings — each one exists for a reason, see MOTION notes inline ───── */
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function rand(a, b) { return a + Math.random() * (b - a); }
  function randi(n) { return (Math.random() * n) | 0; }
  function easeOutExpo(t) { return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); }       // entrances
  function easeInOutCubic(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; } // walk start/stop
  function easeInOutQuad(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }      // turns
  function easeOutBack(t) { const c = 1.70158; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); } // settles
  // cubic-bezier(.34,1.56,.64,1) for pops/waves — solved into a LUT once at
  // boot so the hot path is a lerp, not a Newton iteration.
  function bezierLUT(x1, y1, x2, y2) {
    const N = 64, lut = new Float32Array(N + 1);
    for (let i = 0; i <= N; i++) {
      const x = i / N; let lo = 0, hi = 1, t = x;
      for (let k = 0; k < 22; k++) {
        t = (lo + hi) / 2;
        const cx = 3 * (1 - t) * (1 - t) * t * x1 + 3 * (1 - t) * t * t * x2 + t * t * t;
        if (cx < x) lo = t; else hi = t;
      }
      lut[i] = 3 * (1 - t) * (1 - t) * t * y1 + 3 * (1 - t) * t * t * y2 + t * t * t;
    }
    return function (t) {
      if (t <= 0) return 0; if (t >= 1) return 1;
      const f = t * N, i = f | 0;
      return lut[i] + (lut[i + 1 > N ? N : i + 1] - lut[i]) * (f - i);
    };
  }
  const easePop = bezierLUT(0.34, 1.56, 0.64, 1);

  /* ── theme — colors come from the page's CSS variables at render time.
   * Refreshed at most every 500ms (getPropertyValue allocates strings; the
   * refresh runs OUTSIDE the per-particle hot loops). ─────────────────────── */
  const THEME = {
    ink: '#172b4d', muted: '#6b778c', line: '#dfe1e6', accent: '#0065ff',
    ok: '#00875a', glow: '#0065ff', bub: '#ffffff',
  };
  const CONF = ['#0065ff', '#6554c0', '#00875a', '#e2a33b', '#0065ff', '#8993a4', '#00875a'];
  let themeAt = -1;
  // hoisted so refreshTheme allocates no closure per refresh
  function cssVar(cs, n, fb) { const s = cs.getPropertyValue(n); const t = s && s.trim(); return t || fb; }
  function refreshTheme(now) {
    if (now - themeAt < 500) return;
    themeAt = now;
    const cs = getComputedStyle(document.documentElement);
    const prevGlow = THEME.glow, prevOk = THEME.ok;
    THEME.ink = cssVar(cs, '--ink', THEME.ink);
    THEME.muted = cssVar(cs, '--muted', THEME.muted);
    THEME.line = cssVar(cs, '--line', THEME.line);
    THEME.accent = cssVar(cs, '--accent', THEME.accent);
    THEME.ok = cssVar(cs, '--del', THEME.ok);
    THEME.glow = cssVar(cs, '--accent', THEME.glow);
    THEME.bub = cssVar(cs, '--card', THEME.bub);   // bubble fill tracks the page's card bg
    // confetti draws from the page's own status palette (new colors included)
    CONF[0] = cssVar(cs, '--accent', CONF[0]);
    CONF[1] = cssVar(cs, '--paid', CONF[1]);
    CONF[2] = cssVar(cs, '--del', CONF[2]);
    CONF[3] = cssVar(cs, '--followup', '#e2a33b');
    CONF[4] = cssVar(cs, '--otw', CONF[4]);
    CONF[5] = cssVar(cs, '--issued', '#8993a4');
    CONF[6] = cssVar(cs, '--uploaded', '#00875a');
    if (THEME.glow !== prevGlow || THEME.ok !== prevOk) glowGradsDirty = true;
  }

  /* ── the atlas — every sprite drawn ONCE at boot into an offscreen canvas.
   * No image assets, no decodes after boot.  Three characters (12-16px tall on
   * a 24px cell, drawn facing RIGHT, feet baseline y=21) plus a pixel car and
   * a props row.  Craft rules: 1px near-black outline, 2-3 value ramp per
   * material, readable silhouettes.  Heads live in SEPARATE cells so the head
   * can lag the torso by ~2 animation frames at runtime (overlapping action).
   */
  const PALS = [
    { // 0 — courier kid: red cap, blue shirt, satchel
      outline: '#1a1c2c', skin: '#f2b28c', skinD: '#d18d66',
      torso: '#3b7dd8', torsoL: '#6aa3e8', torsoD: '#2a5aa0',
      sleeve: '#3b7dd8', hand: '#f2b28c', pants: '#33384a', shoe: '#1f2333',
      cap: '#e23b46', capD: '#a92735', bag: '#c9932f', bagStrap: '#8a6d3b',
    },
    { // 1 — hoodie walker: green hoodie, drawstrings are runtime springs
      outline: '#1a1c2c', skin: '#e8a06c', skinD: '#c47f4e',
      torso: '#3fa456', torsoL: '#5ec978', torsoD: '#2c7a3f',
      sleeve: '#3fa456', hand: '#e8a06c', pants: '#2e2f3e', shoe: '#22242f',
      hood: '#3fa456', hoodD: '#2c7a3f', string: '#e6e6e6',
    },
    { // 2 — robot: metal ramp, cyan visor, antenna is a runtime spring
      outline: '#141821', skin: '#9aa5b1', skinD: '#6d7683',
      torso: '#9aa5b1', torsoL: '#c6cdd6', torsoD: '#6d7683',
      sleeve: '#8a95a1', hand: '#c6cdd6', pants: '#6d7683', shoe: '#3a3f4d',
      visor: '#7ef0f6', visorD: '#1e8f96', accent: '#e2a33b',
    },
  ];
  // body animation layout — column ranges inside each character's atlas row
  const BODY = {
    walk: [0, 8], idle: [8, 2], wave: [10, 4], celebrate: [14, 4],
    slump: [18, 2], sleep: [20, 2], turn: [22, 3], notice: [25, 1],
    stretch: [26, 2], phone: [28, 2], scratch: [30, 2],
  };
  const HEADCOL = 32;           // 4 head variants: open, blink, happy, side
  const ATLAS_COLS = 36;
  const PROPS = { car0: 0, car1: 1, flag0: 2, flag1: 3, scroll0: 4, scroll1: 5,
                  scroll2: 6, check: 7, zz: 8, bang: 9, note: 10, pebble: 11,
                  heart: 12 };
  // playback speeds; celebrate/turn/notice are driven frame-by-frame by state code
  const ANIMS = {
    walk: { ms: 83, loop: true }, idle: { ms: 500, loop: true },
    wave: { ms: 125, loop: true }, celebrate: { ms: 120, loop: false },
    slump: { ms: 650, loop: true }, sleep: { ms: 950, loop: true },
    turn: { ms: 70, loop: false }, notice: { ms: 250, loop: false },
    stretch: { ms: 620, loop: true }, phone: { ms: 520, loop: true },
    scratch: { ms: 300, loop: true },
  };
  // 8-frame walk: contact(squash) / down / pass / up, both sides.  Feet are
  // [frontDx, frontLift, backDx, backLift]; bob is phase-locked to footfalls.
  const WALK_FEET = [
    [2, 0, -2, 0], [1, 0, -1, 1], [0, 0, 0, 2], [-1, 0, 1, 1],
    [-2, 0, 2, 0], [-1, 1, 1, 0], [0, 2, 0, 0], [1, 1, -1, 0],
  ];
  const WALK_BOB = [1, 0, -1, 0, 1, 0, -1, 0];
  const WALK_ARM_F = ['back', 'back', 'down', 'fwd', 'fwd', 'fwd', 'down', 'back'];
  const WALK_ARM_B = ['fwd', 'fwd', 'down', 'back', 'back', 'back', 'down', 'fwd'];
  const F0 = [0, 0, 0, 0];

  function poseFor(anim, i) {                    // boot-time only — may allocate
    switch (anim) {
      case 'walk': return { feet: WALK_FEET[i], bob: WALK_BOB[i],
        squash: (i === 0 || i === 4),            // 8% squash on contact frames
        armF: WALK_ARM_F[i], armB: WALK_ARM_B[i], headDrop: 0 };
      case 'idle': return { feet: F0, bob: i ? 1 : 0, armF: 'down', armB: 'down', headDrop: 0 };
      case 'wave': return { feet: F0, bob: 0, headDrop: 0, armB: 'down',
        armF: ['wave1', 'wave2', 'wave1', 'wave3'][i] };
      case 'celebrate': return [
        { feet: F0, bob: 2, squash: true, armF: 'back', armB: 'back', headDrop: 1 }, // crouch (anticipation)
        { feet: [0, 2, 0, 2], bob: -1, armF: 'up', armB: 'up', headDrop: 0 },        // airborne
        { feet: [0, 2, 0, 1], bob: -1, armF: 'punch', armB: 'down', headDrop: 0 },   // fist at apex
        { feet: F0, bob: 2, squash: true, armF: 'fwd', armB: 'back', headDrop: 1 },  // land squash
      ][i];
      case 'slump': return { feet: F0, bob: 1 + i, armF: 'down', armB: 'down', headDrop: 2 };
      case 'sleep': return { feet: F0, bob: 1 + i, armF: 'down', armB: 'down', headDrop: 2 };
      case 'turn': return [
        { feet: F0, bob: 0, narrow: 1, armF: 'down', armB: 'down', headDrop: 0 },
        { feet: F0, bob: 0, front: true, headDrop: 0 },
        { feet: F0, bob: 0, narrow: 1, armF: 'down', armB: 'down', headDrop: 0 },
      ][i];
      case 'notice': return { feet: F0, bob: -1, armF: 'down', armB: 'down', headDrop: 0 };
      case 'stretch': return { feet: F0, bob: i ? 0 : -1, armF: 'up', armB: 'up', headDrop: 0 };
      case 'phone': return { feet: F0, bob: i ? 1 : 0, armF: 'chest', armB: 'down', headDrop: 1 };
      case 'scratch': return { feet: F0, bob: i ? 1 : 0, armF: 'head', armB: 'down', headDrop: 0 };
    }
    return { feet: F0, bob: 0, armF: 'down', armB: 'down', headDrop: 0 };
  }

  /* per-anim-frame metadata the runtime needs (head anchor bob + head drop) */
  const META_B = {}, META_H = {};

  function painterAt(g, col, row) {
    const ox = col * CW, oy = row * CH;
    return function (x, y, w, h, c) { g.fillStyle = c; g.fillRect(ox + x, oy + y, w, h); };
  }
  function boxAt(P, x, y, w, h, fill, out) {
    P(x, y, w, h, out); P(x + 1, y + 1, w - 2, h - 2, fill);
  }
  function legDraw(P, pal, x, hipY, lift) {
    const footY = 21 - (lift || 0);
    if (footY > hipY) P(x, hipY, 2, footY - hipY, pal.pants);
    P(x, footY, 3, 1, pal.shoe);
  }
  function armDraw(P, pal, front, v, bob) {
    const x = front ? 16 : 6, y = 12 + bob;
    const sl = pal.sleeve, hd = pal.hand;
    switch (v) {
      case 'back':  P(x - 1, y, 2, 4, sl); P(x - 1, y + 4, 2, 1, hd); break;
      case 'fwd':   P(x + 1, y, 2, 4, sl); P(x + 1, y + 4, 2, 1, hd); break;
      case 'up':    P(x, y - 6, 2, 6, sl); P(x, y - 7, 2, 1, hd); break;
      case 'wave1': P(x + 1, y - 5, 2, 5, sl); P(x + 1, y - 6, 2, 1, hd); break;
      case 'wave2': P(x + 2, y - 4, 2, 4, sl); P(x + 3, y - 5, 2, 1, hd); break;
      case 'wave3': P(x, y - 6, 2, 6, sl); P(x - 1, y - 7, 2, 1, hd); break;
      case 'punch': P(x + 1, y - 4, 2, 4, sl); P(x + 2, y - 5, 2, 1, hd); break;
      case 'chest': P(x, y, 2, 2, sl); P(x - 2, y + 2, 2, 1, hd); break;
      case 'head':  P(x, y - 4, 2, 4, sl); P(x - 1, y - 5, 2, 1, hd); break;
      default:      P(x, y, 2, 4, sl); P(x, y + 4, 2, 1, hd); break;     // down
    }
  }
  function paintBody(g, col, char, pose) {
    const P = painterAt(g, col, char), pal = PALS[char];
    const bob = pose.bob + (pose.squash ? 1 : 0);
    const w = pose.squash ? 10 : (pose.narrow ? 6 : 8);
    const tx = 12 - (w >> 1);
    if (!pose.front) armDraw(P, pal, false, pose.armB || 'down', bob);   // back arm behind torso
    const f = pose.feet || F0;
    legDraw(P, pal, 9 + f[2], 17 + bob, f[3]);                          // back leg
    legDraw(P, pal, 13 + f[0], 17 + bob, f[1]);                         // front leg
    if (char === 0) { P(5, 15 + bob, 3, 3, pal.bag); P(5, 15 + bob, 3, 1, pal.outline); } // satchel
    const th = pose.squash ? 5 : 6;
    boxAt(P, tx, 11 + bob, w, th, pal.torso, pal.outline);
    P(tx + 1, 12 + bob, w - 2, 1, pal.torsoL);                          // top light
    P(tx + w - 2, 12 + bob, 1, th - 2, pal.torsoD);                     // side shade
    if (char === 0) { P(tx + 1, 12 + bob, 1, 3, pal.bagStrap); P(tx + 2, 14 + bob, 1, 1, pal.bagStrap); }
    if (char === 1) P(tx + 2, 14 + bob, w - 4, 1, pal.torsoD);          // hoodie pocket
    if (char === 2) { P(tx + 2, 12 + bob, 3, 2, pal.visorD); P(tx + 2, 12 + bob, 1, 1, pal.visor);
                      P(tx + w - 3, 13 + bob, 1, 1, pal.accent); }       // chest panel
    P(11, 10 + bob, 2, 1, pal.skin);                                    // neck
    if (pose.front) { armDraw(P, pal, false, 'down', bob); armDraw(P, pal, true, 'down', bob); }
    else armDraw(P, pal, true, pose.armF || 'down', bob);
  }
  function paintHead(g, col, char, variant) {   // 0 open, 1 blink, 2 happy, 3 side
    const P = painterAt(g, col, char), pal = PALS[char];
    if (char === 2) {                            // robot head: metal + visor
      boxAt(P, 7, 9, 10, 8, pal.torso, pal.outline);
      P(14, 10, 2, 6, pal.torsoD);
      P(9, 11, 7, 3, pal.visorD);
      P(11, 8, 2, 1, pal.torsoD);                // antenna mount (spring at runtime)
      P(6, 12, 1, 2, pal.torsoD);                // ear bolt
      const ey = variant === 2 ? 11 : (variant === 1 ? 13 : 12);
      const exo = variant === 3 ? 1 : 0;
      P(10 + exo, ey, 2, 1, pal.visor); P(13 + exo, ey, 2, 1, pal.visor);
      return;
    }
    boxAt(P, 7, 9, 10, 8, pal.skin, pal.outline);
    P(15, 10, 1, 6, pal.skinD);
    let e1 = 11, e2 = 14, my = 15;
    if (char === 1) {                            // hood ring, face in the opening
      boxAt(P, 6, 8, 12, 10, pal.hood, pal.outline);
      P(9, 11, 6, 5, pal.skin);
      P(16, 9, 1, 8, pal.hoodD);
      e1 = 10; e2 = 13;
    } else {                                     // kid cap + forward brim
      boxAt(P, 7, 7, 10, 3, pal.cap, pal.outline);
      P(15, 9, 4, 1, pal.capD);
    }
    const exo = variant === 3 ? 1 : 0;
    if (variant === 1) { P(e1 + exo, 13, 1, 1, pal.outline); P(e2 + exo, 13, 1, 1, pal.outline); }
    else if (variant === 2) {
      P(e1 - 1, 13, 1, 1, pal.outline); P(e1, 12, 1, 1, pal.outline); P(e1 + 1, 13, 1, 1, pal.outline);
      P(e2 - 1, 13, 1, 1, pal.outline); P(e2, 12, 1, 1, pal.outline); P(e2 + 1, 13, 1, 1, pal.outline);
      P(e1 + 1, my, 2, 1, pal.outline);          // smile
    } else { P(e1 + exo, 12, 1, 2, pal.outline); P(e2 + exo, 12, 1, 2, pal.outline); }
    if (variant !== 2) P(e1 + 2, my, 1, 1, pal.skinD);
  }
  function paintCar(g, col, f) {                 // side-view hatchback, 2 wheel frames
    const P = painterAt(g, col, 3);
    boxAt(P, 1, 11, 21, 8, '#c8433c', '#26140f');
    P(2, 12, 19, 1, '#e06a5f');                  // roof light
    P(2, 16, 19, 1, '#93271f');                  // rocker shade
    P(5, 12, 5, 3, '#bfe0ef'); P(12, 12, 5, 3, '#bfe0ef'); P(11, 12, 1, 3, '#26140f');
    P(21, 14, 1, 2, '#ffd964');                  // headlight (front = right)
    P(1, 14, 1, 2, '#7a2019');                   // tail
    const wheel = (x) => { P(x, 17, 4, 4, '#14161f'); P(x + 1, 18, 2, 2, '#3a3f4d');
                           P(x + 1 + (f ? 1 : 0), 18 + (f ? 1 : 0), 1, 1, '#aeb6c2'); };
    wheel(4); wheel(15);
  }
  function paintProps(g) {
    let P;
    P = painterAt(g, PROPS.flag0, 3);            // little flag, 2 wave frames
    P(11, 8, 1, 10, '#8a6d3b'); P(12, 8, 5, 1, '#e23b46'); P(12, 9, 4, 1, '#e23b46'); P(12, 10, 3, 1, '#a92735');
    P = painterAt(g, PROPS.flag1, 3);
    P(11, 8, 1, 10, '#8a6d3b'); P(12, 9, 5, 1, '#e23b46'); P(12, 10, 4, 1, '#a92735'); P(12, 8, 2, 1, '#e23b46');
    P = painterAt(g, PROPS.scroll0, 3);          // scroll: rolled -> half -> open
    boxAt(P, 9, 12, 6, 4, '#e8dcc0', '#8a7d5c');
    P = painterAt(g, PROPS.scroll1, 3);
    boxAt(P, 8, 10, 8, 7, '#e8dcc0', '#8a7d5c'); P(10, 12, 4, 1, '#b0a488');
    P = painterAt(g, PROPS.scroll2, 3);
    boxAt(P, 7, 9, 10, 9, '#e8dcc0', '#8a7d5c');
    P(9, 11, 6, 1, '#b0a488'); P(9, 13, 6, 1, '#b0a488'); P(9, 15, 4, 1, '#b0a488');
    P = painterAt(g, PROPS.check, 3);            // chunky checkmark
    P(8, 13, 2, 2, '#1a5c3a'); P(9, 14, 2, 2, '#2ecc71'); P(10, 15, 2, 2, '#2ecc71');
    P(11, 13, 2, 2, '#2ecc71'); P(12, 11, 2, 2, '#2ecc71'); P(13, 9, 2, 2, '#2ecc71');
    P = painterAt(g, PROPS.zz, 3);               // sleepy Zs
    P(9, 12, 4, 1, '#9aa5b1'); P(11, 13, 2, 1, '#9aa5b1'); P(10, 14, 2, 1, '#9aa5b1'); P(9, 15, 4, 1, '#9aa5b1');
    P(14, 8, 3, 1, '#9aa5b1'); P(15, 9, 1, 1, '#9aa5b1'); P(14, 10, 3, 1, '#9aa5b1');
    P = painterAt(g, PROPS.bang, 3);             // "!" notice
    P(11, 7, 2, 6, '#e2a33b'); P(11, 15, 2, 2, '#e2a33b');
    P = painterAt(g, PROPS.note, 3);             // honk note
    P(12, 8, 1, 6, '#e2a33b'); P(13, 8, 2, 1, '#e2a33b'); P(10, 13, 3, 2, '#e2a33b');
    P = painterAt(g, PROPS.pebble, 3);
    P(11, 19, 2, 2, '#8a8f98'); P(11, 19, 1, 1, '#b3b8c2');
    P = painterAt(g, PROPS.heart, 3);            // pixel heart (tap-reaction pop)
    P(9, 10, 3, 2, '#e2543b'); P(13, 10, 3, 2, '#e2543b');
    P(8, 12, 9, 2, '#e2543b'); P(9, 14, 7, 1, '#e2543b');
    P(10, 15, 5, 1, '#e2543b'); P(11, 16, 3, 1, '#e2543b');
    P(12, 17, 1, 1, '#a92735'); P(9, 11, 2, 1, '#f0867a');
  }

  let ATLAS = null;
  function buildAtlas() {
    if (ATLAS) return;
    const cv = document.createElement('canvas');
    cv.width = ATLAS_COLS * CW; cv.height = 4 * CH;
    const g = cv.getContext('2d');
    for (const anim in BODY) { META_B[anim] = []; META_H[anim] = []; }
    for (let c = 0; c < 3; c++) {
      for (const anim in BODY) {
        const base = BODY[anim][0], n = BODY[anim][1];
        for (let i = 0; i < n; i++) {
          const pose = poseFor(anim, i);
          paintBody(g, base + i, c, pose);
          if (c === 0) {                          // meta identical across chars
            META_B[anim][i] = pose.bob + (pose.squash ? 1 : 0);
            META_H[anim][i] = pose.headDrop || 0;
          }
        }
      }
      for (let v = 0; v < 4; v++) paintHead(g, HEADCOL + v, c, v);
    }
    paintCar(g, PROPS.car0, 0); paintCar(g, PROPS.car1, 1);
    paintProps(g);
    ATLAS = cv;
  }

  /* ── layout: the gutter rails, computed live from <main>'s content box.
   * Mirrored (both rails take the SMALLER gutter so they match), re-evaluated
   * on resize / main-resize / scroll — never per frame unless dirty.  The
   * bands double as the canvas CLIP region, which is what structurally
   * guarantees "never over content". ─────────────────────────────────────── */
  const bands = {
    mode: 'none',                                // 'rails' | 'strip' | 'none'
    n: 0,
    a: { x: 0, y: 0, w: 0, h: 0 },
    b: { x: 0, y: 0, w: 0, h: 0 },
    railW: 0,
    mobile: false,                               // narrow viewport OR coarse pointer
  };
  // the floating voice-mic dead zone (bottom-center): waypoints, the car and
  // bubbles never enter this rect.  Inert until evalBands computes it.
  const dead = { x0: -1e9, x1: -1e9, y0: 1e9 };
  let layoutDirty = true;
  let COARSE = false;                            // hover-none device -> strip + no hover
  const WP_FY = [0.2, 0.4, 0.6, 0.8, 0.92];      // weighted pause points…
  const WP_W = [1, 2, 3, 3, 2];                  // …favouring the lower half
  const WP_CUM = [];
  (function () { let s = 0; for (let i = 0; i < WP_W.length; i++) { s += WP_W[i]; WP_CUM[i] = s; } })();

  function evalBands() {
    layoutDirty = false;
    glowGradsDirty = true;                          // band geometry feeds the glow gradients
    const vw = window.innerWidth, vh = window.innerHeight;
    const sh = COARSE ? STRIP_H_COARSE : STRIP_H;
    dead.x0 = vw / 2 - DEAD_HALF; dead.x1 = vw / 2 + DEAD_HALF;
    dead.y0 = vh - sh - 64;                         // the mic floats just above the strip
    bands.mobile = (vw < VIEW_MIN || COARSE);
    const main = bands.mobile ? null : document.querySelector('main');
    const r = main ? main.getBoundingClientRect() : null;
    const lw = r ? Math.max(0, r.left) : 0;
    const rw = r ? Math.max(0, vw - r.right) : 0;
    const lRail = Math.floor(lw) - CONTENT_GAP;
    const rRail = Math.floor(rw) - CONTENT_GAP;
    const railW = Math.min(lRail, rRail);           // mirrored
    if (r && railW >= RAIL_MIN) {
      const top = clamp(r.top, 0, vh - 120);      // stay out of the header
      bands.mode = 'rails'; bands.n = 2; bands.railW = railW;
      bands.a.x = 0; bands.a.y = top; bands.a.w = railW; bands.a.h = vh - top;
      bands.b.x = vw - railW; bands.b.y = top; bands.b.w = railW; bands.b.h = vh - top;
    } else if (r && (lRail >= RAIL_MIN || rRail >= RAIL_MIN)) {
      // exactly one gutter is wide enough: single rail on that side
      const top = clamp(r.top, 0, vh - 120);
      const w = Math.max(lRail, rRail);
      bands.mode = 'rails'; bands.n = 1; bands.railW = w;
      bands.a.x = (lRail >= RAIL_MIN) ? 0 : vw - w;
      bands.a.y = top; bands.a.w = w; bands.a.h = vh - top;
    } else {
      // mobile / coarse pointer / both rails too narrow / no <main>: the
      // bottom strip — the very bottom of the viewport, where the page only
      // has padding.  (This replaces the old <1100px hard-off.)
      bands.mode = 'strip'; bands.n = 1; bands.railW = 0;
      bands.a.x = 0; bands.a.y = vh - sh; bands.a.w = vw; bands.a.h = sh;
    }
  }
  function stripX(fromX) {                          // strip waypoint OUTSIDE the mic dead zone
    const b = bands.a;
    const lo = b.x + 24, hi = b.x + b.w - 24;
    const lw = Math.max(0, (dead.x0 - 18) - lo);    // 18 ≈ sprite half-width margin
    const rw = Math.max(0, hi - (dead.x1 + 18));
    if (lw <= 0 && rw <= 0) return lo;              // absurdly narrow: hug the left edge
    let nx = (Math.random() * (lw + rw) < lw)
      ? lo + Math.random() * lw
      : (dead.x1 + 18) + Math.random() * rw;
    if (Math.abs(nx - fromX) < 60) {                // one re-roll away from the same spot
      nx = (Math.random() * (lw + rw) < lw)
        ? lo + Math.random() * lw
        : (dead.x1 + 18) + Math.random() * rw;
    }
    return nx;
  }
  function bandOf(s) { return (bands.n > 1 && s.bandIx === 1) ? bands.b : bands.a; }
  function pickWaypoint(s) {                      // weighted, never the same spot
    const b = bandOf(s);
    if (bands.mode === 'strip') {
      s.ty = b.y + b.h - 10;
      s.tx = stripX(s.x);                          // never parks in the mic dead zone
      return;
    }
    const roll = Math.random() * WP_CUM[WP_CUM.length - 1];
    let i = 0; while (WP_CUM[i] < roll) i++;
    if (i === s.wpIx) i = (i + 1) % WP_FY.length; // no jitter back to the same point
    s.wpIx = i;
    s.ty = b.y + 60 + WP_FY[i] * (b.h - 80);
    s.tx = b.x + 20 + Math.random() * Math.max(8, b.w - 44);
  }
  function clampToBand(s) {
    const b = bandOf(s);
    s.x = clamp(s.x, b.x + 16, b.x + b.w - 16);
    s.y = clamp(s.y, b.y + 56, b.y + b.h - 8);
    s.px = s.x; s.py = s.y;
  }

  /* ── the canvas: one fixed full-viewport element.  Created on resume,
   * destroyed on suspend.  Sized ONLY here (resize/DPR events) — contract (7):
   * the element itself never moves or resizes per frame. ─────────────────── */
  let cv = null, ctx = null, DPR = 1;
  let dprMq = null, armedDpr = 0;                 // the DPR the watcher was armed with
  let zSpritesSet = false;                        // did WE set the inline --z-sprites token?
  function armDprWatch() {                        // matchMedia('(resolution: …dppx)')
    if (dprMq) { try { dprMq.removeEventListener('change', onDprChange); } catch (e) {} }
    try {
      armedDpr = window.devicePixelRatio || 1;
      dprMq = window.matchMedia('(resolution: ' + window.devicePixelRatio + 'dppx)');
      dprMq.addEventListener('change', onDprChange);
    } catch (e) { dprMq = null; }
  }
  function onDprChange() { sizeCanvas(); armDprWatch(); }
  function ensureCanvas() {
    if (cv) return;
    // the z token exists so the page (or a future modal) can restack the layer
    try {
      const cur = getComputedStyle(document.documentElement).getPropertyValue('--z-sprites');
      if (!cur || !cur.trim()) {
        document.documentElement.style.setProperty('--z-sprites', '20');
        zSpritesSet = true;
      }
    } catch (e) {}
    cv = document.createElement('canvas');
    cv.setAttribute('aria-hidden', 'true');       // contract (4): invisible to a11y
    cv.setAttribute('role', 'presentation');
    cv.setAttribute('tabindex', '-1');
    // NO 100vw/100vh here — the element is sized in px by sizeCanvas from the
    // same innerWidth/innerHeight as the buffer (iOS Safari's collapsing
    // toolbar makes 100vh drift from innerHeight and the strip would smear)
    cv.style.cssText = 'position:fixed;left:0;top:0;' +
      'pointer-events:none;z-index:var(--z-sprites,20);display:block;';
    if (tetris.active) cv.style.zIndex = '23';    // canvas rebuilt mid-game: stay over the panel
    document.body.appendChild(cv);
    ctx = cv.getContext('2d');
    sizeCanvas();
    armDprWatch();
  }
  function sizeCanvas() {
    if (!cv) return;
    DPR = window.devicePixelRatio || 1;
    if (dprMq && DPR !== armedDpr) armDprWatch();  // DPR moved without a change event: re-arm
    const w = window.innerWidth, h = window.innerHeight;
    cv.style.width = w + 'px';                     // element px == buffer px source —
    cv.style.height = h + 'px';                    //   never drifts under Safari's toolbar
    cv.width = Math.max(1, Math.round(w * DPR));
    cv.height = Math.max(1, Math.round(h * DPR));
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.imageSmoothingEnabled = false;            // crisp pixels, always
  }
  function destroyCanvas() {
    if (dprMq) { try { dprMq.removeEventListener('change', onDprChange); } catch (e) {} dprMq = null; }
    if (zSpritesSet) {                             // only remove what WE set
      try { document.documentElement.style.removeProperty('--z-sprites'); } catch (e) {}
      zSpritesSet = false;
    }
    if (cv && cv.parentNode) cv.parentNode.removeChild(cv);
    cv = null; ctx = null;
  }
  const snap = (v) => Math.round(v * DPR) / DPR;  // sub-pixel physics, whole-device-pixel draws

  /* ── particle pool — P_MAX objects preallocated at boot; update()/render()
   * never allocate (no literals, no closures — index loops only). ─────────── */
  const K_CONF = 0, K_ZZ = 1, K_CHECK = 2, K_NOTE = 3, K_PEBBLE = 4,
        K_HEART = 5, K_BOOM = 6;
  const BOOMC = ['#ffd964', '#ff9b3d', '#e2543b', '#8a8f98', '#4a4f5d'];
  const pool = new Array(P_MAX);
  const freeList = new Int16Array(P_MAX);
  let freeTop = 0, liveParts = 0;
  (function () {
    for (let i = 0; i < P_MAX; i++) {
      pool[i] = { on: false, kind: 0, x: 0, y: 0, px: 0, py: 0, vx: 0, vy: 0,
                  life: 0, ttl: 1, size: 2, ci: 0, ang: 0, spin: 0 };
      freeList[i] = i;
    }
    freeTop = P_MAX;
  })();
  function emit(kind, x, y, vx, vy, ttl, size, ci, spin) {
    if (freeTop <= 0) return;
    const p = pool[freeList[--freeTop]];
    p.on = true; p.kind = kind; p.x = x; p.y = y; p.px = x; p.py = y; p.vx = vx; p.vy = vy;
    p.life = 0; p.ttl = ttl; p.size = size; p.ci = ci; p.ang = rand(0, 6.28); p.spin = spin;
    liveParts++;
  }
  function emitConfetti(x, y, n) {
    n = Math.min(n, freeTop);
    for (let i = 0; i < n; i++)
      emit(K_CONF, x + rand(-8, 8), y + rand(-6, 6), rand(-95, 95), rand(-270, -110),
           rand(900, 1600), 2 + randi(2), randi(7), rand(4, 10));
  }
  function emitBoom(x, y) {                        // chunky 💥-style radial pixel burst
    const n = Math.min((22 * TIERS[tier].pmul) | 0, freeTop);
    for (let i = 0; i < n; i++) {
      const a = rand(0, 6.28), v = rand(60, 240);
      emit(K_BOOM, x, y, Math.cos(a) * v, Math.sin(a) * v - 40,
           rand(350, 750), 3 + randi(3), randi(5), 0);
    }
  }
  function updateParticles(dt) {
    const dts = dt / 1000;
    for (let i = 0; i < P_MAX; i++) {
      const p = pool[i];
      if (!p.on) continue;
      p.px = p.x; p.py = p.y;                       // previous position for render lerp
      p.life += dt;
      if (p.life >= p.ttl) { p.on = false; freeList[freeTop++] = i; liveParts--; continue; }
      if (p.kind === K_CONF || p.kind === K_PEBBLE) {
        p.vy += 340 * dts; p.vx *= (1 - 0.85 * dts);
      } else if (p.kind === K_BOOM) {
        p.vy += 210 * dts; p.vx *= (1 - 1.6 * dts);
      } else if (p.kind === K_HEART) {
        p.vy = -34; p.vx = Math.sin((p.life + p.ang * 300) / 200) * 10;
      } else if (p.kind === K_ZZ) {
        p.vy = -22; p.vx = Math.sin((p.life + p.ang * 300) / 260) * 14;
      } else if (p.kind === K_NOTE) {
        p.vy -= 60 * dts;
      } else if (p.kind === K_CHECK) {
        p.vy *= (1 - 3 * dts);
      }
      p.x += p.vx * dts; p.y += p.vy * dts;
    }
  }
  function renderParticles(g, alpha) {
    for (let i = 0; i < P_MAX; i++) {
      const p = pool[i];
      if (!p.on) continue;
      const t = p.life / p.ttl;
      const a = t > 0.7 ? (1 - t) / 0.3 : 1;
      const ix = lerp(p.px, p.x, alpha);            // interpolated like sprites
      const iy = lerp(p.py, p.y, alpha);
      g.globalAlpha = a;
      if (p.kind === K_CONF) {
        g.fillStyle = CONF[p.ci];
        const w = 1 + ((p.size * Math.abs(Math.cos(p.ang + p.life * p.spin / 1000))) | 0);
        g.fillRect(snap(ix), snap(iy), w, p.size);
      } else if (p.kind === K_BOOM) {              // chunky explosion pixels
        g.fillStyle = BOOMC[p.ci];
        g.fillRect(snap(ix), snap(iy), p.size, p.size);
      } else {
        // bitmap particles reuse the props row — no shapes built per frame
        let col = PROPS.zz;
        if (p.kind === K_CHECK) col = PROPS.check;
        else if (p.kind === K_NOTE) col = PROPS.note;
        else if (p.kind === K_PEBBLE) col = PROPS.pebble;
        else if (p.kind === K_HEART) col = PROPS.heart;
        const sc = p.kind === K_CHECK ? 3 : 2;    // integer scales only
        g.drawImage(ATLAS, col * CW, 3 * CH, CW, CH,
          snap(ix) - 12 * sc, snap(iy) - 12 * sc, CW * sc, CH * sc);
      }
    }
    g.globalAlpha = 1;
  }

  /* ── secondary-motion springs (hoodie strings, robot antenna) —
   * critically-damped-ish: k≈180, d≈20, m=1, fed by the sprite's motion. ──── */
  function stepSpring(sp, tx, ty, dts) {
    sp.vx += (180 * (tx - sp.x) - 20 * sp.vx) * dts;
    sp.vy += (180 * (ty - sp.y) - 20 * sp.vy) * dts;
    sp.x += sp.vx * dts; sp.y += sp.vy * dts;
  }

  /* ── chat bubbles — canvas-drawn pixel speech bubbles.  A pool of TWO
   * (manager rule: max 2 on screen); spawn allocates nothing but one
   * measureText, update/draw allocate nothing.  Text is drawn via fillText so
   * markup is inert; say() additionally strips control chars.  Bubbles are a
   * sanctioned overlay: they render OUTSIDE the band clip but are clamped
   * fully on-viewport, never inside the mic dead zone (widened while the voice
   * UI is up), and never over the tetris HUD band — the panel CARD's top down
   * to the board top, measured, not guessed. ─────────────────────────────── */
  const LINES = Object.assign(Object.create(null), {   // data, easy to extend
    'lead.created':      ['ouu new lead ✨', 'fresh one!', "who's taking it?"],
    'deal.won':          ['cha-ching!', 'receipt secured 🧾', 'money in!'],
    'driver.on_the_way': ['drive safe!', 'vroom vroom'],
    'stage.advanced':    ['moving up!', 'nice progress'],
    'goal.hit':          ['BEST JOB EVER', 'LEGENDS!'],
    'task.completed':    ['sent ✉', 'done and dusted'],
    idle:  ['this is the best job ever', 'best job ever, no cap', '☕ break soon?',
            "taggin' and braggin'", 'nice board today'],
    dodge: ['whoa!', 'hey, watch it'],
    tap:   ['hey!', ':D', 'careful!'],
    invite:   ['wanna play tetris? 🎮'],
    clear:    ['nice!'],
    over:     ['gg... rematch?'],
    boom:     ['boom 💥'],
    mischief: ['hehe >:)'],
  });
  const lastLineIx = Object.create(null);
  function pickLine(cat) {                         // random, never the same twice running
    const arr = LINES[cat];
    if (!arr || !arr.length) return '';
    if (arr.length === 1) return arr[0];
    let i = randi(arr.length);
    if (i === lastLineIx[cat]) i = (i + 1) % arr.length;
    lastLineIx[cat] = i;
    return arr[i];
  }
  const BUB_FONT = '600 20px ui-monospace,SFMono-Regular,Menlo,monospace'; // 10px ×2 = crisp
  const BUB_H = 34, BUB_TAIL = 8, BUB_PAD = 10;
  const bubbles = [
    { on: false, s: null, text: '', w: 0, life: 0, ttl: 0, ax: 0, ay: 0 },
    { on: false, s: null, text: '', w: 0, life: 0, ttl: 0, ax: 0, ay: 0 },
  ];
  function rectsHit(x, y, w, h, rx0, ry0, rx1, ry1) {
    return x < rx1 && x + w > rx0 && y < ry1 && y + h > ry0;
  }
  function bubbleAnchor(b) {                       // follows its sprite while it lives
    const s = b.s;
    if (s && !s.dead) { b.ax = s.x; b.ay = s.y + s.jumpY - 52; }
  }
  // voice-UI courtesy: while the page's floating voice control is listening /
  // thinking / speaking it grows, so BUBBLES (only) treat a wider mic dead
  // zone — centerX ± min(170, innerWidth/2 - 16).  Sprite waypoints, the car
  // and the invite chip keep the plain DEAD_HALF zone.  Sampled at spawn time.
  let bubDeadPad = 0;                              // extra half-width, 0 when idle
  function refreshBubbleDead() {
    bubDeadPad = 0;
    try {
      const vc = document.querySelector('.vc-root');
      if (vc && vc.classList &&
          (vc.classList.contains('listening') || vc.classList.contains('thinking') ||
           vc.classList.contains('speaking'))) {
        const half = Math.min(170, window.innerWidth / 2 - 16);
        bubDeadPad = Math.max(0, half - DEAD_HALF);
      }
    } catch (e) { bubDeadPad = 0; }
  }
  // WHERE A BUBBLE ACTUALLY LANDS: viewport clamp, then the mic-dead-zone and
  // tetris-HUD displacements.  ONE helper, used by both the spawn-time overlap
  // test and drawBubbles — measuring the pre-displacement rect in one place and
  // the post-displacement rect in the other is how two bubbles ended up
  // overlapping.  Returns a REUSED object: read it before calling again.
  const bubPos = { x: 0, y: 0 };
  function bubbleRect(ax, ay, w) {
    const vw = window.innerWidth, vh = window.innerHeight;
    const h = BUB_H + BUB_TAIL;
    let x = clamp(ax - w / 2, 4, Math.max(4, vw - w - 4));
    let y = clamp(ay - BUB_TAIL - BUB_H, 4, Math.max(4, vh - h - 4));
    const dx0 = dead.x0 - bubDeadPad, dx1 = dead.x1 + bubDeadPad;
    if (rectsHit(x, y, w, h, dx0, dead.y0, dx1, 1e9)) {     // never over the mic
      x = (ax < (dx0 + dx1) / 2) ? dx0 - w - 4 : dx1 + 4;
      x = clamp(x, 4, Math.max(4, vw - w - 4));
    }
    // never over the tetris HUD band: the panel card's top down to the board
    // top (score / lines / level / NEXT live in there, and their height is set
    // by the theme's fonts — it was never a fixed 34px header)
    if (tetris.active && tetris.pw > 0 && tetris.py > tetris.panelTop &&
        rectsHit(x, y, w, h, tetris.px, tetris.panelTop, tetris.px + tetris.pw, tetris.py))
      y = tetris.py + 2;                           // drop onto the board itself
    bubPos.x = x; bubPos.y = y;
    return bubPos;
  }
  function bubbleSay(s, text) {
    if (!live || !ctx || !s || s.dead || !text) return false;
    let slot = -1;
    for (let i = 0; i < 2; i++) {
      if (bubbles[i].on && bubbles[i].s === s) return false;   // one per sprite
      if (!bubbles[i].on && slot < 0) slot = i;
    }
    if (slot < 0) return false;                    // manager rule: max 2 — skip
    const b = bubbles[slot];
    ctx.font = BUB_FONT;
    const w = Math.min(Math.ceil(ctx.measureText(text).width) + BUB_PAD * 2,
                       window.innerWidth - 8);
    b.s = s; bubbleAnchor(b);
    refreshBubbleDead();                           // one DOM read per spawn, not per frame
    // overlap rule: would it intersect the other live bubble WHERE EACH ONE
    // ACTUALLY DRAWS (after dead-zone / HUD displacement)?  then skip
    const o = bubbles[1 - slot];
    if (o.on) {
      const mine = bubbleRect(b.ax, b.ay, w);
      const nx = mine.x, ny = mine.y;               // copy: bubbleRect reuses its object
      bubbleAnchor(o);
      const theirs = bubbleRect(o.ax, o.ay, o.w);
      if (rectsHit(nx, ny, w, BUB_H + BUB_TAIL,
                   theirs.x, theirs.y, theirs.x + o.w, theirs.y + BUB_H + BUB_TAIL)) {
        b.s = null; return false;
      }
    }
    b.on = true; b.text = text; b.w = w;
    b.life = 0;
    b.ttl = clamp(2200 + text.length * 40, 2200, 3500);   // life scales with length
    return true;
  }
  function bubbleUpdate(dt) {
    for (let i = 0; i < 2; i++) {
      const b = bubbles[i];
      if (!b.on) continue;
      b.life += dt;
      if (b.s && b.s.dead) b.life = Math.max(b.life, b.ttl - 260);  // owner left: fade now
      if (b.life >= b.ttl) { b.on = false; b.s = null; }
    }
  }
  function clearBubbles() {
    for (let i = 0; i < 2; i++) { bubbles[i].on = false; bubbles[i].s = null; }
  }
  function drawPixelBubble(g, x, y, w, h, tailX) { // rounded pixel rect + pixel tail
    g.fillStyle = THEME.ink;                       // 2px outline, notched corners
    g.fillRect(x + 2, y, w - 4, h);
    g.fillRect(x, y + 2, w, h - 4);
    g.fillRect(x + 1, y + 1, 1, 1); g.fillRect(x + w - 2, y + 1, 1, 1);
    g.fillRect(x + 1, y + h - 2, 1, 1); g.fillRect(x + w - 2, y + h - 2, 1, 1);
    g.fillRect(tailX - 4, y + h, 8, 2);            // 4px-scale stepped tail
    g.fillRect(tailX - 2, y + h + 2, 4, 2);
    g.fillStyle = THEME.bub;
    g.fillRect(x + 2, y + 2, w - 4, h - 4);
    g.fillRect(tailX - 3, y + h - 2, 6, 2);
    g.fillRect(tailX - 1, y + h, 2, 2);
  }
  function drawBubbles(g) {
    if (!bubbles[0].on && !bubbles[1].on) return;
    g.font = BUB_FONT; g.textAlign = 'center'; g.textBaseline = 'middle';
    for (let i = 0; i < 2; i++) {
      const b = bubbles[i];
      if (!b.on) continue;
      bubbleAnchor(b);
      // the SAME placement the spawn-time overlap test used: viewport clamp,
      // mic dead zone, tetris HUD band
      const r = bubbleRect(b.ax, b.ay, b.w);
      const x = r.x, y = r.y;
      const tailX = clamp(Math.round(b.ax), x + 8, x + b.w - 8);
      const sc = REDUCED ? 1 : easePop(Math.min(1, b.life / 160));   // pop-in overshoot
      const a = b.life > b.ttl - 260 ? (b.ttl - b.life) / 260 : Math.min(1, b.life / 120);
      const cx2 = Math.round(x + b.w / 2), cy2 = Math.round(y + BUB_H / 2);
      g.save();
      g.globalAlpha = clamp(a, 0, 1);
      g.translate(cx2, cy2); g.scale(sc, sc); g.translate(-cx2, -cy2);
      drawPixelBubble(g, Math.round(x), Math.round(y), b.w, BUB_H, tailX);
      g.fillStyle = THEME.ink;
      g.fillText(b.text, cx2, cy2 + 1);
      g.restore();
    }
    g.globalAlpha = 1;
  }

  /* ── cursor awareness — position, stillness, and smoothed speed (px/ms;
   * pointermove covers mouse AND touch drags, so swipes can trigger dodges) ── */
  const mouse = { x: -9999, y: -9999, movedAt: 0, speed: 0, dirX: 0, dirY: 0, at: 0 };
  function onMouse(e) {
    const now = performance.now();
    const dx = e.clientX - mouse.x, dy = e.clientY - mouse.y;
    if (dx * dx + dy * dy > 9) mouse.movedAt = now;
    const dtm = now - mouse.at;
    if (dtm > 0 && dtm < 120) {
      const sp = Math.sqrt(dx * dx + dy * dy) / dtm;
      mouse.speed += (sp - mouse.speed) * 0.5;     // light smoothing
      if (sp > 0.02) { mouse.dirX = dx; mouse.dirY = dy; }
    } else mouse.speed = 0;                        // stale gap: not a lunge
    mouse.at = now;
    mouse.x = e.clientX; mouse.y = e.clientY;
  }

  /* ── tap / click play — a PASSIVE window pointerdown hit-tests sprite
   * bboxes (+10px pad).  NEVER preventDefault: the canvas is pointer-events
   * none and this listener is passive, so the event reaches the page
   * untouched either way. ────────────────────────────────────────────────── */
  function onPointerDown(e) {
    if (!live) return;
    const px = e.clientX, py = e.clientY;
    for (let i = 0; i < sprites.length; i++) {
      const s = sprites[i];
      if (s.dead || s.retiring || s.alpha < 0.5) continue;
      const sy = s.y + s.jumpY;
      // char box is ~±21 × 45 tall at ×3 — pad by 10 on every side
      if (px >= s.x - 31 && px <= s.x + 31 && py >= sy - 55 && py <= sy + 10) {
        tapSprite(s, px);
        break;
      }
    }
  }
  function tapSprite(s, px) {
    if (simNow < s.tapCd) return;                  // 2s per-sprite cooldown
    const k = s.perf.kind;
    if (s.state === 'celebrate' ||
        (s.state === 'react' && (k === 'dyn' || k === 'dynplant' || k === 'scurry' || k === 'invite')))
      return;                                      // don't derail set pieces
    s.tapCd = simNow + TAP_CD;
    const talk = Math.random() < 0.45;             // MAY answer with a bubble
    if (REDUCED) { if (talk) bubbleSay(s, pickLine('tap')); return; }
    const r = randi(4);
    if (r === 0) {                                 // startled hop, squash first
      s.extraSY = 0.8;
      beginPerf(s, 'cheer', s.x, '', 0);
      s.jumpV = -200; s.landed = false;
    } else if (r === 1) {                          // quick spin
      beginPerf(s, 'spin', s.x, '', 0);
    } else if (r === 2) {                          // wave at the finger
      const want = px >= s.x ? 1 : -1;
      if (want !== s.facing) beginTurn(s, want, 'wave'); else beginState(s, 'wave');
    } else {                                       // heart pop
      emit(K_HEART, s.x, s.y + s.jumpY - 62, 0, -34, 1400, 2, 0, 0);
      s.happyUntil = simNow + 1200;
      s.extraSY = 1.12;
    }
    if (talk) bubbleSay(s, pickLine('tap'));
  }

  /* ── shared runtime state ───────────────────────────────────────────────── */
  let MODE = 'off';               // 'off' | 'subtle' | 'full'
  let running = false;            // mode is on (listeners attached)
  let live = false;               // canvas + RAF actually going
  let REDUCED = false;            // prefers-reduced-motion: static poses only
  let simNow = 0;                 // simulation clock — advances by STEP only
  let tier = 0;                   // index into TIERS
  const sprites = [];
  let spriteSeq = 0, nextChar = randi(3), nextSpawnAt = 0;
  let attUntil = 0;               // global attention budget: ONE big action at a time
  let reactUntil = 0;             // reactions never interrupt each other
  let lastBigAt = -1e9;
  let lastEventAt = 0;            // wakes sleepers
  let glowRail = 0, glowScreen = 0;
  const goal = { phase: 0, at: 0, cx: 0, cy: 0 };
  const flags = [ { on: false, x: 0, y: 0, t0: 0, label: '' },
                  { on: false, x: 0, y: 0, t0: 0, label: '' } ];
  const car = { on: false, x: 0, y: 0, px: 0, py: 0, vx: 0, dir: 1, phase: 0, t: 0,
                bandIx: 0, honked: false, wheelT: 0, bounce: 0, stopX: 0 };

  function tryAttention(dur) {
    if (simNow < attUntil) return false;
    attUntil = simNow + dur; return true;
  }

  /* ── sprites ────────────────────────────────────────────────────────────── */
  function newSprite(char) {
    return {
      id: spriteSeq++, char: char, bandIx: 0, wpIx: -1,
      x: 0, y: 0, px: 0, py: 0,
      facing: 1, sx: 1, alpha: 0,
      state: 'enter', stateT: 0,
      anim: 'idle', frame: 0, animT: 0, animRate: 1,
      tx: 0, ty: 0, walkRamp: 0,
      jumpY: 0, jumpV: 0, landed: false,
      lean: 0, extraSY: 1,
      look: 0, lookY: 0,
      headHist: new Float32Array(16), hh: 0, headDrop: 0,
      blinkAt: rand(2000, 6000), blinkT: 0, doubleLeft: 0,
      walkAt: 0, breakAt: rand(6000, 14000), lastBreak: '', breakKind: '',
      sleepAt: rand(60000, 120000), zzAt: 0,
      waveCooldownUntil: 0, happyUntil: 0, cheerAt: -1e9,
      prop: '', propT: 0,
      turnFrom: 1, turnTo: 1, afterTurn: 'idle',
      celPhase: 0, phaseT: 0,
      perf: { kind: '', t: 0, tx: 0, reps: 1, intensity: 1, delay: 0, label: '' },
      // personality vector — nothing in lockstep: per-sprite speed, idle-break
      // bias, expressiveness, preferred rail, randomized phase offsets
      pers: { speed: rand(0.85, 1.15), breakBias: rand(0.6, 1.6),
              expr: rand(0.5, 1.5), rail: randi(2) },
      s1: { x: 0, y: 0, vx: 0, vy: 0 },           // hoodie string A / antenna
      s2: { x: 0, y: 0, vx: 0, vy: 0 },           // hoodie string B
      springInit: false,
      retiring: false, dead: false,
      fadeMix: 0, poseSwapAt: 0,                  // reduced-motion cross-fades
      tapCd: 0, dodgeCd: 0, dodgeDir: 1,          // pointer-play cooldowns
      quipAt: rand(IDLE_QUIP_MIN, IDLE_QUIP_MAX), // idle chat-bubble timer
      duty: 0, col: 0, colH: -1, colAt: 0,        // tetris rooftop duty
    };
  }
  function setAnim(s, name, rate, frame) {
    if (s.anim !== name || s.frame !== frame) { s.anim = name; s.frame = frame; s.animT = 0; }
    s.animRate = rate;
  }
  function spawnSprite(withScroll) {
    const c = nextChar; nextChar = (nextChar + 1) % 3;
    const s = newSprite(c);
    s.bandIx = bands.n > 1 ? s.pers.rail : 0;
    const b = bandOf(s);
    pickWaypoint(s);
    if (bands.mode === 'strip') {
      const fromLeft = Math.random() < 0.5;
      s.x = fromLeft ? b.x - 30 : b.x + b.w + 30;
      s.y = s.ty; s.facing = s.sx = fromLeft ? 1 : -1;
    } else {
      const left = (b.x === 0);                   // band side, not band index (single-rail safe)
      s.x = left ? -30 : b.x + b.w + 30;          // walk in from the screen edge
      s.y = s.ty; s.facing = s.sx = left ? 1 : -1;
    }
    if (REDUCED) { s.x = s.tx; s.y = s.ty; }      // reduced motion: no offscreen enter-walk
    s.px = s.x; s.py = s.y;
    s.blinkAt += simNow; s.breakAt += simNow; s.sleepAt += simNow; s.quipAt += simNow;
    beginState(s, 'enter');
    if (withScroll) { s.prop = 'scroll'; s.propT = 0; }
    sprites.push(s);
    return s;
  }
  function pickPerformer() {
    let best = null;
    for (let i = 0; i < sprites.length; i++) {
      const s = sprites[i];
      if (s.retiring || s.dead || s.duty) continue;   // rooftop crew is busy
      if (s.state === 'idle' || s.state === 'walk' || s.state === 'idleBreak' || s.state === 'sleep') return s;
      if (!best) best = s;
    }
    return best;
  }

  function beginState(s, name) {
    s.state = name; s.stateT = 0;
    switch (name) {
      case 'enter': setAnim(s, 'walk', 1, 0); s.walkRamp = 0.4; break;
      case 'idle':
        setAnim(s, 'idle', 1, randi(2));          // random phase — no lockstep
        s.animT = rand(0, 400);
        s.walkAt = simNow + rand(4000, 11000);
        break;
      case 'walk': setAnim(s, 'walk', 1, 0); s.walkRamp = 0; break;
      case 'turn': setAnim(s, 'turn', 0, 0); break;
      case 'notice':
        setAnim(s, 'notice', 0, 0);
        s.prop = 'bang'; s.propT = 0;
        s.extraSY = 0.94;                          // tiny anticipation dip
        break;
      case 'wave': setAnim(s, 'wave', 1, 0); break;
      case 'celebrate': setAnim(s, 'celebrate', 0, 0); s.celPhase = 0; s.phaseT = 0; break;
      case 'idleBreak': /* anim chosen by pickBreak */ break;
      case 'sleep': setAnim(s, 'sleep', 1, 0); s.zzAt = simNow + 900; break;
      case 'react': break;
      case 'exit': {
        setAnim(s, 'walk', 1, 0); s.retiring = true; s.walkRamp = 0;
        const b = bandOf(s);
        s.tx = (s.x - b.x < b.w / 2) ? b.x - 60 : b.x + b.w + 60;
        s.ty = s.y;
        break;
      }
      case 'slump': setAnim(s, 'slump', 1, 0); break;
      case 'tetris': setAnim(s, 'idle', 1, 0); s.colH = -1; break;
    }
  }
  function beginTurn(s, dir, after) {
    if (s.state === 'turn') { s.afterTurn = after; return; }
    s.turnFrom = s.facing; s.turnTo = dir; s.afterTurn = after;
    beginState(s, 'turn');
  }
  function beginCelebrate(s, reps, intensity) {
    s.perf.reps = reps; s.perf.intensity = intensity || 1;
    beginState(s, 'celebrate');
  }
  function beginPerf(s, kind, tx, label, delay) {
    s.perf.kind = kind; s.perf.t = 0; s.perf.tx = tx || s.x;
    s.perf.label = label || ''; s.perf.delay = delay || 0;
    beginState(s, 'react');
    if (kind === 'flagrun') { s.prop = 'flag'; s.propT = 0; setAnim(s, 'walk', 1.5, 0); s.walkRamp = 0; }
    else if (kind === 'scroll') { s.prop = 'scroll'; s.propT = 0; setAnim(s, 'idle', 1, 0); }
    else if (kind === 'cheer') { setAnim(s, 'celebrate', 0, 2); s.jumpV = -130; s.landed = false; }
    else if (kind === 'hop') { setAnim(s, 'celebrate', 0, 1); s.jumpV = -180; s.landed = false; s.prop = 'check'; s.propT = 0; }
    else if (kind === 'delayjump') { setAnim(s, 'idle', 1, 0); }
    else if (kind === 'dodge') { setAnim(s, 'idle', 0, 0); s.perf.delay = 0; }
    else if (kind === 'spin') { setAnim(s, 'idle', 0, 0); }
    else if (kind === 'invite') { setAnim(s, 'turn', 0, 1); }   // face the viewer
    else if (kind === 'dyn') { setAnim(s, 'walk', 1.6, 0); s.walkRamp = 0; }
  }
  function endPerf(s) {
    s.perf.kind = ''; s.prop = ''; s.animRate = 1;
    beginState(s, 'idle');
  }

  // weighted idle-breaks; never the same one twice in a row
  const BREAKS = ['stretch', 'phone', 'scratch', 'look', 'pebble'];
  const BREAK_W = [3, 3, 2, 3, 1];
  function pickBreak(s) {
    let total = 0;
    for (let i = 0; i < BREAKS.length; i++) if (BREAKS[i] !== s.lastBreak) total += BREAK_W[i];
    let roll = Math.random() * total, kind = 'stretch';
    for (let i = 0; i < BREAKS.length; i++) {
      if (BREAKS[i] === s.lastBreak) continue;
      roll -= BREAK_W[i];
      if (roll <= 0) { kind = BREAKS[i]; break; }
    }
    s.lastBreak = kind; s.breakKind = kind;
    if (kind === 'look') setAnim(s, 'notice', 0, 0);
    else if (kind === 'pebble') setAnim(s, 'walk', 0, 2);
    else setAnim(s, kind, 1, 0);
    beginState(s, 'idleBreak');
    if (kind === 'pebble') setAnim(s, 'walk', 0, 2);   // beginState reset it
    else if (kind === 'look') setAnim(s, 'notice', 0, 0);
    else setAnim(s, kind, 1, 0);
  }

  // walk integration: easeInOutCubic ramp in, distance-based ease out, linear
  // ONLY in the constant-velocity middle; vertical drift capped to a slope so
  // rail moves read as walking, not floating
  function moveToward(s, dt, speedMul) {
    const sp = WALK_SPEED * s.pers.speed * speedMul;
    s.walkRamp = Math.min(1, s.walkRamp + dt / 300);
    const dx = s.tx - s.x, ad = Math.abs(dx);
    const dec = clamp(ad / 50, 0, 1);
    const v = sp * easeInOutCubic(s.walkRamp) * (0.25 + 0.75 * dec);
    const dir = dx < 0 ? -1 : 1;
    if (dir !== s.facing && ad > 4 && !s.retiring) { beginTurn(s, dir, s.state); return false; }
    s.x += dir * v * dt / 1000;
    if (s.retiring && dir !== s.facing) { s.facing = s.sx = dir; }
    const dy = s.ty - s.y;
    const cap = v * 0.35 * dt / 1000;
    s.y += clamp(dy, -cap, cap);
    s.animRate = clamp(v / WALK_SPEED, 0.5, 1.8);  // footfalls track actual speed
    return (ad < 2.5 && Math.abs(dy) < 2.5);
  }

  function cursorLogic(s, dt) {
    if (REDUCED) return;
    const hx = s.x, hy = s.y + s.jumpY - 50;
    const dx = mouse.x - hx, dy = mouse.y - hy;
    const d2 = dx * dx + dy * dy;
    const engaged = (s.state === 'idle' || s.state === 'walk' || s.state === 'idleBreak');
    // FAST approach (flick or swipe) toward the sprite -> DODGE: anticipation
    // lean, then a sidestep hop away from the pointer vector.  3s cooldown.
    if (engaged && mouse.speed > DODGE_SPEED && simNow >= s.dodgeCd &&
        performance.now() - mouse.movedAt < 90 && d2 < 70 * 70 &&
        (mouse.dirX * -dx + mouse.dirY * -dy) > 0) {   // pointer motion points AT the sprite
      s.dodgeCd = simNow + DODGE_CD;
      s.dodgeDir = (hx - mouse.x) >= 0 ? 1 : -1;       // hop away from the pointer
      beginPerf(s, 'dodge', s.x, '', 0);
      if (Math.random() < 0.4) bubbleSay(s, pickLine('dodge'));
      return;
    }
    if (COARSE) return;   // touch devices have no hover — tap covers interaction
    if (d2 < ATT_R2 && engaged) {
      // head tracks the cursor while the body keeps doing what it was doing
      const k = Math.min(1, dt / 140);
      s.look += (clamp(dx / 90, -1, 1) * 2 - s.look) * k;
      s.lookY += (clamp(dy / 120, -1, 1) - s.lookY) * k;
      const still = performance.now() - mouse.movedAt;
      if (still > 600 && simNow >= s.waveCooldownUntil &&
          simNow >= reactUntil && d2 < ATT_R2 * 0.7 && tryAttention(2000)) {
        s.waveCooldownUntil = simNow + WAVE_COOLDOWN;
        const want = dx >= 0 ? 1 : -1;
        if (want !== s.facing) beginTurn(s, want, 'notice');
        else beginState(s, 'notice');
      }
    } else {
      const k = Math.min(1, dt / 300);
      s.look -= s.look * k; s.lookY -= s.lookY * k;
    }
  }

  function updateSprite(s, dt) {
    s.px = s.x; s.py = s.y;
    const dts = dt / 1000;

    if (REDUCED) {                                 // static poses, slow cross-fades
      s.alpha = Math.min(1, s.alpha + dt / 900);
      if (simNow >= s.poseSwapAt) {
        s.poseSwapAt = simNow + rand(5000, 9000);
        s.frame = (s.frame + 1) % 2; setAnim(s, 'idle', 0, s.frame);
        s.fadeMix = 1;                             // rendered as a soft blink of pose
      }
      s.fadeMix = Math.max(0, s.fadeMix - dt / 1200);
      s.headHist[s.hh] = META_B.idle[s.frame]; s.hh = (s.hh + 1) & 15;
      s.headDrop = 0;
      return;
    }

    // procedural jump arc (celebrate / hop / cheer) — arcs, not lines
    if (s.jumpV !== 0 || s.jumpY !== 0) {
      s.jumpV += GRAV * dts;
      s.jumpY += s.jumpV * dts;
      if (s.jumpY >= 0) { s.jumpY = 0; s.jumpV = 0; s.landed = true; }
    }
    if (!s.retiring) s.alpha = Math.min(1, s.alpha + dt / 400);

    // blink: 2-6s randomized, occasional double-blink
    if (s.blinkT > 0) s.blinkT -= dt;
    else if (simNow >= s.blinkAt && s.state !== 'sleep') {
      s.blinkT = 120;
      if (s.doubleLeft > 0) { s.doubleLeft--; s.blinkAt = simNow + 170; }
      else {
        if (Math.random() < 0.18) { s.doubleLeft = 1; s.blinkAt = simNow + 170; }
        else s.blinkAt = simNow + rand(2000, 6000);
      }
    }
    cursorLogic(s, dt);
    // idle chat quips: every 45-120s per sprite, gated so they stay rare
    if (simNow >= s.quipAt) {
      s.quipAt = simNow + rand(IDLE_QUIP_MIN, IDLE_QUIP_MAX);
      if ((s.state === 'idle' || s.state === 'walk' || s.state === 'idleBreak') &&
          Math.random() < 0.65)
        bubbleSay(s, pickLine('idle'));
    }
    s.stateT += dt;

    switch (s.state) {
      case 'enter': {
        // easeOutExpo entrance: brisk arrival that settles
        const boost = 1 + 0.6 * (1 - easeOutExpo(Math.min(1, s.stateT / 900)));
        if (moveToward(s, dt, boost)) {
          beginState(s, 'idle');
          if (s.prop === 'scroll') { s.perf.kind = 'scroll'; s.perf.t = 0; beginState(s, 'react'); setAnim(s, 'idle', 1, 0); }
        }
        break;
      }
      case 'idle': {
        if (s.duty) { beginState(s, 'tetris'); break; }   // rooftop crew bounce back
        if (simNow >= s.breakAt) {
          pickBreak(s);
          s.breakAt = simNow + rand(6000, 14000) / s.pers.breakBias;
        } else if (simNow >= s.walkAt) {
          pickWaypoint(s);
          const dir = (s.tx >= s.x) ? 1 : -1;
          if (dir !== s.facing) beginTurn(s, dir, 'walk'); else beginState(s, 'walk');
        } else if (simNow >= s.sleepAt && simNow - lastEventAt > 45000) {
          beginState(s, 'sleep');
        }
        break;
      }
      case 'walk':
        if (moveToward(s, dt, 1)) {
          beginState(s, 'idle');
          if (Math.random() < 0.25 * s.pers.expr) s.breakAt = simNow + rand(800, 2500);
        }
        break;
      case 'turn': {
        // scaleX through 0 over ~140ms with a lean — never an instant flip
        const t = Math.min(1, s.stateT / 140);
        s.sx = lerp(s.turnFrom, s.turnTo, easeInOutQuad(t));
        s.lean = Math.sin(Math.PI * t) * 0.1 * s.turnTo;
        s.frame = Math.abs(s.sx) < 0.3 ? 1 : (t < 0.5 ? 0 : 2);
        setAnim(s, 'turn', 0, s.frame);
        if (t >= 1) {
          s.facing = s.turnTo; s.sx = s.turnTo;
          const after = s.afterTurn;
          if (after === 'walk') beginState(s, 'walk');
          else if (after === 'notice') beginState(s, 'notice');
          else if (after === 'wave') beginState(s, 'wave');
          else if (after === 'react' && s.perf.kind) {   // performer mid-turn
            beginState(s, 'react');
            if (s.perf.kind === 'flagrun') { setAnim(s, 'walk', 1.5, 0); s.walkRamp = 0; }
            else if (s.perf.kind === 'dyn' || s.perf.kind === 'scurry') {
              setAnim(s, 'walk', 1.7, 0); s.walkRamp = 0.3;
            }
          }
          else beginState(s, 'idle');
        }
        break;
      }
      case 'notice':
        if (s.stateT >= 260) { s.prop = ''; beginState(s, 'wave'); }
        break;
      case 'wave': {
        // anticipation (90ms counter-lean) -> wave arcs -> overshoot -> held beat
        if (s.stateT < 90) s.lean = -0.08 * s.facing * (s.stateT / 90);
        else if (s.stateT < 130 && s.extraSY === 1) s.extraSY = 1.12;   // follow-through pop
        if (s.stateT > 1150 && s.animRate !== 0) { setAnim(s, 'wave', 0, 0); s.happyUntil = simNow + 400; }
        if (s.stateT >= 1500) beginState(s, 'idle');
        break;
      }
      case 'idleBreak': {
        const k = s.breakKind;
        if (k === 'pebble' && s.stateT > 300 && s.propT === 0) {
          s.propT = 1;
          emit(K_PEBBLE, s.x + s.facing * 10, s.y - 4, s.facing * rand(50, 90), -60, 900, 2, 0, 0);
        }
        if (k === 'look') {                       // glance one way, then the other
          s.look = s.stateT < 700 ? -2 : 2;
        }
        const dur = (k === 'stretch' || k === 'phone') ? rand(1600, 2400) : 1300;
        if (s.stateT >= dur) { s.propT = 0; s.look = 0; beginState(s, 'idle'); }
        break;
      }
      case 'sleep': {
        if (simNow >= s.zzAt) {
          s.zzAt = simNow + 1400;
          emit(K_ZZ, s.x + s.facing * 8, s.y - 58, 8, -22, 2400, 2, 0, 0);
        }
        const md = (mouse.x - s.x) * (mouse.x - s.x) + (mouse.y - s.y + 40) * (mouse.y - s.y + 40);
        if (lastEventAt > s.sleepAt || md < 120 * 120) {
          s.sleepAt = simNow + rand(60000, 120000);
          setAnim(s, 'stretch', 1, 0); s.breakKind = 'stretch'; s.lastBreak = 'stretch';
          beginState(s, 'idleBreak'); setAnim(s, 'stretch', 1, 0);
        }
        break;
      }
      case 'celebrate': {
        s.phaseT += dt;
        if (s.celPhase === 0) {                    // crouch = anticipation
          setAnim(s, 'celebrate', 0, 0);
          if (s.phaseT >= 140) {
            s.celPhase = 1; s.phaseT = 0;
            s.jumpV = -(205 + 35 * s.perf.intensity); s.landed = false;
          }
        } else if (s.celPhase === 1) {             // airborne: rise / fist at apex
          setAnim(s, 'celebrate', 0, s.jumpV < -60 ? 1 : 2);
          if (s.landed) {
            s.celPhase = 2; s.phaseT = 0;
            s.extraSY = 0.88;                      // landing squash
            if (TIERS[tier].pmul > 0 && s.pers.expr > 0.8)
              emitConfetti(s.x, s.y - 10, (6 * TIERS[tier].pmul) | 0);
          }
        } else {                                   // land beat, then again or settle
          setAnim(s, 'celebrate', 0, 3);
          if (s.phaseT >= 130) {
            s.perf.reps--;
            if (s.perf.reps > 0) { s.celPhase = 0; s.phaseT = 0; }
            else {
              s.extraSY = 1.1;                     // settle overshoot (easeOutBack feel)
              s.happyUntil = simNow + 900;
              beginState(s, 'idle');
            }
          }
        }
        break;
      }
      case 'react': {
        const p = s.perf;
        p.t += dt;
        if (p.kind === 'flagrun') {
          s.ty = s.y;
          const b = bandOf(s); s.tx = clamp(p.tx, b.x + 18, b.x + b.w - 18);
          if (s.state === 'react' && moveToward(s, dt, 1.9)) {
            p.kind = 'plant'; p.t = 0; setAnim(s, 'notice', 0, 0);
          }
        } else if (p.kind === 'plant') {
          if (p.t >= 350) {
            plantFlag(s.x + s.facing * 14, s.y, p.label);
            s.prop = ''; p.kind = 'salute'; p.t = 0;
            setAnim(s, 'wave', 0, 3);              // held salute frame
            s.happyUntil = simNow + 800;
          }
        } else if (p.kind === 'salute') {
          if (p.t >= 750) endPerf(s);
        } else if (p.kind === 'scroll') {
          s.propT += dt;                           // unfurl over ~700ms, then hold
          if (p.t >= 1700) endPerf(s);
        } else if (p.kind === 'cheer') {
          if (p.t >= 700) endPerf(s);
        } else if (p.kind === 'hop') {
          s.propT += dt;
          if (p.t >= 950) endPerf(s);
        } else if (p.kind === 'delayjump') {
          if (p.t >= p.delay) beginCelebrate(s, p.reps, p.intensity);
        } else if (p.kind === 'dodge') {
          if (p.delay === 0) {                     // 90ms anticipation lean INTO the threat
            s.lean = -0.14 * s.dodgeDir * Math.min(1, p.t / 90);
            if (p.t >= 90) {
              p.delay = 1;
              s.jumpV = -150; s.landed = false;
              s.extraSY = 0.86;                    // push-off squash
            }
          } else {                                 // airborne sidestep away
            const b = bandOf(s);
            s.x = clamp(s.x + s.dodgeDir * 170 * (dt / 1000), b.x + 16, b.x + b.w - 16);
            if (s.landed || p.t > 600) endPerf(s);
          }
        } else if (p.kind === 'spin') {
          const t = Math.min(1, p.t / 340);        // one full flat-sprite spin
          s.sx = s.facing * Math.cos(t * Math.PI * 2);
          if (Math.abs(s.sx) < 0.15) s.sx = s.sx < 0 ? -0.15 : 0.15;
          if (t >= 1) { s.sx = s.facing; endPerf(s); }
        } else if (p.kind === 'invite') {
          s.sx = s.facing;                         // held front pose; the chip drives exit
          if (!tetris.inviteOn || tetris.inviteSprite !== s) endPerf(s);
        } else if (p.kind === 'dyn') {             // approach the tall column
          s.ty = s.y;
          s.tx = p.tx;
          if (moveToward(s, dt, 1.7)) {
            p.kind = 'dynplant'; p.t = 0;
            setAnim(s, 'celebrate', 0, 0);         // crouch to plant
          }
        } else if (p.kind === 'dynplant') {
          if (p.t >= 350) {
            dynArm(s);                             // fuse lit — scurry target set inside
            p.kind = 'scurry'; p.t = 0;
            setAnim(s, 'walk', 1.8, 0); s.walkRamp = 0.4;
          }
        } else if (p.kind === 'scurry') {
          s.ty = s.y;
          if (moveToward(s, dt, 2) || p.t > 1400) endPerf(s);
        }
        break;
      }
      case 'tetris': {                             // rooftop duty on the krabTetris stack
        if (!tetris.active || !tetris.geo) { releaseDuty(s); break; }
        if (simNow >= s.colAt) {                   // wander to another column now and then
          s.colAt = simNow + rand(4000, 9000);
          s.col = randi(tetris.cols);
        }
        const h = tColH(s.col);
        if (h !== s.colH) {                        // the stack moved under them: small hop
          if (s.colH >= 0 && s.jumpY === 0) { s.jumpV = -120; s.landed = false; }
          s.colH = h;
        }
        const gx = clamp(tColX(s.col), tetris.px + 8, tetris.px + tetris.pw - 8);
        const gy = tStackY(s.col) - 1;
        s.y += clamp(gy - s.y, -260 * dts, 260 * dts);   // ride the stack top
        const ddx = gx - s.x, add = Math.abs(ddx);
        if (add > 3) {
          const dir = ddx < 0 ? -1 : 1;
          s.facing = s.sx = dir;
          s.x += dir * Math.min(add, 52 * dts);
          if (s.anim !== 'walk') setAnim(s, 'walk', 1.2, 0);
        } else if (s.anim !== 'idle') setAnim(s, 'idle', 1, randi(2));   // sit between moves
        break;
      }
      case 'exit': {
        moveToward(s, dt, 1.2);
        s.alpha -= dt / 400;                       // fade out while walking off
        if (s.alpha <= 0) s.dead = true;
        break;
      }
      case 'slump':
        if (s.stateT > 2500) beginState(s, 'idle');
        break;
    }

    // frame advance (rate 0 = state code owns the frame)
    if (s.animRate > 0) {
      const an = ANIMS[s.anim];
      s.animT += dt * s.animRate;
      if (s.animT >= an.ms) {
        s.animT -= an.ms;
        const n = BODY[s.anim][1];
        s.frame = an.loop ? (s.frame + 1) % n : Math.min(s.frame + 1, n - 1);
      }
    }

    // head lags the torso by ~2 walk frames (10 sim steps) — overlapping action
    s.headHist[s.hh] = META_B[s.anim][s.frame] || 0;
    s.hh = (s.hh + 1) & 15;
    s.headDrop = META_H[s.anim][s.frame] || 0;

    // decay procedural lean / follow-through scale back to rest
    s.lean -= s.lean * Math.min(1, dt / 120);
    s.extraSY += (1 - s.extraSY) * Math.min(1, dt / 90);

    // secondary motion springs, fed by the sprite's own movement
    const fac = s.sx < 0 ? -1 : 1;
    if (s.char === 1) {                            // hoodie drawstrings
      const ax1 = s.x + fac * SCALE * 1, ay1 = s.y + s.jumpY - SCALE * 10;
      const ax2 = s.x + fac * SCALE * 3, ay2 = s.y + s.jumpY - SCALE * 9;
      if (!s.springInit) { s.springInit = true; s.s1.x = ax1; s.s1.y = ay1 + 9; s.s2.x = ax2; s.s2.y = ay2 + 9; }
      stepSpring(s.s1, ax1, ay1 + 9, dts);
      stepSpring(s.s2, ax2, ay2 + 9, dts);
    } else if (s.char === 2) {                     // robot antenna
      const ax = s.x, ay = s.y + s.jumpY - SCALE * 14;
      if (!s.springInit) { s.springInit = true; s.s1.x = ax; s.s1.y = ay - 12; }
      stepSpring(s.s1, ax, ay - 12, dts);
    }
  }

  /* ── sprite rendering — interpolated position, whole-device-pixel snapping,
   * body + 2-frame-lagged head + runtime accessories. ────────────────────── */
  function drawSprite(g, s, alpha) {
    if (s.alpha <= 0 || s.dead) return;
    const ix = lerp(s.px, s.x, alpha);
    const iy = lerp(s.py, s.y, alpha) + s.jumpY;
    if (ix < -110 || ix > window.innerWidth + 110) return;   // cull offscreen
    const dx = snap(ix), dy = snap(iy);
    g.save();
    g.globalAlpha = s.alpha;
    g.translate(dx, dy);
    if (s.lean) g.rotate(s.lean);
    g.scale(s.sx * SCALE, s.extraSY * SCALE);
    const bd = BODY[s.anim];
    const col = bd[0] + (s.frame % bd[1]);
    g.drawImage(ATLAS, col * CW, s.char * CH, CW, CH, -12, -22, CW, CH);
    // head: reads the bob from ~2 walk frames ago (ring buffer) — the lag that
    // makes the body feel connected instead of stamped
    const lag = s.headHist[(s.hh + 6) & 15];
    let hv = 0;
    if (s.state === 'sleep' || s.state === 'slump' || s.blinkT > 0) hv = 1;
    else if (simNow < s.happyUntil) hv = 2;
    else if (Math.abs(s.look) > 1.2) hv = 3;
    const hx = Math.round(s.look);
    const hy = Math.round(lag - 7 + s.headDrop + s.lookY);
    if (REDUCED && s.fadeMix > 0) g.globalAlpha = s.alpha * (1 - s.fadeMix * 0.5);
    g.drawImage(ATLAS, (HEADCOL + hv) * CW, s.char * CH, CW, CH, -12 + hx, -22 + hy, CW, CH);
    // carried props (atlas cells, integer offsets in sprite space)
    if (s.prop === 'bang') {
      g.drawImage(ATLAS, PROPS.bang * CW, 3 * CH, CW, CH, -12, -36, CW, CH);
    } else if (s.prop === 'flag') {
      g.drawImage(ATLAS, (PROPS.flag0 + (((simNow / 260) | 0) % 2)) * CW, 3 * CH, CW, CH, -5, -28, CW, CH);
    } else if (s.prop === 'scroll') {
      const f = Math.min(2, (s.propT / 250) | 0);
      g.drawImage(ATLAS, (PROPS.scroll0 + f) * CW, 3 * CH, CW, CH, -10, -20, CW, CH);
    } else if (s.prop === 'check') {
      const rise = Math.min(8, (easePop(Math.min(1, s.propT / 450)) * 8) | 0);
      g.drawImage(ATLAS, PROPS.check * CW, 3 * CH, CW, CH, -12, -34 - rise, CW, CH);
    }
    g.restore();
    // accessory springs render in WORLD space (they simulate there) —
    // skipped under reduced motion / when the springs were never initialized
    if (!REDUCED && s.springInit) {
      g.globalAlpha = s.alpha;
      const fac = s.sx < 0 ? -1 : 1;
      if (s.char === 1) {                          // hoodie drawstrings
        g.fillStyle = PALS[1].string;
        drawString(g, ix + fac * SCALE * 1, iy - SCALE * 10, s.s1);
        drawString(g, ix + fac * SCALE * 3, iy - SCALE * 9, s.s2);
      } else if (s.char === 2) {                   // antenna rod + bobble
        const ax = ix, ay = iy - SCALE * 14;
        g.fillStyle = PALS[2].torsoD;
        g.fillRect(snap((ax + s.s1.x) / 2) - 1, snap((ay + s.s1.y) / 2) - 1, 2, 2);
        g.fillRect(snap(ax) - 1, snap(ay) - 2, 2, 3);
        g.fillStyle = PALS[2].accent;
        g.fillRect(snap(s.s1.x) - 2, snap(s.s1.y) - 2, 4, 4);
      }
    }
    g.globalAlpha = 1;
  }
  function drawString(g, ax, ay, sp) {             // 2-segment pixel chain
    g.fillRect(snap((ax + sp.x) / 2) - 1, snap((ay + sp.y) / 2) - 1, 2, 2);
    g.fillRect(snap(sp.x) - 1, snap(sp.y) - 1, 3, 3);
  }

  /* ── planted flags (stage.advanced) ─────────────────────────────────────── */
  function plantFlag(x, y, label) {
    let f = flags[0].on && !flags[1].on ? flags[1] : flags[0];
    if (flags[0].on && flags[1].on) f = flags[0].t0 <= flags[1].t0 ? flags[0] : flags[1];
    f.on = true; f.x = x; f.y = y; f.t0 = simNow; f.label = label || '';
  }
  function drawFlags(g) {
    for (let i = 0; i < 2; i++) {
      const f = flags[i];
      if (!f.on) continue;
      const age = simNow - f.t0;
      if (age > 6000) { f.on = false; continue; }
      g.globalAlpha = age > 5000 ? (6000 - age) / 1000 : 1;
      const wave = ((simNow / 300) | 0) % 2;
      g.drawImage(ATLAS, (PROPS.flag0 + wave) * CW, 3 * CH, CW, CH,
        snap(f.x) - 11 * SCALE, snap(f.y) - 18 * SCALE, CW * SCALE, CH * SCALE);
      if (f.label) {                               // payload text: canvas-drawn, length-clamped
        g.font = '600 10px ui-monospace,SFMono-Regular,Menlo,monospace';
        g.textAlign = 'center';
        g.fillStyle = THEME.muted;
        g.fillText(f.label, snap(f.x), snap(f.y) - 58);
      }
    }
    g.globalAlpha = 1;
  }

  /* ── the pixel car (driver.on_the_way) — crosses the strip, or drives into
   * a rail, brakes with a bounce, honks, U-turns through scaleX 0, leaves. ── */
  let carHonkT = 0;
  function carStart() {
    car.on = true; car.t = 0; car.honked = false; car.wheelT = 0; carHonkT = 0;
    if (bands.mode === 'strip') {
      // the car path NEVER enters the mic dead zone: drive in from an edge,
      // brake short of it, honk, U-turn, leave — same choreography as a rail
      const b = bands.a;
      car.bandIx = 0; car.phase = 1;
      car.dir = Math.random() < 0.5 ? 1 : -1;
      car.stopX = car.dir > 0 ? dead.x0 - 48 : dead.x1 + 48;
      if (car.dir > 0 ? car.stopX < b.x + 60 : car.stopX > b.x + b.w - 60) {
        car.on = false; return;                    // no room on this viewport: skip
      }
      car.x = car.dir > 0 ? b.x - 40 : b.x + b.w + 40;
      car.y = b.y + b.h - 6;
      car.px = car.x; car.py = car.y;
      car.vx = car.dir * 170;
    } else {
      car.bandIx = randi(bands.n); car.phase = 1;
      const b = car.bandIx ? bands.b : bands.a;
      const left = (b.x === 0);                    // band side, not band index (single-rail safe)
      car.dir = left ? 1 : -1;                     // enter from the screen edge
      car.stopX = left ? b.x + b.w - 36 : b.x + 36;   // brake toward the inner edge
      car.x = left ? b.x - 40 : b.x + b.w + 40;
      car.y = b.y + b.h - 6;
      car.px = car.x; car.py = car.y;
      car.vx = car.dir * 150;
    }
  }
  function honk() {
    car.honked = true; carHonkT = 500;
    emit(K_NOTE, car.x + car.dir * 24, car.y - 30, car.dir * 20, -50, 1100, 2, 0, 0);
    emit(K_NOTE, car.x + car.dir * 30, car.y - 24, car.dir * 30, -60, 1300, 2, 0, 0);
  }
  function carUpdate(dt) {
    if (!car.on) return;
    car.px = car.x; car.py = car.y;                // previous position for render lerp
    const dts = dt / 1000;
    car.t += dt; carHonkT = Math.max(0, carHonkT - dt);
    car.wheelT += dt * Math.min(1, Math.abs(car.vx) / 60);
    car.bounce = Math.sin(car.t / 70) * (Math.abs(car.vx) > 20 ? 1 : 0.3);   // slight bounce
    const b = (car.bandIx && bands.n > 1) ? bands.b : bands.a;
    if (car.phase === 1) {                         // brake toward the stop mark
      car.x += car.vx * dts;
      const d = Math.abs(car.stopX - car.x);
      if (d < 70) car.vx = car.dir * Math.max(26, 150 * (d / 70));
      if ((car.dir > 0 && car.x >= car.stopX) || (car.dir < 0 && car.x <= car.stopX)) {
        car.x = car.stopX; car.vx = 0; car.phase = 2; car.t = 0; honk();
      }
    } else if (car.phase === 2) {                  // held honk beat
      if (car.t > 800) { car.phase = 3; car.t = 0; }
    } else if (car.phase === 3) {                  // U-turn through scaleX 0
      if (car.t >= 220) { car.dir = -car.dir; car.phase = 4; car.vx = car.dir * 40; }
    } else {                                       // accelerate away
      car.vx += car.dir * 280 * dts;
      car.x += car.vx * dts;
      if (car.x < b.x - 60 || car.x > b.x + b.w + 60) car.on = false;
    }
    // nearby idle sprites cheer at the car (small action — no attention slot)
    for (let i = 0; i < sprites.length; i++) {
      const s = sprites[i];
      if (s.retiring || s.dead || s.duty || s.cheerAt > simNow - 4000) continue;
      if ((s.state === 'idle' || s.state === 'walk') && Math.abs(s.x - car.x) < 240 &&
          Math.abs(s.y - car.y) < 200) {
        s.cheerAt = simNow;
        beginPerf(s, 'cheer', s.x, '', 0);
        s.happyUntil = simNow + 900;
      }
    }
  }
  function carDraw(g, alpha) {
    if (!car.on) return;
    const f = ((car.wheelT / 70) | 0) % 2;
    let sx = car.dir;
    if (car.phase === 3) sx = car.dir * Math.cos(Math.PI * Math.min(1, car.t / 220));
    const cx = lerp(car.px, car.x, alpha);         // interpolated like sprites
    const cy = lerp(car.py, car.y, alpha);
    g.save();
    g.translate(snap(cx), snap(cy + car.bounce));
    g.scale(sx * SCALE, SCALE);
    g.drawImage(ATLAS, (PROPS.car0 + f) * CW, 3 * CH, CW, CH, -12, -22, CW, CH);
    if (carHonkT > 0 && ((simNow / 90) | 0) % 2) { // headlight flicker while honking
      g.fillStyle = '#ffd964'; g.fillRect(10, -8, 3, 2);
    }
    g.restore();
  }

  /* ── reactions — a data-driven config map, an 800ms coalescer that merges
   * same-type events (12 wins = ONE big celebration), and a depth-3 priority
   * queue with a 4s floor between big moments. ────────────────────────────── */
  const REACT_CFG = {
    'deal.won':          { pri: 4, big: true,  major: true,  dur: 2600 },
    'stage.advanced':    { pri: 3, big: true,  major: false, dur: 3600 },
    'lead.created':      { pri: 2, big: false, major: false, dur: 1900 },
    'driver.on_the_way': { pri: 3, big: false, major: false, dur: 4800 },
    'task.completed':    { pri: 1, big: false, major: false, dur: 1100 },
    'goal.hit':          { pri: 5, big: true,  major: true,  dur: 5600 },
  };
  const R_TYPES = Object.keys(REACT_CFG);
  const pend = Object.create(null);
  for (let i = 0; i < R_TYPES.length; i++)
    pend[R_TYPES[i]] = { on: false, count: 0, last: 0, firstAt: 0, label: '' };
  const rq = [
    { on: false, type: '', count: 0, pri: 0, at: 0, label: '' },
    { on: false, type: '', count: 0, pri: 0, at: 0, label: '' },
    { on: false, type: '', count: 0, pri: 0, at: 0, label: '' },
  ];
  function intake(type, payload) {
    if (!Object.prototype.hasOwnProperty.call(REACT_CFG, type)) return;
    const cfg = REACT_CFG[type];
    if (!cfg || !live || REDUCED) return;
    if (MODE === 'subtle' && !cfg.major) return;   // subtle = major reactions only
    lastEventAt = simNow;
    const p = pend[type];
    if (!p.on) p.firstAt = performance.now();      // starvation guard anchor
    p.on = true; p.count++; p.last = performance.now();
    if (type === 'stage.advanced' && payload && payload.label != null)
      p.label = String(payload.label).slice(0, 14); // payload is data; clamped, canvas-only
  }
  function enqueue(type, count, label) {
    const pri = REACT_CFG[type].pri;
    for (let i = 0; i < 3; i++)                     // merge with a queued same-type
      if (rq[i].on && rq[i].type === type) { rq[i].count += count; return; }
    let slot = -1;
    for (let i = 0; i < 3; i++) if (!rq[i].on) { slot = i; break; }
    if (slot < 0) {                                 // full: drop the oldest lowest-pri
      let low = 0;
      for (let i = 1; i < 3; i++)
        if (rq[i].pri < rq[low].pri || (rq[i].pri === rq[low].pri && rq[i].at < rq[low].at)) low = i;
      if (rq[low].pri > pri) return;                // the newcomer loses instead
      slot = low;
    }
    const q = rq[slot];
    q.on = true; q.type = type; q.count = count; q.pri = pri; q.at = simNow; q.label = label || '';
  }
  function updateReactions(dt) {
    const pn = performance.now();
    for (let i = 0; i < R_TYPES.length; i++) {      // flush the coalescer
      const t = R_TYPES[i], p = pend[t];
      // flush on quiet OR when same-type events have kept arriving too long
      if (p.on && (pn - p.last >= COALESCE_MS || pn - p.firstAt > 4 * COALESCE_MS)) {
        p.on = false; enqueue(t, p.count, p.label);
        p.count = 0; p.label = '';
      }
    }
    if (simNow >= reactUntil) {                     // reactions never interrupt each other
      let best = -1;
      for (let i = 0; i < 3; i++) {
        const q = rq[i];
        if (!q.on) continue;
        if (REACT_CFG[q.type].big && simNow - lastBigAt < BIG_GAP) continue;
        if (best < 0 || q.pri > rq[best].pri || (q.pri === rq[best].pri && q.at < rq[best].at)) best = i;
      }
      if (best >= 0) {
        const q = rq[best]; q.on = false;
        runReaction(q.type, q.count, q.label);
      }
    }
    goalUpdate(dt);
    glowRail = Math.max(0, glowRail - dt / 900);
    if (goal.phase !== 2) glowScreen = Math.max(0, glowScreen - dt / 1400);
  }
  function runReaction(type, count, label) {
    const cfg = REACT_CFG[type];
    // intensity scales with the merged count, log2-ish, capped at 3
    const inten = 1 + Math.min(2, Math.floor(Math.log(Math.max(1, count)) / Math.LN2));
    reactUntil = simNow + cfg.dur + (inten - 1) * 400;
    if (cfg.big) lastBigAt = simNow;
    attUntil = Math.max(attUntil, reactUntil);      // a reaction owns the attention budget
    const pmul = TIERS[tier].pmul;
    let voice = null;                               // ONE event bubble per coalesced reaction
    if (type === 'deal.won') {
      const s = pickPerformer();
      if (s) { beginCelebrate(s, 1 + inten, inten); s.happyUntil = simNow + 2500; voice = s; }
      if (pmul > 0) {
        const n = (26 * inten * pmul) | 0;
        if (bands.mode === 'rails') {
          emitConfetti(bands.a.x + bands.a.w - 12, bands.a.y + bands.a.h * 0.45, n);
          if (bands.n > 1) emitConfetti(bands.b.x + 12, bands.b.y + bands.b.h * 0.45, n);
        } else if (s) emitConfetti(s.x, s.y - 30, n * 2);
      }
      glowRail = 1;
    } else if (type === 'stage.advanced') {
      const s = pickPerformer();
      if (s) {
        const b = bandOf(s);
        const tx = s.x < b.x + b.w / 2 ? b.x + b.w - 28 : b.x + 28;
        beginPerf(s, 'flagrun', tx, label, 0);
        voice = s;
      }
    } else if (type === 'lead.created') {
      if (sprites.length < desiredSprites()) voice = spawnSprite(true);
      else { const s = pickPerformer(); if (s) { beginPerf(s, 'scroll', s.x, '', 0); voice = s; } }
    } else if (type === 'driver.on_the_way') {
      carStart();
      voice = pickPerformer();
    } else if (type === 'task.completed') {
      const s = pickPerformer();
      if (s) { beginPerf(s, 'hop', s.x, '', 0); s.happyUntil = simNow + 1000; voice = s; }
    } else if (type === 'goal.hit') {              // the biggest moment
      goal.phase = 1; goal.at = simNow + 2400; goal.inten = inten;
      for (let i = 0; i < sprites.length; i++) {
        const s = sprites[i];
        if (s.retiring || s.dead || s.duty) continue;   // rooftop crew stays put
        const b = bandOf(s);
        s.tx = b.x + b.w / 2;
        if (bands.mode === 'strip') {
          s.tx = stripX(s.x);                      // gather, but outside the mic dead zone
          s.ty = b.y + b.h - 10;
        } else s.ty = b.y + b.h * 0.78;
        if (s.state !== 'walk') beginState(s, 'walk');   // reactions interrupt idle
        if (!voice) voice = s;
      }
      glowScreen = 1;
    }
    if (voice) bubbleSay(voice, pickLine(type));
  }
  function goalUpdate(dt) {
    if (goal.phase === 0) return;
    if (goal.phase === 1 && simNow >= goal.at) {
      goal.phase = 2;
      let k = 0;
      for (let i = 0; i < sprites.length; i++) {
        const s = sprites[i];
        if (s.retiring || s.dead || s.duty) continue;
        beginPerf(s, 'delayjump', s.x, '', k * 130);    // staggered, never lockstep
        s.perf.reps = 2 + (goal.inten | 0);
        s.perf.intensity = goal.inten || 1;
        s.happyUntil = simNow + 5000;
        k++;
      }
      goal.pulseAt = simNow;
    }
    if (goal.phase === 2) {
      glowScreen = 0.55 + 0.45 * Math.sin(simNow / 260); // screen-edge pulse
      if (TIERS[tier].pmul > 0 && simNow - goal.pulseAt > 420) {
        goal.pulseAt = simNow;
        const b = bands.a;
        emitConfetti(b.x + rand(10, b.w - 10), b.y + b.h * 0.4, (12 * TIERS[tier].pmul) | 0);
        if (bands.n > 1) emitConfetti(bands.b.x + rand(10, bands.b.w - 10), bands.b.y + bands.b.h * 0.4, (12 * TIERS[tier].pmul) | 0);
      }
      if (simNow >= reactUntil) { goal.phase = 0; glowScreen = 0.8; }
    }
  }

  /* ── glow accents (canvas-only, inside the clip, theme-colored) —
   * gradients are CACHED: rebuilt only when band geometry (evalBands) or the
   * glow colors (refreshTheme) change, never per frame. ───────────────────── */
  let glowGradsDirty = true;
  let gradRailA = null, gradRailB = null;
  const gradScr = [null, null];
  function rebuildGlowGrads(g) {
    glowGradsDirty = false;
    gradRailA = gradRailB = null;
    gradScr[0] = gradScr[1] = null;
    if (bands.mode === 'rails') {
      // rail glow sits on the INNER (content-facing) edge of each rail
      const aLeft = bands.a.x === 0;
      gradRailA = aLeft
        ? g.createLinearGradient(bands.a.x + bands.a.w - 22, 0, bands.a.x + bands.a.w, 0)
        : g.createLinearGradient(bands.a.x + 22, 0, bands.a.x, 0);
      gradRailA.addColorStop(0, 'rgba(0,0,0,0)'); gradRailA.addColorStop(1, THEME.glow);
      if (bands.n > 1) {
        gradRailB = g.createLinearGradient(bands.b.x + 22, 0, bands.b.x, 0);
        gradRailB.addColorStop(0, 'rgba(0,0,0,0)'); gradRailB.addColorStop(1, THEME.glow);
      }
    }
    for (let i = 0; i < bands.n; i++) {
      const b = i ? bands.b : bands.a;
      const gr = g.createLinearGradient(b.x === 0 ? 16 : b.x + b.w, 0, b.x === 0 ? 0 : b.x + b.w - 16, 0);
      gr.addColorStop(0, 'rgba(0,0,0,0)'); gr.addColorStop(1, THEME.ok);
      gradScr[i] = gr;
    }
  }
  function drawGlow(g) {
    if (glowGradsDirty) rebuildGlowGrads(g);
    if (glowRail > 0.01 && bands.mode === 'rails' && gradRailA) {
      g.globalAlpha = glowRail * 0.4;
      const aLeft = bands.a.x === 0;
      g.fillStyle = gradRailA;
      g.fillRect(aLeft ? bands.a.x + bands.a.w - 22 : bands.a.x, bands.a.y, 22, bands.a.h);
      if (bands.n > 1 && gradRailB) {
        g.fillStyle = gradRailB;
        g.fillRect(bands.b.x, bands.b.y, 22, bands.b.h);
      }
      g.globalAlpha = 1;
    }
    if (glowScreen > 0.01) {
      g.globalAlpha = Math.max(0, Math.min(1, glowScreen)) * 0.35;
      for (let i = 0; i < bands.n; i++) {
        const b = i ? bands.b : bands.a;
        if (!gradScr[i]) continue;
        const edgeX = b.x === 0 ? 0 : b.x + b.w - 16;
        g.fillStyle = gradScr[i];
        g.fillRect(edgeX, b.y, 16, b.h);
      }
      g.globalAlpha = 1;
    }
  }

  /* ── tetris integration — window.krabTetris is OPTIONAL and typeof-guarded
   * at every touch point.  Expected surface (all reads are defensive):
   *   krabTetris.start({onEvent, onClose})   krabTetris.stop()
   *   krabTetris.geometry() ->
   *     { rect:{left,top,width,height},  BOARD rect in viewport px
   *       cell, cols, rows,    cell size + grid dimensions
   *       colHeights }         settled-stack height per column, in cells
   *   krabTetris.blast(col, rowNearTop)      krabTetris.nudge(±1)
   *   krabTetris.pause(on)                   krabTetris.active()
   *   krabTetris.element?                    panel CARD node — the shake target,
   *                                          and the top of the HUD band that
   *                                          bubbles must clear (its rect top
   *                                          down to the board top; the old
   *                                          fixed headerH was a wrong guess)
   * The USER CLOSING the panel arrives as the onClose CALLBACK, never as an
   * event; RESTART after game over arrives as a fresh 'start' event on a panel
   * that is still live, which we re-adopt.
   * While a game runs the sprite canvas z-index is raised to 23 (panel: 22)
   * so sprites draw OVER the panel; pointer-events stays none, so play is
   * unaffected.  geometry() is polled at LOGIC rate, ONLY while active. */
  const tetris = {
    active: false, starting: false, geo: null,
    px: 0, py: 0, pw: 0, ph: 0, headerH: 34, cell: 8, cols: 10, rows: 20,
    panelTop: 0, panelTopAt: -1e9,               // measured card top (HUD band)
    inviteOn: false, inviteEl: null, inviteAt: 0, inviteSprite: null,
    declineUntil: 0, lastGameAt: 0,
    blasts: 0, lastBlastAt: -1e9, nudgeAt: -1e9, nudgePendAt: 0, nudgeDir: 1,
    shakeT: 0, shakeArmed: false,
    panelEl: null, prevTransform: '', shakeA: '', shakeB: '',
  };
  const dyn = { on: false, t: 0, x: 0, y: 0, col: 0 };   // one dynamite at a time
  function KT() {
    return (typeof window.krabTetris !== 'undefined' && window.krabTetris)
      ? window.krabTetris : null;
  }
  function tPollGeo() {
    const kt = KT();
    if (!kt || typeof kt.geometry !== 'function') { tetris.geo = null; return; }
    let g = null;
    try { g = kt.geometry(); } catch (e) { g = null; }
    tetris.geo = (g && typeof g === 'object') ? g : null;
    if (!tetris.geo) return;
    // The real module reports rect:{left,top,width,height}; older shapes
    // used bare x/y/w/h — accept both so the interface cannot drift apart.
    const _r = tetris.geo.rect;
    tetris.px = (+tetris.geo.x || 0) || (_r ? (+_r.left || 0) : 0);
    tetris.py = (+tetris.geo.y || 0) || (_r ? (+_r.top || 0) : 0);
    tetris.pw = (+tetris.geo.w || 0) || (_r ? (+_r.width || 0) : 0);
    tetris.ph = (+tetris.geo.h || 0) || (_r ? (+_r.height || 0) : 0);
    tetris.headerH = (+tetris.geo.headerH > 0) ? +tetris.geo.headerH : 34;
    tetris.cols = (tetris.geo.colHeights && tetris.geo.colHeights.length) ||
                  ((+tetris.geo.cols > 0) ? (+tetris.geo.cols | 0) : 10);
    tetris.rows = (+tetris.geo.rows > 0) ? (+tetris.geo.rows | 0) : 20;
    tetris.cell = (+tetris.geo.cell > 0) ? +tetris.geo.cell
                : (tetris.pw > 0 ? tetris.pw / tetris.cols : 8);
    // the HUD band bubbles must clear runs from the panel CARD's top down to
    // the board top — MEASURED off the card, because the header + stats rows
    // are as tall as the theme's fonts make them.  At most 4 reads/second.
    if (simNow - tetris.panelTopAt > 250) {
      tetris.panelTopAt = simNow;
      const el = tetris.panelEl;
      let top = tetris.py - tetris.headerH;         // fallback: the old estimate
      if (el && el.getBoundingClientRect) {
        try { const pr = el.getBoundingClientRect(); if (pr) top = pr.top; } catch (e) {}
      }
      tetris.panelTop = (top < tetris.py) ? top : tetris.py;
    }
  }
  function tColH(c) {
    const g = tetris.geo;
    if (!g || !g.colHeights || c >= g.colHeights.length) return 0;
    return g.colHeights[c] | 0;
  }
  function tColX(c) { return tetris.px + (c + 0.5) * tetris.cell; }
  function tStackY(c) {                            // viewport y of col c's stack top
    return tetris.py + tetris.ph - tColH(c) * tetris.cell;
  }
  function tTallestCol() {
    let best = 0, bh = -1;
    for (let c = 0; c < tetris.cols; c++) { const h = tColH(c); if (h > bh) { bh = h; best = c; } }
    return best;
  }
  function firstDuty() {
    for (let i = 0; i < sprites.length; i++) {
      const s = sprites[i];
      if (s.duty && !s.dead && !s.retiring) return s;
    }
    return null;
  }

  /* invite chip — the ONE interactive DOM element this layer may own: two
   * 44px-target buttons, theme-styled via the page's CSS variables, auto-
   * dismissed from the logic clock (no timers to leak), removed on teardown. */
  function removeInviteChip() {
    tetris.inviteOn = false;
    if (tetris.inviteEl && tetris.inviteEl.parentNode)
      tetris.inviteEl.parentNode.removeChild(tetris.inviteEl);
    tetris.inviteEl = null;
    const s = tetris.inviteSprite;
    tetris.inviteSprite = null;
    if (s && !s.dead && s.state === 'react' && s.perf.kind === 'invite') endPerf(s);
  }
  function onInviteYes() { removeInviteChip(); startTetris(); }
  function onInviteNo() {
    tetris.declineUntil = simNow + INVITE_DECLINE; // NO -> 5min cooldown
    tetris.lastGameAt = simNow;
    removeInviteChip();
  }
  function showInvite(s) {
    tetris.inviteOn = true; tetris.inviteAt = simNow; tetris.inviteSprite = s;
    beginPerf(s, 'invite', s.x, '', 0);            // stop, face out
    bubbleSay(s, pickLine('invite'));
    const el = document.createElement('div');
    el.setAttribute('data-krab-game', 'invite');
    // placed AFTER it is measured: hidden first, so the clamp and the mic
    // dead-zone test use the chip's REAL box (the buttons are theme-styled —
    // font metrics decide the width; the old 124/132px guess put it under the
    // mic button on wider fonts)
    el.style.cssText = 'position:fixed;z-index:24;display:flex;gap:6px;padding:5px;' +
      'border-radius:10px;background:var(--card,#fff);border:1px solid var(--line,#dfe1e6);' +
      'box-shadow:0 6px 18px rgba(9,30,66,.22);left:0;top:0;visibility:hidden;';
    const yes = document.createElement('button'), no = document.createElement('button');
    yes.type = 'button'; no.type = 'button';
    yes.textContent = 'YES'; no.textContent = 'NO';
    const base = 'min-width:44px;min-height:44px;padding:0 10px;border-radius:8px;cursor:pointer;' +
      'font:700 12px ui-monospace,SFMono-Regular,Menlo,monospace;';
    yes.style.cssText = base + 'border:1px solid var(--accent,#0065ff);background:var(--accent,#0065ff);color:#fff;';
    no.style.cssText = base + 'border:1px solid var(--line,#dfe1e6);background:transparent;color:var(--ink,#172b4d);';
    yes.addEventListener('click', onInviteYes);
    no.addEventListener('click', onInviteNo);
    el.appendChild(yes); el.appendChild(no);
    document.body.appendChild(el);
    tetris.inviteEl = el;
    const vw = window.innerWidth, vh = window.innerHeight;
    const cw = Math.max(60, el.offsetWidth || 132);   // ONE layout read per invite
    const ch = Math.max(44, el.offsetHeight || 54);
    const maxX = Math.max(8, vw - cw - 8);
    let lx = clamp(s.x - cw / 2, 8, maxX);
    const ly = clamp(s.y - 150, 8, Math.max(8, vh - ch - 8));
    if (lx + cw > dead.x0 && lx < dead.x1 && ly + ch > dead.y0)     // keep clear of the mic
      lx = (s.x < vw / 2) ? clamp(dead.x0 - cw - 8, 8, maxX) : clamp(dead.x1 + 8, 8, maxX);
    el.style.left = Math.round(lx) + 'px';       // whole px: crisp border, no blur
    el.style.top = Math.round(ly) + 'px';
    el.style.visibility = 'visible';
  }

  // the page styles its toasts out of the panel's way from this body class
  function bodyTetrisClass(on) {
    try {
      if (!document.body) return;
      if (on) document.body.classList.add('krab-tetris-on');
      else document.body.classList.remove('krab-tetris-on');
    } catch (e) {}
  }
  function armTetris(kt) {                         // shared: fresh start AND restart re-adopt
    tetris.active = true; tetris.geo = null;
    tetris.pw = 0; tetris.ph = 0;                  // no clip rect, no HUD band, and no
                                                   //   bubble displacement against a
                                                   //   previous game's stale geometry
    tetris.blasts = 0; tetris.lastBlastAt = simNow;
    tetris.nudgeAt = simNow; tetris.nudgePendAt = 0;
    dyn.on = false;
    removeInviteChip();                            // a running game supersedes any invite
    tetris.panelEl = (kt.element && kt.element.style) ? kt.element
                   : (kt.el && kt.el.style) ? kt.el : null;
    // shake strings are built at ARM time (armShake) so a layout() transform
    // change mid-game is never stomped — here they only get cleared
    tetris.prevTransform = ''; tetris.shakeA = ''; tetris.shakeB = '';
    tetris.shakeT = 0; tetris.shakeArmed = false;
    tetris.panelTop = 0; tetris.panelTopAt = -1e9; // force a fresh card-top read
    bodyTetrisClass(true);
    if (cv) cv.style.zIndex = '23';                // over the panel (22); input unaffected
    if (live && !REDUCED) { tPollGeo(); assignDuty(); }
  }
  function armShake() {                            // snapshot NOW, not at game start —
    const el = tetris.panelEl;                     //   layout() may have moved the panel
    tetris.prevTransform = el ? (el.style.transform || '') : '';
    const pre = tetris.prevTransform ? tetris.prevTransform + ' ' : '';
    tetris.shakeA = pre + 'translate(2px,1px)';    // prebuilt: no per-frame strings
    tetris.shakeB = pre + 'translate(-2px,-1px)';
    tetris.shakeT = 150; tetris.shakeArmed = true;
  }
  function startTetris() {
    const kt = KT();
    if (!kt || typeof kt.start !== 'function' || tetris.active) return;
    // the module fires 'start' SYNCHRONOUSLY inside start(); the guard keeps
    // the restart-adoption path in onTetrisEvent out of this fresh start
    tetris.starting = true;
    try {
      kt.start({ onEvent: onTetrisEvent,
                 onClose: function () { endTetris(false); } });   // user-close is a CALLBACK,
    } catch (e) { tetris.starting = false; return; }              //   never an event
    tetris.starting = false;
    armTetris(kt);
  }
  function assignDuty() {                          // 1-2 sprites relocate to the panel
    if (!tetris.geo || tetris.pw <= 0) return;
    let active = 0;
    for (let i = 0; i < sprites.length; i++)
      if (!sprites[i].retiring && !sprites[i].dead) active++;
    let want = Math.min(active >= 3 ? 2 : 1, active);
    for (let i = 0; i < sprites.length; i++) if (sprites[i].duty) want--;
    for (let i = 0; i < sprites.length && want > 0; i++) {
      const s = sprites[i];
      if (s.retiring || s.dead || s.duty) continue;
      if (s.state === 'celebrate' || s.state === 'react') continue;
      s.duty = 1; want--;
      s.col = randi(tetris.cols); s.colH = -1;
      s.colAt = simNow + rand(4000, 9000);
      s.x = tetris.px + ((s.col < tetris.cols / 2) ? 8 : tetris.pw - 8);
      s.y = tetris.py + tetris.ph - 2;
      s.px = s.x; s.py = s.y;
      s.alpha = 0;                                 // fade in on the panel
      s.jumpY = 0; s.jumpV = 0; s.prop = '';
      beginState(s, 'tetris');
    }
  }
  function releaseDuty(s) {                        // …and walk back onto their rails
    s.duty = 0; s.colH = -1; s.prop = '';
    s.bandIx = bands.n > 1 ? s.pers.rail : 0;
    const b = bandOf(s);
    s.x = clamp(s.x, b.x + 16, b.x + b.w - 16);
    s.y = (bands.mode === 'strip') ? b.y + b.h - 10
        : b.y + 56 + Math.random() * Math.max(8, b.h - 70);
    s.px = s.x; s.py = s.y;
    s.alpha = 0; s.jumpY = 0; s.jumpV = 0;         // fade back in on the rail
    pickWaypoint(s);
    beginState(s, 'idle');
  }
  function endTetris(alsoStop) {
    removeInviteChip();
    bodyTetrisClass(false);                        // toasts go back where they were
    const wasActive = tetris.active;
    if (!wasActive && !alsoStop) return;
    const kt = KT();
    if (alsoStop && kt && typeof kt.stop === 'function') { try { kt.stop(); } catch (e) {} }
    if (!wasActive) return;
    tetris.active = false; tetris.geo = null; tetris.lastGameAt = simNow;
    dyn.on = false;
    // restore ONLY a transform we actually took a snapshot of: with no shake
    // this game, prevTransform is '' — writing it would wipe the panel's own
    // centering translate on a game-over panel that stays on screen
    if (tetris.panelEl && tetris.shakeArmed) {
      try { tetris.panelEl.style.transform = tetris.prevTransform; } catch (e) {}
    }
    tetris.panelEl = null; tetris.shakeT = 0; tetris.shakeArmed = false;
    if (cv) cv.style.zIndex = 'var(--z-sprites,20)';    // restore stacking
    for (let i = 0; i < sprites.length; i++) {          // stop per-tick geometry polling;
      const s = sprites[i];                             // rooftop crew heads home
      if (s.duty && !s.dead) releaseDuty(s);
      else s.duty = 0;
    }
  }
  function onTetrisEvent(ev) {
    // events from the game module are DATA — only known types act
    const type = (typeof ev === 'string') ? ev
               : (ev && typeof ev.type === 'string') ? ev.type : '';
    if (type === 'start') {
      // RESTART after game over: 'over' sent the crew home and cleared
      // tetris.active, but the panel is still live and a human just pressed
      // RESTART.  Re-adopt it — crew back on the roof, invites suppressed.
      // (A fresh start() emits this synchronously; tetris.starting skips it,
      // startTetris arms straight after.)
      if (tetris.active || tetris.starting) return;
      const kt = KT();
      let livePanel = false;
      try {
        livePanel = !!kt && ((typeof kt.active === 'function') ? !!kt.active()
                                                              : !!kt.element);
      } catch (e) { livePanel = false; }
      if (livePanel) armTetris(kt);
    } else if (type === 'clear') {                 // line clear: rooftop crew cheers
      let said = false;
      for (let i = 0; i < sprites.length; i++) {
        const s = sprites[i];
        if (!s.duty || s.dead) continue;
        s.jumpV = -170; s.landed = false; s.happyUntil = simNow + 1500;
        if (TIERS[tier].pmul > 0) emitConfetti(s.x, s.y - 20, (5 * TIERS[tier].pmul) | 0);
        if (!said) said = bubbleSay(s, pickLine('clear'));
      }
    } else if (type === 'over') {                  // game over: slump, then head home
      const mourner = firstDuty();
      endTetris(false);
      if (mourner && !mourner.dead) {
        beginState(mourner, 'slump');
        bubbleSay(mourner, pickLine('over'));
      }
    } else if (type === 'close' || type === 'stop') {
      endTetris(false);
    }
  }
  function dynArm(s) {                             // fuse lit; sprite scurries away
    dyn.on = true; dyn.t = 0;
    dyn.x = s.perf.tx; dyn.y = tStackY(dyn.col);
    s.perf.tx = (s.x > tetris.px + tetris.pw / 2)  // far side of the panel
      ? tetris.px + 10 : tetris.px + tetris.pw - 10;
  }
  function drawDyn(g) {                            // 6x8px stick, blinking fuse tip
    if (!dyn.on) return;
    const x = Math.round(dyn.x) - 3, y = Math.round(dyn.y) - 8;
    g.fillStyle = '#c8433c'; g.fillRect(x, y, 6, 8);
    g.fillStyle = '#93271f'; g.fillRect(x + 4, y, 2, 8);
    g.fillStyle = (((simNow / 120) | 0) % 2) ? '#ffd964' : '#e2543b';
    g.fillRect(x + 2, y - 2, 2, 2);
  }
  function tetrisUpdate(dt) {
    // invite chip auto-dismiss, driven by the logic clock
    if (tetris.inviteOn && simNow - tetris.inviteAt >= INVITE_TTL) {
      tetris.lastGameAt = simNow;                  // soft decline: wait a full idle window
      removeInviteChip();
    }
    if (!tetris.active) {
      // INVITE: idle >= 90s since boot/last game, tier High/Medium, not reduced
      if (tetris.inviteOn || REDUCED || tier > 1) return;
      if (simNow - tetris.lastGameAt < INVITE_IDLE || simNow < tetris.declineUntil) return;
      const kt = KT();
      if (!kt || typeof kt.start !== 'function') return;
      const s = pickPerformer();
      if (s && (s.state === 'idle' || s.state === 'walk' || s.state === 'idleBreak'))
        showInvite(s);
      return;
    }
    tPollGeo();                                    // LOGIC rate, only while active
    if (!tetris.geo || tetris.pw <= 0) return;
    if (!firstDuty()) assignDuty();                // keep 1-2 sprites on duty
    // PANEL-only screen shake: 1-2px for 150ms, composed over the panel's own
    // transform — the page itself never moves
    if (tetris.shakeT > 0) {
      tetris.shakeT -= dt;
      const done = tetris.shakeT <= 0;
      const el = tetris.panelEl;
      if (el) {
        try {
          el.style.transform = done ? tetris.prevTransform
            : ((((simNow / 30) | 0) % 2) ? tetris.shakeA : tetris.shakeB);
        } catch (e) {}
      }
      // the snapshot is already back on the element — endTetris must not write
      // this (by then possibly stale) string a second time
      if (done) tetris.shakeArmed = false;
    }
    // DYNAMITE: at most 3 per game, >= 20s apart
    if (dyn.on) {
      dyn.t += dt;
      dyn.y = tStackY(dyn.col);                    // ride the stack if it shifts
      if (dyn.t >= 1100) {
        const kt = KT();
        const rowTop = clamp(tetris.rows - tColH(dyn.col), 0, tetris.rows - 1);
        let cleared = 0;
        if (kt && typeof kt.blast === 'function') {
          try { cleared = kt.blast(dyn.col, rowTop) | 0; } catch (e) { cleared = 0; }
        }
        // blast() refuses during the 150ms line-clear flash. Book the cost only
        // when something actually blew up — otherwise hold the fuse and try
        // again next tick, so one of just three dynamites is never burned on a
        // no-op (and the player never sees an explosion that destroyed nothing).
        if (!cleared && kt && !tetris.dynGaveUp) {
          dyn.t = 1000;                            // re-arm: retry on a later tick
          if ((tetris.dynRetries = (tetris.dynRetries || 0) + 1) > 30)
            tetris.dynGaveUp = true;               // ~0.5s of retries, then let it go
          return;
        }
        tetris.dynRetries = 0; tetris.dynGaveUp = false;
        dyn.on = false;
        tetris.blasts++; tetris.lastBlastAt = simNow;
        emitBoom(dyn.x, dyn.y - 6);
        armShake();                                // re-snapshots the panel's CURRENT
                                                   //   transform: a layout() move
                                                   //   mid-game is never stomped
        const vs = firstDuty();
        if (vs) bubbleSay(vs, pickLine('boom'));
      }
    } else if (tetris.blasts < 3 && simNow - tetris.lastBlastAt >= 20000 &&
               Math.random() < 0.0007) {           // per logic tick ≈ every ~24s once armed
      const vs = firstDuty();
      if (vs && vs.state === 'tetris' && tColH(tTallestCol()) > 2) {
        dyn.col = tTallestCol();
        tetris.lastBlastAt = simNow;               // stamp at LAUNCH: enforces the 20s
        beginPerf(vs, 'dyn',                       //   gap and bars a second run mid-plant
          clamp(tColX(dyn.col), tetris.px + 8, tetris.px + tetris.pw - 8), '', 0);
      }
    }
    // MISCHIEF: max once per 30s, telegraphed 600ms ahead of the nudge
    if (tetris.nudgePendAt) {
      if (simNow >= tetris.nudgePendAt) {
        tetris.nudgePendAt = 0;
        const kt = KT();
        // The sprite already announced this 600ms ago — so if the telegraphed
        // direction is blocked (wall or stack), push the OTHER way rather than
        // let the tell land on nothing.
        if (kt && typeof kt.nudge === 'function') {
          try {
            if (!kt.nudge(tetris.nudgeDir)) kt.nudge(-tetris.nudgeDir);
          } catch (e) {}
        }
      }
    } else if (simNow - tetris.nudgeAt >= 30000 && Math.random() < 0.0006) {
      const vs = firstDuty();
      if (vs) {
        tetris.nudgeAt = simNow;
        tetris.nudgeDir = Math.random() < 0.5 ? -1 : 1;
        tetris.nudgePendAt = simNow + 600;
        bubbleSay(vs, pickLine('mischief'));       // "hehe >:)" — the tell
      }
    }
  }

  /* ── population management ──────────────────────────────────────────────── */
  function desiredSprites() {
    if (MODE === 'off') return 0;
    if (!live || bands.mode === 'none') return 0;
    let n = TIERS[tier].sprites;
    if (MODE === 'subtle') n = Math.min(bands.mobile ? 1 : 2, n);  // subtle: 2 (1 on mobile)
    if (REDUCED) n = Math.min(2, n);
    return n;
  }
  function reconcileSprites() {
    const want = desiredSprites();
    let active = 0;
    for (let i = 0; i < sprites.length; i++) if (!sprites[i].retiring && !sprites[i].dead) active++;
    if (active > want) {                            // too many: walk one off, fading
      for (let i = 0; i < sprites.length && active > want; i++) {
        const s = sprites[i];
        if (!s.retiring && !s.dead && !s.duty &&
            s.state !== 'react' && s.state !== 'celebrate') {
          beginState(s, 'exit'); active--;
        }
      }
    } else if (active < want && simNow >= nextSpawnAt) {
      spawnSprite(false);                           // staggered entrances
      nextSpawnAt = simNow + rand(1400, 2800);
    }
  }

  /* ── fixed-timestep loop: STEP=1000/60 accumulator, interpolated render —
   * identical motion at 60/90/120/144Hz.  dt clamped; hidden tab freezes the
   * clock (no fast-forward on return). ───────────────────────────────────── */
  let raf = 0, lastTs = 0, acc = 0;
  const costBuf = new Float32Array(60);
  let costIx = 0, costSum = 0, costN = 0, avgCost = 0, badSince = 0, goodSince = 0;
  function noteCost(c, ts) {
    costSum += c - costBuf[costIx];
    costBuf[costIx] = c; costIx = (costIx + 1) % 60;
    if (costN < 60) costN++;
    avgCost = costSum / costN;
    if (avgCost > DEGRADE_MS) {
      goodSince = 0;
      if (!badSince) badSince = ts;
      else if (ts - badSince > DEGRADE_AFTER && tier < TIERS.length - 1) {
        tier++; badSince = 0;                       // reconcile fades extras out
      }
    } else {
      badSince = 0;
      if (avgCost < RECOVER_MS) {
        if (!goodSince) goodSince = ts;
        else if (ts - goodSince > RECOVER_AFTER && tier > 0) { tier--; goodSince = 0; }
      } else goodSince = 0;
    }
  }
  function update(dt) {
    simNow += dt;
    if (layoutDirty) {
      const wasMode = bands.mode;
      evalBands();
      if (bands.mode !== 'none') {
        for (let i = 0; i < sprites.length; i++) {  // re-home into the new bands
          const s = sprites[i];
          if (s.duty) continue;                     // rooftop crew tracks the panel, not the bands
          if (bands.n === 1) s.bandIx = 0;
          clampToBand(s);
          if (wasMode !== bands.mode) { pickWaypoint(s); if (s.state === 'walk') s.walkRamp = 0; }
        }
      }
    }
    if (bands.mode === 'none') return;
    reconcileSprites();
    for (let i = sprites.length - 1; i >= 0; i--) {
      const s = sprites[i];
      updateSprite(s, dt);
      if (s.dead) {                                 // swap-pop: no splice allocation/shift
        sprites[i] = sprites[sprites.length - 1];
        sprites.pop();
        // downward loop: the element swapped in from the end was already updated
      }
    }
    if (!REDUCED) {
      carUpdate(dt);
      updateParticles(dt);
      updateReactions(dt);
      tetrisUpdate(dt);                            // geometry polling: logic rate,
    }                                              //   and only while a game is on
    bubbleUpdate(dt);                              // timers only — cheap, alloc-free
  }
  function render(alpha) {
    const g = ctx;
    if (!g) return;
    g.clearRect(0, 0, window.innerWidth, window.innerHeight);
    if (bands.n === 0) return;
    refreshTheme(performance.now());
    g.save();
    g.beginPath();                                  // ← the non-intrusion clip:
    g.rect(bands.a.x, bands.a.y, bands.a.w, bands.a.h);
    if (bands.n > 1) g.rect(bands.b.x, bands.b.y, bands.b.w, bands.b.h);
    if (tetris.active && tetris.pw > 0)             //   plus the tetris panel while a
      g.rect(tetris.px, tetris.py, tetris.pw, tetris.ph);   // game is on (spec'd)
    g.clip();                                       //   nothing draws outside these
    g.imageSmoothingEnabled = false;
    drawGlow(g);
    drawFlags(g);
    for (let i = 0; i < sprites.length; i++) drawSprite(g, sprites[i], alpha);
    carDraw(g, alpha);
    drawDyn(g);
    renderParticles(g, alpha);
    g.restore();
    drawBubbles(g);   // sanctioned overlay: viewport-clamped, dead-zone aware
  }
  function tick(ts) {
    raf = requestAnimationFrame(tick);
    let dt = ts - lastTs; lastTs = ts;
    if (dt < 0) dt = 0;
    if (dt > MAX_DT) dt = MAX_DT;
    acc += dt;
    const t0 = performance.now();
    let n = 0;
    while (acc >= STEP && n < MAX_STEPS) { update(STEP); acc -= STEP; n++; }
    if (n === MAX_STEPS) acc = 0;                   // spiral guard: drop the debt
    if (bands.mode === 'none') { suspendLive(); return; }
    render(acc / STEP);
    noteCost(performance.now() - t0, ts);
    if (DEBUG) updateDebug();
  }

  /* ── debug overlay (?gamedebug=1): frame ms, sprites, tier, rail widths ─── */
  let dbg = null, dbgAt = 0;
  function updateDebug() {
    const pn = performance.now();
    if (pn - dbgAt < 250) return;
    dbgAt = pn;
    if (!dbg) {
      dbg = document.createElement('div');
      dbg.setAttribute('aria-hidden', 'true');
      dbg.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:21;' +
        'font:10px/1.5 ui-monospace,monospace;color:#7ee2b8;background:rgba(9,30,66,.8);' +
        'padding:4px 8px;border-radius:6px;pointer-events:none;white-space:pre;';
      document.body.appendChild(dbg);
    }
    dbg.textContent = avgCost.toFixed(2) + 'ms · sprites ' + sprites.length +
      ' · parts ' + liveParts + ' · tier ' + TIERS[tier].name +
      ' · ' + bands.mode + (bands.mode === 'rails' ? ' ' + bands.railW + '|' + bands.railW : '') +
      ' · mode ' + MODE + (REDUCED ? ' (reduced)' : '') +
      (tetris.active ? ' · tetris(' + tetris.blasts + '💣)' : '');
  }
  function destroyDebug() { if (dbg && dbg.parentNode) dbg.parentNode.removeChild(dbg); dbg = null; }

  /* ── event bus — window only; detail is DATA, unknown types are ignored ─── */
  function onBus(e) {
    const d = e && e.detail;
    if (!d || typeof d.type !== 'string') return;
    intake(d.type, d.payload);
  }
  function onVis() {
    if (document.hidden) { if (raf) { cancelAnimationFrame(raf); raf = 0; } }
    else if (live && !raf) { lastTs = performance.now(); acc = 0; raf = requestAnimationFrame(tick); }
  }
  function onScroll() { layoutDirty = true; }       // rails track <main>'s live box
  function onResize() {
    if (!running) return;
    evalBands(); layoutDirty = true;
    sizeCanvas();
    syncLive();
  }
  let prmMq = null;
  function onPrm() {
    REDUCED = !!(prmMq && prmMq.matches);
    if (REDUCED) {
      resetEffects();
      removeInviteChip();                          // invites need motion — pull it
      if (tetris.active) endTetris(false);         // game keeps running; sprites bow out
    }
  }
  function resetEffects() {
    for (let i = 0; i < P_MAX; i++) { pool[i].on = false; freeList[i] = i; }
    freeTop = P_MAX; liveParts = 0;
    car.on = false; goal.phase = 0;
    flags[0].on = false; flags[1].on = false;
    glowRail = 0; glowScreen = 0;
    dyn.on = false;
    clearBubbles();
    for (let i = 0; i < 3; i++) rq[i].on = false;
    for (let i = 0; i < R_TYPES.length; i++) { pend[R_TYPES[i]].on = false; pend[R_TYPES[i]].count = 0; }
  }

  /* ── lifecycle: running (listeners) vs live (canvas + RAF).  setMode('off')
   * is a FULL teardown: RAF cancelled, pools cleared, canvas removed. ─────── */
  let ro = null;
  function resumeLive() {
    if (live || !running) return;
    if (bands.mode === 'none') return;
    buildAtlas();
    ensureCanvas();
    live = true;
    resetEffects();
    nextSpawnAt = simNow + 300;
    lastTs = performance.now(); acc = 0;
    if (!raf && !document.hidden) raf = requestAnimationFrame(tick);
  }
  function suspendLive() {
    if (!live) return;
    live = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    removeInviteChip();                             // teardown: chips gone,
    if (tetris.active) endTetris(true);             //   game closed, canvas z
    bodyTetrisClass(false);                         //   restored, toast class off,
    clearBubbles();                                 //   bubbles cleared
    destroyCanvas();
    destroyDebug();
    sprites.length = 0;
    resetEffects();
    tier = 0; costIx = 0; costSum = 0; costN = 0; avgCost = 0; badSince = 0; goodSince = 0;
  }
  function syncLive() {
    if (!running || bands.mode === 'none') suspendLive();
    else resumeLive();
  }
  function start() {
    if (MODE === 'off') return;                     // boot race: a pending start must not win
    if (running) return;
    running = true;
    try { COARSE = window.matchMedia('(hover: none)').matches; } catch (e) { COARSE = false; }
    try {
      prmMq = window.matchMedia('(prefers-reduced-motion: reduce)');
      prmMq.addEventListener('change', onPrm);
      REDUCED = prmMq.matches;
    } catch (e) { prmMq = null; }
    window.addEventListener('krab', onBus);
    window.addEventListener('resize', onResize);
    window.addEventListener('scroll', onScroll, { passive: true });
    // pointer events cover mouse AND touch; passive — preventDefault is never called
    window.addEventListener('pointermove', onMouse, { passive: true });
    window.addEventListener('pointerdown', onPointerDown, { passive: true });
    document.addEventListener('visibilitychange', onVis);
    try {
      const main = document.querySelector('main');
      if (main && window.ResizeObserver) {
        ro = new ResizeObserver(function () { layoutDirty = true; });
        ro.observe(main);
      }
    } catch (e) { ro = null; }
    evalBands();
    syncLive();
  }
  function stop() {
    if (!running) return;
    running = false;
    suspendLive();
    window.removeEventListener('krab', onBus);
    window.removeEventListener('resize', onResize);
    window.removeEventListener('scroll', onScroll);
    window.removeEventListener('pointermove', onMouse);
    window.removeEventListener('pointerdown', onPointerDown);
    document.removeEventListener('visibilitychange', onVis);
    if (prmMq) { try { prmMq.removeEventListener('change', onPrm); } catch (e) {} prmMq = null; }
    if (ro) { try { ro.disconnect(); } catch (e) {} ro = null; }
  }

  /* ── public API + persisted mode ────────────────────────────────────────── */
  let bootStart = null;                             // pending DOMContentLoaded start handler
  function readMode() {
    try {
      const v = localStorage.getItem('krab_game');
      return (v === 'subtle' || v === 'full' || v === 'off') ? v : 'off';
    } catch (e) { return 'off'; }                   // storage can throw; page unaffected
  }
  function saveMode(m) { try { localStorage.setItem('krab_game', m); } catch (e) {} }
  window.krabGame = {
    setMode: function (m) {
      if (m !== 'off' && m !== 'subtle' && m !== 'full') return MODE;
      MODE = m; saveMode(m);
      // ANY mode change (subtle <-> full included) pulls a live invite chip:
      // it was offered under the old mode's terms, and a 'full' -> 'subtle'
      // switch can retire the very sprite that is standing there asking
      removeInviteChip();
      if (m === 'off') {
        if (bootStart) {                            // cancel a still-pending boot start
          try { document.removeEventListener('DOMContentLoaded', bootStart); } catch (e) {}
          bootStart = null;
        }
        stop();                                     // -> suspendLive: chips, bubbles, z
        removeInviteChip();                         // and again unconditionally, for a
        if (tetris.active) endTetris(true);         //   game started while already off
        clearBubbles();
      }
      else if (!running) start();
      else { evalBands(); syncLive(); }
      return MODE;
    },
    mode: function () { return MODE; },
    celebrate: function () {                        // manual party button
      if (!live || REDUCED) return;
      const p = pend['deal.won'];
      p.on = true; p.count++;
      p.last = performance.now() - COALESCE_MS;     // flush on the next update
      lastEventAt = simNow;
    },
    playTetris: function () {                       // the page's voice command calls this
      // Documented NO-OP while the layer is off/suspended: the page enables a
      // mode FIRST (setMode('subtle'|'full')), and only then can a game open —
      // a panel with no sprite layer behind it is not this module's to own, and
      // nothing would restore the canvas z or the crew when it closed.
      // Returns whether a game is running as a result.
      if (!running || !live) return false;
      startTetris();
      return tetris.active;
    },
    say: function (text) {                          // a random sprite speaks the line.
      // Drawn via fillText so HTML is inert — still strip control chars and
      // clamp to 60 chars before it becomes a bubble.
      if (typeof text !== 'string' || !live) return false;
      let t = '';
      for (let i = 0; i < text.length && t.length < 60; i++) {
        const c = text.charCodeAt(i);
        if (c >= 32 && c !== 127 && (c < 0x80 || c > 0x9f)) t += text.charAt(i);
      }
      if (!t) return false;
      let s = null, n = 0;
      for (let i = 0; i < sprites.length; i++) {    // reservoir-pick an on-screen sprite
        const c = sprites[i];
        if (c.dead || c.retiring || c.alpha < 0.3) continue;
        n++;
        if (Math.random() < 1 / n) s = c;
      }
      return s ? bubbleSay(s, t) : false;
    },
  };

  /* ── boot — self-starting, but 'off' by default: first load does nothing
   * beyond installing the API and reading localStorage. ──────────────────── */
  MODE = readMode();
  if (MODE !== 'off') {
    if (document.readyState === 'loading') {
      bootStart = function () { bootStart = null; start(); };
      document.addEventListener('DOMContentLoaded', bootStart, { once: true });
    } else start();
  }
})();

'''

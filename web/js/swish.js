/**
 * Swish web — half court, sheets, localStorage
 */
(function () {
  "use strict";

  const STORAGE_KEY = "swish_web_v1";
  const COURT_X0 = -25,
    COURT_X1 = 25,
    COURT_Y0 = 0,
    COURT_Y1 = 47;
  const HOOP_X = 0,
    HOOP_Y = 5.25;
  const THREE_R = 23.75;
  const THREE_LINE_INSET = 3;
  const THREE_JOIN_X = 25 - THREE_LINE_INSET;
  const RESTRICTED_R = 4;
  const FT_CIRCLE_R = 6;
  const PAINT_X = 8;
  const FT_Y = 19;
  const COURT_W = 560;
  const COURT_H = Math.round((COURT_W * (COURT_Y1 - COURT_Y0)) / (COURT_X1 - COURT_X0));
  const PICK_JUMP = 3;
  const PICK_LAYUP = 4;
  const SHEET_CARD_W = 260;
  const SHEETS_PER_PAGE = 9;

  let data = { sheets: ["Practice", "Drills", "Game prep"], shots: [] };
  let nextId = 1;
  let activeSheet = null;
  let pendingJump = null;
  let inspectId = null;
  let layupDraft = [];
  /** 0-based page index for the home sheet grid (3×3). */
  let sheetGridPage = 0;
  /** Loaded from `assets/halfcourt.png` (same artwork as Streamlit); falls back to vector court. */
  let courtBgImg = null;

  function feetToPixel(x, y, w, h) {
    const px = ((x - COURT_X0) / (COURT_X1 - COURT_X0)) * w;
    const py = ((COURT_Y1 - y) / (COURT_Y1 - COURT_Y0)) * h;
    return [px, py];
  }

  function pixelToCourt(px, py, w, h) {
    let x = COURT_X0 + (px / w) * (COURT_X1 - COURT_X0);
    let y = COURT_Y1 - (py / h) * (COURT_Y1 - COURT_Y0);
    x = Math.max(COURT_X0, Math.min(COURT_X1, x));
    y = Math.max(COURT_Y0, Math.min(COURT_Y1, y));
    return [x, y];
  }

  function todayISO() {
    return new Date().toISOString().slice(0, 10);
  }

  function load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const p = JSON.parse(raw);
        if (Array.isArray(p.sheets) && p.sheets.length) data.sheets = p.sheets;
        if (Array.isArray(p.shots)) data.shots = p.shots;
        if (typeof p.nextId === "number") nextId = p.nextId;
      }
    } catch (e) {}
    data.shots.forEach((s) => {
      if (s.id >= nextId) nextId = s.id + 1;
    });
  }

  function save() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          sheets: data.sheets,
          shots: data.shots,
          nextId,
        })
      );
    } catch (e) {
      console.warn("Swish: could not save to localStorage", e);
    }
  }

  function shotsTodayForSheet(sheet) {
    const t = todayISO();
    return data.shots.filter(
      (s) => s.session_name === sheet && s.created && s.created.slice(0, 10) === t
    );
  }

  function distSeg(px, py, ax, ay, bx, by) {
    const vx = bx - ax,
      vy = by - ay;
    const wx = px - ax,
      wy = py - ay;
    const c2 = vx * vx + vy * vy;
    if (c2 < 1e-12) return Math.hypot(px - ax, py - ay);
    const t = Math.max(0, Math.min(1, (wx * vx + wy * vy) / c2));
    const qx = ax + t * vx,
      qy = ay + t * vy;
    return Math.hypot(px - qx, py - qy);
  }

  function distToPolyline(cx, cy, pts) {
    if (pts.length < 2) return 1e9;
    let best = Math.hypot(cx - pts[pts.length - 1][0], cy - pts[pts.length - 1][1]);
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i],
        b = pts[i + 1];
      best = Math.min(best, distSeg(cx, cy, a[0], a[1], b[0], b[1]));
    }
    return best;
  }

  function findShotNearCourt(todayShots, cx, cy) {
    let best = null,
      bestD = 1e9;
    for (const s of todayShots) {
      if (s.shot_kind === "layup") {
        const path = s.layup_path;
        if (!path || path.length < 2) continue;
        const pts = path.map((p) => [+p[0], +p[1]]);
        const d = distToPolyline(cx, cy, pts);
        if (d <= PICK_LAYUP && d < bestD) {
          bestD = d;
          best = s;
        }
      } else {
        const sx = Number(s.court_x),
          sy = Number(s.court_y);
        if (!Number.isFinite(sx) || !Number.isFinite(sy)) continue;
        const d = Math.hypot(cx - sx, cy - sy);
        if (d <= PICK_JUMP && d < bestD) {
          bestD = d;
          best = s;
        }
      }
    }
    return best;
  }

  function drawCourtBackground(ctx, w, h) {
    if (
      courtBgImg &&
      courtBgImg.complete &&
      courtBgImg.naturalWidth > 0
    ) {
      ctx.drawImage(courtBgImg, 0, 0, w, h);
      return;
    }
    const floor = "#1a2f4a";
    ctx.fillStyle = floor;
    ctx.fillRect(0, 0, w, h);

    ctx.fillStyle = "rgba(135, 206, 250, 0.88)";
    ctx.beginPath();
    const p1 = feetToPixel(-PAINT_X, COURT_Y0, w, h);
    const p2 = feetToPixel(PAINT_X, COURT_Y0, w, h);
    const p3 = feetToPixel(PAINT_X, FT_Y, w, h);
    const p4 = feetToPixel(-PAINT_X, FT_Y, w, h);
    ctx.moveTo(p1[0], p1[1]);
    ctx.lineTo(p2[0], p2[1]);
    ctx.lineTo(p3[0], p3[1]);
    ctx.lineTo(p4[0], p4[1]);
    ctx.closePath();
    ctx.fill();

    const rimOrange = "#ff6b2d";
    const lineWhite = "#e8eef5";
    ctx.strokeStyle = lineWhite;
    ctx.lineWidth = Math.max(1, w / 280);
    const line = (x0, y0, x1, y1) => {
      const a = feetToPixel(x0, y0, w, h);
      const b = feetToPixel(x1, y1, w, h);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    };

    line(-25, 0, 25, 0);
    line(-25, 0, -25, 47);
    line(25, 0, 25, 47);
    line(-25, 47, 25, 47);

    line(-PAINT_X, COURT_Y0, PAINT_X, COURT_Y0);
    line(PAINT_X, COURT_Y0, PAINT_X, FT_Y);
    line(-PAINT_X, FT_Y, PAINT_X, FT_Y);
    line(-PAINT_X, COURT_Y0, -PAINT_X, FT_Y);
    line(-6, FT_Y, 6, FT_Y);

    ctx.strokeStyle = lineWhite;
    ctx.lineWidth = Math.max(2, w / 240);
    ctx.beginPath();
    for (let i = 0; i <= 64; i++) {
      const t = (i / 64) * Math.PI * 2;
      const [px, py] = feetToPixel(
        FT_CIRCLE_R * Math.cos(t),
        FT_Y + FT_CIRCLE_R * Math.sin(t),
        w,
        h
      );
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.stroke();

    ctx.lineWidth = Math.max(1.5, w / 300);
    ctx.beginPath();
    for (let i = 0; i <= 32; i++) {
      const t = Math.PI + (i / 32) * Math.PI;
      const [px, py] = feetToPixel(
        RESTRICTED_R * Math.cos(t),
        HOOP_Y + RESTRICTED_R * Math.sin(t),
        w,
        h
      );
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.stroke();

    const hx = HOOP_X,
      hy = HOOP_Y;
    ctx.strokeStyle = rimOrange;
    ctx.lineWidth = Math.max(2, w / 200);
    ctx.beginPath();
    const jx = THREE_JOIN_X;
    const [pR0, pR1] = [feetToPixel(25, 0, w, h), feetToPixel(jx, 0, w, h)];
    ctx.moveTo(pR0[0], pR0[1]);
    ctx.lineTo(pR1[0], pR1[1]);
    for (let i = 0; i <= 72; i++) {
      const xv = jx + (i / 72) * (-2 * jx);
      const d = THREE_R * THREE_R - xv * xv;
      if (d < 0) continue;
      const yv = hy + Math.sqrt(d);
      const [px, py] = feetToPixel(xv, yv, w, h);
      ctx.lineTo(px, py);
    }
    const [pL1, pL0] = [feetToPixel(-jx, 0, w, h), feetToPixel(-25, 0, w, h)];
    ctx.lineTo(pL1[0], pL1[1]);
    ctx.lineTo(pL0[0], pL0[1]);
    ctx.stroke();

    const RIM_R_FT = 0.75;
    const [rimx, rimy] = feetToPixel(HOOP_X, HOOP_Y, w, h);
    const rimPx = (RIM_R_FT / (COURT_X1 - COURT_X0)) * w;
    ctx.beginPath();
    ctx.arc(rimx, rimy, Math.max(3, rimPx), 0, Math.PI * 2);
    ctx.strokeStyle = rimOrange;
    ctx.lineWidth = Math.max(2, w / 220);
    ctx.stroke();

    ctx.fillStyle = "#b8c9dc";
    ctx.font = `${Math.round(11 * (w / COURT_W))}px sans-serif`;
    ctx.fillText("Half court", w / 2 - 28, 14);
  }

  function drawJumpMarker(ctx, px, py, kind) {
    const r = (13 * ctx.canvas.width) / COURT_W;
    if (kind === "made") {
      ctx.fillStyle = "rgba(0,0,0,0.22)";
      ctx.beginPath();
      ctx.arc(px + 2, py + 2, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 3;
      ctx.fillStyle = "rgba(34,197,94,0.97)";
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    } else if (kind === "miss") {
      ctx.fillStyle = "rgba(0,0,0,0.22)";
      ctx.beginPath();
      ctx.arc(px + 2, py + 2, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 3;
      ctx.fillStyle = "rgba(239,68,68,0.97)";
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    } else {
      const ro = (17 * ctx.canvas.width) / COURT_W;
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.arc(px, py, ro, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "rgba(253,224,71,0.95)";
      const ri = (11 * ctx.canvas.width) / COURT_W;
      ctx.beginPath();
      ctx.arc(px, py, ri, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawInspectRing(ctx, shot, w, h) {
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 5;
    if (shot.shot_kind === "layup" && shot.layup_path && shot.layup_path.length >= 2) {
      const pix = shot.layup_path.map((p) =>
        feetToPixel(+p[0], +p[1], w, h)
      );
      for (let i = 0; i < pix.length - 1; i++) {
        ctx.beginPath();
        ctx.moveTo(pix[i][0], pix[i][1]);
        ctx.lineTo(pix[i + 1][0], pix[i + 1][1]);
        ctx.stroke();
      }
      return;
    }
    const ix = Number(shot.court_x),
      iy = Number(shot.court_y);
    if (Number.isFinite(ix) && Number.isFinite(iy)) {
      const [px, py] = feetToPixel(ix, iy, w, h);
      ctx.beginPath();
      ctx.arc(px, py, 24 * (w / COURT_W), 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = "rgba(255,255,255,0.85)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(px, py, 28 * (w / COURT_W), 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function drawLayupEndpointDots(ctx, pix, w, kind) {
    if (!pix || pix.length < 2) return;
    const r = Math.max(5, 6 * (w / COURT_W));
    const lw = Math.max(1.5, 2 * (w / COURT_W));
    const start = pix[0];
    const end = pix[pix.length - 1];
    const isMade = kind === "made";
    const startFill = isMade
      ? "rgba(220,255,235,0.98)"
      : "rgba(255,220,220,0.98)";
    const endFill = isMade
      ? "rgba(34,197,94,0.98)"
      : "rgba(239,68,68,0.98)";
    ctx.strokeStyle = "rgba(255,255,255,0.92)";
    ctx.lineWidth = lw;
    ctx.fillStyle = startFill;
    ctx.beginPath();
    ctx.arc(start[0], start[1], r, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = endFill;
    ctx.beginPath();
    ctx.arc(end[0], end[1], r, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }

  function drawShotMap(
    ctx,
    w,
    h,
    todayShots,
    pending,
    inspectShot,
    draftLayup
  ) {
    drawCourtBackground(ctx, w, h);

    for (const s of todayShots) {
      if (s.shot_kind === "layup" && s.layup_path && s.layup_path.length >= 2) {
        const col = s.result === "made" ? "rgba(40,140,90,0.95)" : "rgba(200,55,55,0.95)";
        ctx.strokeStyle = col;
        ctx.lineWidth = 5 * (w / COURT_W);
        const pix = s.layup_path.map((p) =>
          feetToPixel(+p[0], +p[1], w, h)
        );
        ctx.beginPath();
        ctx.moveTo(pix[0][0], pix[0][1]);
        for (let i = 1; i < pix.length; i++) ctx.lineTo(pix[i][0], pix[i][1]);
        ctx.stroke();
        drawLayupEndpointDots(
          ctx,
          pix,
          w,
          s.result === "made" ? "made" : "miss"
        );
      }
    }

    for (const s of todayShots) {
      if (s.shot_kind === "layup") continue;
      const jx = Number(s.court_x),
        jy = Number(s.court_y);
      if (!Number.isFinite(jx) || !Number.isFinite(jy)) continue;
      const [px, py] = feetToPixel(jx, jy, w, h);
      drawJumpMarker(ctx, px, py, s.result === "made" ? "made" : "miss");
    }

    if (pending) {
      const [px, py] = feetToPixel(pending[0], pending[1], w, h);
      drawJumpMarker(ctx, px, py, "pending");
    }

    if (draftLayup && draftLayup.length >= 2) {
      const pix = draftLayup.map((p) => feetToPixel(p[0], p[1], w, h));
      ctx.strokeStyle = "rgba(250,204,21,0.98)";
      ctx.lineWidth = 8 * (w / COURT_W);
      ctx.beginPath();
      ctx.moveTo(pix[0][0], pix[0][1]);
      for (let i = 1; i < pix.length; i++) ctx.lineTo(pix[i][0], pix[i][1]);
      ctx.stroke();
      ctx.strokeStyle = "rgba(255,255,255,0.75)";
      ctx.lineWidth = 3 * (w / COURT_W);
      ctx.beginPath();
      ctx.moveTo(pix[0][0], pix[0][1]);
      for (let i = 1; i < pix.length; i++) ctx.lineTo(pix[i][0], pix[i][1]);
      ctx.stroke();
      const r = Math.max(5, 6 * (w / COURT_W));
      const lw = Math.max(1.5, 2 * (w / COURT_W));
      const start = pix[0];
      const end = pix[pix.length - 1];
      ctx.strokeStyle = "rgba(251,191,36,0.98)";
      ctx.lineWidth = lw;
      ctx.fillStyle = "rgba(255,255,255,0.95)";
      ctx.beginPath();
      ctx.arc(start[0], start[1], r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "rgba(250,204,21,0.98)";
      ctx.beginPath();
      ctx.arc(end[0], end[1], r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }

    if (inspectShot) drawInspectRing(ctx, inspectShot, w, h);
  }

  function formatShot(s) {
    const t = s.created ? s.created.slice(11, 19) : "?";
    const res = (s.result || "—").toString().toUpperCase();
    if (s.shot_kind === "layup") {
      const n = (s.layup_path && s.layup_path.length) || 0;
      return `${res} layup · ${n} pts · ${t}`;
    }
    const jx = Number(s.court_x),
      jy = Number(s.court_y);
    if (Number.isFinite(jx) && Number.isFinite(jy)) {
      return `${res} jump · (${jx.toFixed(1)}, ${jy.toFixed(1)}) ft · ${t}`;
    }
    return `${res} jump · ${t}`;
  }

  function setDocumentTitle() {
    document.title = activeSheet
      ? `Swish · ${activeSheet}`
      : "Swish — Basketball tracker";
  }

  function sessionSkillsHtml(today) {
    const jumps = today.filter((s) => s.shot_kind !== "layup");
    const lays = today.filter((s) => s.shot_kind === "layup");
    const jm = jumps.filter((s) => s.result === "made").length;
    const jt = jumps.length;
    const lm = lays.filter((s) => s.result === "made").length;
    const lt = lays.length;
    const jp = jt ? ((100 * jm) / jt).toFixed(0) : "—";
    const lp = lt ? ((100 * lm) / lt).toFixed(0) : "—";
    return `This sheet today — Jump <strong>${jm}/${jt}</strong> (${jp}%) · Layup <strong>${lm}/${lt}</strong> (${lp}%) · Total <strong>${today.length}</strong>`;
  }

  function shotsAllToday() {
    const t = todayISO();
    return data.shots.filter(
      (s) => s.created && s.created.slice(0, 10) === t
    );
  }

  function renderHomeRecent() {
    const el = document.getElementById("home-recent");
    const btn = document.getElementById("btn-reset-all-today");
    if (!el) return;
    const list = shotsAllToday()
      .slice()
      .sort(
        (a, b) =>
          new Date(b.created).getTime() - new Date(a.created).getTime()
      )
      .slice(0, 15);
    el.innerHTML = "";
    if (!list.length) {
      const li = document.createElement("li");
      li.className = "shot-list-empty";
      li.textContent = "No shots logged yet today.";
      el.appendChild(li);
      if (btn) btn.disabled = true;
      return;
    }
    if (btn) btn.disabled = false;
    list.forEach((s) => {
      const li = document.createElement("li");
      const sheet = s.session_name || "—";
      li.textContent = `${formatShot(s)} · ${sheet}`;
      el.appendChild(li);
    });
  }

  function renderHomeMetrics() {
    const today = shotsAllToday();
    const made = today.filter((s) => s.result === "made").length;
    const missed = today.filter((s) => s.result === "missed").length;
    const total = made + missed;
    const pct = total ? Math.round((100 * made) / total) : 0;
    document.getElementById("home-metrics").innerHTML = `
      <div class="metric"><label>Shots today</label><span class="val">${total}</span></div>
      <div class="metric"><label>Made</label><span class="val">${made}</span></div>
      <div class="metric"><label>Missed</label><span class="val">${missed}</span></div>
      <div class="metric"><label>Accuracy</label><span class="val">${total ? pct + "%" : "—"}</span></div>`;
  }

  function clampSheetGridPage() {
    const n = data.sheets.length;
    if (n === 0) return;
    const pages = Math.max(1, Math.ceil(n / SHEETS_PER_PAGE));
    if (sheetGridPage >= pages) sheetGridPage = pages - 1;
    if (sheetGridPage < 0) sheetGridPage = 0;
  }

  function setSheetOverflowOpen(open) {
    const panel = document.getElementById("sheet-overflow-panel");
    const btn = document.getElementById("btn-sheet-overflow");
    if (!panel || !btn) return;
    panel.classList.toggle("hidden", !open);
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function buildSheetOverflowPanel() {
    const panel = document.getElementById("sheet-overflow-panel");
    if (!panel) return;
    panel.innerHTML = "";
    const label = document.createElement("div");
    label.className = "sheet-overflow-label";
    label.textContent = "All sheets";
    panel.appendChild(label);
    data.sheets.forEach((name, idx) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.textContent = name;
      b.addEventListener("click", () => {
        setSheetOverflowOpen(false);
        sheetGridPage = Math.floor(idx / SHEETS_PER_PAGE);
        openSession(name);
      });
      panel.appendChild(b);
    });
  }

  function renderSheetGrid() {
    clampSheetGridPage();
    const grid = document.getElementById("sheet-grid");
    const footer = document.getElementById("sheet-grid-footer");
    grid.innerHTML = "";
    const cardH = Math.round(
      (SHEET_CARD_W * (COURT_Y1 - COURT_Y0)) / (COURT_X1 - COURT_X0)
    );
    const total = data.sheets.length;
    const pages = Math.max(1, Math.ceil(total / SHEETS_PER_PAGE));
    const start = sheetGridPage * SHEETS_PER_PAGE;
    const pageSheets = data.sheets.slice(start, start + SHEETS_PER_PAGE);

    pageSheets.forEach((sheet) => {
      const sub = shotsTodayForSheet(sheet);
      const made = sub.filter((s) => s.result === "made").length;
      const miss = sub.filter((s) => s.result === "missed").length;
      const tot = made + miss;
      const subline = tot
        ? `${tot} shots today · ${Math.round((100 * made) / tot)}% made`
        : "Tap to open";

      const card = document.createElement("div");
      card.className = "sheet-card";
      card.innerHTML = `<h3>${escapeHtml(sheet)}</h3><div class="sub">${escapeHtml(
        subline
      )}</div>`;
      const cv = document.createElement("canvas");
      cv.width = SHEET_CARD_W;
      cv.height = cardH;
      const ctx = cv.getContext("2d");
      drawShotMap(ctx, SHEET_CARD_W, cardH, sub, null, null, null);
      card.appendChild(cv);
      card.addEventListener("click", () => openSession(sheet));
      grid.appendChild(card);
    });

    if (!footer) return;
    if (total <= SHEETS_PER_PAGE) {
      footer.classList.add("hidden");
      setSheetOverflowOpen(false);
      return;
    }
    footer.classList.remove("hidden");
    const hint = document.getElementById("sheet-grid-hint");
    if (hint) {
      hint.textContent = `Page ${sheetGridPage + 1} of ${pages} · ${total} sheets`;
    }
    const prev = document.getElementById("btn-sheet-prev");
    const next = document.getElementById("btn-sheet-next");
    if (prev) prev.disabled = sheetGridPage <= 0;
    if (next) next.disabled = sheetGridPage >= pages - 1;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function renderSheetList() {
    const ul = document.getElementById("sheet-list");
    ul.innerHTML = "";
    data.sheets.forEach((name) => {
      const li = document.createElement("li");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "link";
      open.textContent = name;
      open.addEventListener("click", () => openSession(name));
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "danger";
      rm.textContent = "×";
      rm.title = "Remove sheet";
      rm.addEventListener("click", (e) => {
        e.stopPropagation();
        removeSheet(name);
      });
      li.appendChild(open);
      li.appendChild(rm);
      ul.appendChild(li);
    });
  }

  function removeSheet(name) {
    if (data.sheets.length <= 1) return;
    if (!confirm(`Remove sheet “${name}”?`)) return;
    data.sheets = data.sheets.filter((s) => s !== name);
    data.shots = data.shots.filter((s) => s.session_name !== name);
    if (activeSheet === name) {
      activeSheet = null;
      showHome();
    }
    save();
    refreshAll();
  }

  function showHome() {
    activeSheet = null;
    document.getElementById("view-home").classList.add("active");
    document.getElementById("view-session").classList.remove("active");
    setDocumentTitle();
    renderHomeMetrics();
    renderHomeRecent();
    renderSheetGrid();
    renderSheetList();
  }

  function openSession(sheet) {
    activeSheet = sheet;
    pendingJump = null;
    inspectId = null;
    layupDraft = [];
    document.getElementById("view-home").classList.remove("active");
    document.getElementById("view-session").classList.add("active");
    document.getElementById("session-title").textContent = "Session · " + sheet;
    document.querySelector('input[name="mode"][value="jump"]').checked = true;
    setDocumentTitle();
    syncModeUI();
    refreshSession();
  }

  function syncModeUI() {
    const jump = document.querySelector('input[name="mode"][value="jump"]').checked;
    document.querySelectorAll("#mode-toggle label").forEach((lab) => {
      const inp = lab.querySelector('input[name="mode"]');
      lab.classList.toggle("is-on", !!(inp && inp.checked));
    });
    document.getElementById("jump-panel").classList.toggle("hidden", !jump);
    document.getElementById("layup-panel").classList.toggle("hidden", jump);
    refreshSession();
  }

  function refreshSession() {
    if (!activeSheet) return;
    const today = shotsTodayForSheet(activeSheet);
    const made = today.filter((s) => s.result === "made").length;
    const missed = today.filter((s) => s.result === "missed").length;
    const total = made + missed;
    const pct = total ? ((100 * made) / total).toFixed(1) : "—";
    document.getElementById("session-metrics").innerHTML = `
      <div class="metric"><label>Made</label><span class="val">${made}</span></div>
      <div class="metric"><label>Missed</label><span class="val">${missed}</span></div>
      <div class="metric"><label>FG%</label><span class="val">${pct === "—" ? "—" : pct + "%"}</span></div>`;

    const sk = document.getElementById("session-skills");
    if (sk) sk.innerHTML = sessionSkillsHtml(today);

    const undoBtn = document.getElementById("btn-undo");
    if (undoBtn) undoBtn.disabled = today.length === 0;
    const resetBtn = document.getElementById("btn-reset");
    if (resetBtn) resetBtn.disabled = today.length === 0;

    const inspectShot =
      inspectId != null
        ? today.find(
            (s) => s.id === inspectId || String(s.id) === String(inspectId)
          )
        : null;
    const banner = document.getElementById("inspect-banner");
    const bannerWrap = document.getElementById("inspect-banner-wrap");
    if (inspectShot && banner && bannerWrap) {
      banner.textContent = "Selected shot · " + formatShot(inspectShot);
      bannerWrap.classList.remove("hidden");
    } else if (bannerWrap) {
      bannerWrap.classList.add("hidden");
    }

    const jumpCv = document.getElementById("main-court");
    jumpCv.width = COURT_W;
    jumpCv.height = COURT_H;
    const jctx = jumpCv.getContext("2d");
    drawShotMap(
      jctx,
      COURT_W,
      COURT_H,
      today,
      pendingJump,
      inspectShot,
      null
    );

    const layCv = document.getElementById("layup-court");
    layCv.width = COURT_W;
    layCv.height = COURT_H;
    const lctx = layCv.getContext("2d");
    drawShotMap(
      lctx,
      COURT_W,
      COURT_H,
      today,
      null,
      null,
      layupDraft.length >= 2 ? layupDraft : null
    );

    const hasPending = pendingJump != null;
    document.getElementById("btn-made").disabled = !hasPending;
    document.getElementById("btn-miss").disabled = !hasPending;

    const layupLen = pathLengthFt(layupDraft);
    const hasLayup = layupDraft.length >= 2 && layupLen >= 1;
    document.getElementById("btn-lay-made").disabled = !hasLayup;
    document.getElementById("btn-lay-miss").disabled = !hasLayup;

    const ul = document.getElementById("session-shots");
    ul.innerHTML = "";
    today
      .slice()
      .reverse()
      .slice(0, 12)
      .forEach((s) => {
        const li = document.createElement("li");
        li.textContent = formatShot(s);
        ul.appendChild(li);
      });
  }

  function pathLengthFt(pts) {
    let s = 0;
    for (let i = 1; i < pts.length; i++) {
      s += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    }
    return s;
  }

  function addShot(rec) {
    rec.id = nextId++;
    rec.created = new Date().toISOString();
    if (rec.court_x != null) rec.court_x = Number(rec.court_x);
    if (rec.court_y != null) rec.court_y = Number(rec.court_y);
    if (Array.isArray(rec.layup_path)) {
      rec.layup_path = rec.layup_path.map((p) => [
        Number(p[0]),
        Number(p[1]),
      ]);
    }
    data.shots.push(rec);
    save();
  }

  function refreshAll() {
    renderHomeMetrics();
    renderHomeRecent();
    renderSheetGrid();
    renderSheetList();
    if (activeSheet) refreshSession();
  }

  function resetAllToday() {
    const t = todayISO();
    const onDay = data.shots.filter(
      (s) => s.created && s.created.slice(0, 10) === t
    );
    if (!onDay.length) return;
    if (
      !confirm(
        `Remove all ${onDay.length} shot(s) from every sheet for today? This cannot be undone.`
      )
    ) {
      return;
    }
    data.shots = data.shots.filter(
      (s) => !s.created || s.created.slice(0, 10) !== t
    );
    save();
    inspectId = null;
    pendingJump = null;
    layupDraft = [];
    refreshAll();
  }

  function downloadActiveCourtPng() {
    const jump = document.querySelector(
      'input[name="mode"][value="jump"]'
    ).checked;
    const cv = document.getElementById(jump ? "main-court" : "layup-court");
    if (!cv || !activeSheet) return;
    const safe = activeSheet.replace(/[^\w\s\-]/g, "").trim().replace(/\s+/g, "_");
    const tag = jump ? "jump" : "layup";
    const url = cv.toDataURL("image/png");
    const a = document.createElement("a");
    a.href = url;
    a.download = `swish_${safe || "court"}_${tag}_preview.png`;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function undoLastShot() {
    if (!activeSheet) return;
    const t = todayISO();
    const candidates = data.shots.filter(
      (s) =>
        s.session_name === activeSheet &&
        s.created &&
        s.created.slice(0, 10) === t
    );
    if (!candidates.length) return;
    candidates.sort(
      (a, b) => new Date(b.created).getTime() - new Date(a.created).getTime()
    );
    const rid = candidates[0].id;
    data.shots = data.shots.filter((s) => s.id !== rid);
    save();
    inspectId = null;
    pendingJump = null;
    refreshAll();
  }

  function resetSheetToday() {
    if (!activeSheet) return;
    const t = todayISO();
    const onSheet = data.shots.filter(
      (s) =>
        s.session_name === activeSheet &&
        s.created &&
        s.created.slice(0, 10) === t
    );
    if (!onSheet.length) return;
    const msg = `Reset this sheet: remove all ${onSheet.length} shot(s) today on “${activeSheet}”? This cannot be undone.`;
    if (!confirm(msg)) return;
    data.shots = data.shots.filter(
      (s) =>
        !(
          s.session_name === activeSheet &&
          s.created &&
          s.created.slice(0, 10) === t
        )
    );
    save();
    inspectId = null;
    pendingJump = null;
    layupDraft = [];
    refreshAll();
  }

  function onJumpCourtPointer(ev) {
    if (!activeSheet) return;
    if (ev.pointerType === "mouse" && ev.button !== 0) return;
    ev.preventDefault();
    const cv = document.getElementById("main-court");
    const r = cv.getBoundingClientRect();
    const sx = cv.width / r.width;
    const sy = cv.height / r.height;
    const px = (ev.clientX - r.left) * sx;
    const py = (ev.clientY - r.top) * sy;
    const [cx, cy] = pixelToCourt(px, py, cv.width, cv.height);
    const today = shotsTodayForSheet(activeSheet);
    const hit = findShotNearCourt(today, cx, cy);
    if (hit) {
      inspectId = hit.id;
      pendingJump = null;
    } else {
      inspectId = null;
      pendingJump = [cx, cy];
    }
    refreshSession();
  }

  function bindLayupCanvas() {
    const cv = document.getElementById("layup-court");
    let drawing = false;
    const addPt = (clientX, clientY) => {
      const r = cv.getBoundingClientRect();
      const sx = cv.width / r.width;
      const sy = cv.height / r.height;
      const px = (clientX - r.left) * sx;
      const py = (clientY - r.top) * sy;
      const next = pixelToCourt(px, py, cv.width, cv.height);
      if (layupDraft.length) {
        const last = layupDraft[layupDraft.length - 1];
        if (Math.hypot(next[0] - last[0], next[1] - last[1]) < 0.35) return;
      }
      layupDraft.push(next);
      refreshSession();
    };

    function endStroke(e) {
      drawing = false;
      if (e && e.pointerId != null) {
        try {
          cv.releasePointerCapture(e.pointerId);
        } catch (err) {}
      }
    }

    cv.addEventListener(
      "pointerdown",
      (e) => {
        if (!activeSheet) return;
        if (e.pointerType === "mouse" && e.button !== 0) return;
        e.preventDefault();
        drawing = true;
        layupDraft = [];
        try {
          cv.setPointerCapture(e.pointerId);
        } catch (err) {}
        addPt(e.clientX, e.clientY);
      },
      { passive: false }
    );
    cv.addEventListener(
      "pointermove",
      (e) => {
        if (!drawing) return;
        e.preventDefault();
        addPt(e.clientX, e.clientY);
      },
      { passive: false }
    );
    cv.addEventListener("pointerup", endStroke);
    cv.addEventListener("pointercancel", endStroke);
  }

  function init() {
    load();
    document.getElementById("add-sheet-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const inp = document.getElementById("new-sheet-name");
      const name = (inp.value || "").trim();
      if (!name || data.sheets.includes(name)) return;
      data.sheets.push(name);
      sheetGridPage = Math.floor(
        (data.sheets.length - 1) / SHEETS_PER_PAGE
      );
      inp.value = "";
      save();
      refreshAll();
    });
    document.getElementById("btn-back").addEventListener("click", showHome);
    document
      .getElementById("btn-download-court")
      .addEventListener("click", downloadActiveCourtPng);
    document.getElementById("btn-undo").addEventListener("click", undoLastShot);
    document.getElementById("btn-reset").addEventListener("click", resetSheetToday);
    document
      .getElementById("btn-reset-all-today")
      .addEventListener("click", resetAllToday);
    document.getElementById("btn-clear-inspect").addEventListener("click", () => {
      inspectId = null;
      refreshSession();
    });
    document.querySelectorAll('input[name="mode"]').forEach((el) => {
      el.addEventListener("change", syncModeUI);
    });
    document
      .getElementById("main-court")
      .addEventListener("pointerdown", onJumpCourtPointer, { passive: false });
    document.getElementById("btn-clear-mark").addEventListener("click", () => {
      pendingJump = null;
      inspectId = null;
      refreshSession();
    });
    document.getElementById("btn-made").addEventListener("click", () => {
      if (!activeSheet || !pendingJump) return;
      addShot({
        session_name: activeSheet,
        result: "made",
        shot_kind: "jump",
        court_x: pendingJump[0],
        court_y: pendingJump[1],
      });
      pendingJump = null;
      inspectId = null;
      refreshAll();
    });
    document.getElementById("btn-miss").addEventListener("click", () => {
      if (!activeSheet || !pendingJump) return;
      addShot({
        session_name: activeSheet,
        result: "missed",
        shot_kind: "jump",
        court_x: pendingJump[0],
        court_y: pendingJump[1],
      });
      pendingJump = null;
      inspectId = null;
      refreshAll();
    });
    document.getElementById("btn-clear-layup").addEventListener("click", () => {
      layupDraft = [];
      refreshSession();
    });
    document.getElementById("btn-lay-made").addEventListener("click", () => {
      if (!activeSheet || layupDraft.length < 2) return;
      const last = layupDraft[layupDraft.length - 1];
      addShot({
        session_name: activeSheet,
        result: "made",
        shot_kind: "layup",
        layup_path: layupDraft.map((p) => [p[0], p[1]]),
        court_x: last[0],
        court_y: last[1],
      });
      layupDraft = [];
      refreshAll();
    });
    document.getElementById("btn-lay-miss").addEventListener("click", () => {
      if (!activeSheet || layupDraft.length < 2) return;
      const last = layupDraft[layupDraft.length - 1];
      addShot({
        session_name: activeSheet,
        result: "missed",
        shot_kind: "layup",
        layup_path: layupDraft.map((p) => [p[0], p[1]]),
        court_x: last[0],
        court_y: last[1],
      });
      layupDraft = [];
      refreshAll();
    });
    bindLayupCanvas();

    document.getElementById("btn-sheet-prev").addEventListener("click", () => {
      sheetGridPage = Math.max(0, sheetGridPage - 1);
      setSheetOverflowOpen(false);
      renderSheetGrid();
    });
    document.getElementById("btn-sheet-next").addEventListener("click", () => {
      const pages = Math.max(
        1,
        Math.ceil(data.sheets.length / SHEETS_PER_PAGE)
      );
      sheetGridPage = Math.min(pages - 1, sheetGridPage + 1);
      setSheetOverflowOpen(false);
      renderSheetGrid();
    });
    document.getElementById("btn-sheet-overflow").addEventListener("click", () => {
      const panel = document.getElementById("sheet-overflow-panel");
      if (!panel) return;
      const isOpen = !panel.classList.contains("hidden");
      if (isOpen) {
        setSheetOverflowOpen(false);
      } else {
        buildSheetOverflowPanel();
        setSheetOverflowOpen(true);
      }
    });
    document.addEventListener("click", (e) => {
      if (e.target.closest(".sheet-overflow-wrap")) return;
      setSheetOverflowOpen(false);
    });

    setDocumentTitle();
    const img = new Image();
    img.onload = () => {
      courtBgImg = img;
      showHome();
    };
    img.onerror = () => {
      courtBgImg = null;
      showHome();
    };
    img.src = new URL("assets/halfcourt.png", window.location.href).href;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

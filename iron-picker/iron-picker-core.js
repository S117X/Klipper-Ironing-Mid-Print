(() => {
  "use strict";

  const STRIPE_OFFSET = 10;

  class IronPicker {
    constructor(root, options = {}) {
      this.root = root;
      this.embedded = !!options.embedded;
      this.onClose = options.onClose || (() => {});
      this.state = {
        selected: null,
        objects: [],
        excluded: [],
        current: null,
        filename: "",
        printState: "standby",
        axisMin: [0, 0],
        axisMax: [200, 200],
        ws: null,
        reqId: 1,
        pending: new Map(),
        busy: false,
        connected: false,
        scheduledObject: null,
        ironCache: {},
        ironingSettings: null,
      };
      this._bindUi();
    }

    $(sel) {
      return this.root.querySelector(sel);
    }

    _bindUi() {
      const back = this.$('[data-ip="btn-back"]');
      if (back) {
        back.addEventListener("click", () => {
          if (this.embedded) this.onClose();
          else if (history.length > 1) history.back();
          else location.href = "/";
        });
      }
      this.$('[data-ip="btn-topmost"]')?.addEventListener("click", () => this.enableIron("topmost"));
      this.$('[data-ip="btn-all-top"]')?.addEventListener("click", () => this.enableIron("all_top"));
      this.$('[data-ip="btn-cancel"]')?.addEventListener("click", () => this.closeModeDialog());
      this.$('[data-ip="mode-backdrop"]')?.addEventListener("click", () => this.closeModeDialog());
    }

    wsUrl() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      return `${proto}//${location.host}/websocket`;
    }

    rpc(method, params = {}) {
      const id = this.state.reqId++;
      return new Promise((resolve, reject) => {
        this.state.pending.set(id, { resolve, reject });
        this.state.ws.send(JSON.stringify({ jsonrpc: "2.0", method, params, id }));
        setTimeout(() => {
          if (this.state.pending.has(id)) {
            this.state.pending.delete(id);
            reject(new Error(`timeout: ${method}`));
          }
        }, 15000);
      });
    }

    shortLabel(name) {
      const base = name.split(".")[0].replace(/_/g, " ");
      return base.length > 40 ? `${base.slice(0, 40)}…` : base;
    }

    polygonArea(poly) {
      if (!poly || poly.length < 3) return 0;
      let sum = 0;
      for (let i = 0; i < poly.length; i++) {
        const [x1, y1] = poly[i];
        const [x2, y2] = poly[(i + 1) % poly.length];
        sum += x1 * y2 - x2 * y1;
      }
      return Math.abs(sum);
    }

    convertX(x) {
      return x;
    }

    convertY(y) {
      return -y;
    }

    stepperXmin() {
      return this.state.axisMin[0] ?? 0;
    }

    stepperXmax() {
      return this.state.axisMax[0] ?? 200;
    }

    stepperYmin() {
      return this.state.axisMin[1] ?? 0;
    }

    stepperYmax() {
      return this.state.axisMax[1] ?? 200;
    }

    bedBounds() {
      return {
        minX: this.stepperXmin(),
        maxX: this.stepperXmax(),
        minY: this.stepperYmin(),
        maxY: this.stepperYmax(),
      };
    }

    objectBounds() {
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const obj of this.state.objects) {
        if (!Array.isArray(obj.polygon)) continue;
        for (const [x, y] of obj.polygon) {
          minX = Math.min(minX, x);
          maxX = Math.max(maxX, x);
          minY = Math.min(minY, y);
          maxY = Math.max(maxY, y);
        }
      }
      if (!Number.isFinite(minX)) return null;
      return { minX, maxX, minY, maxY };
    }

    mapBounds() {
      const bed = this.bedBounds();
      const objs = this.objectBounds();
      if (!objs) return bed;

      const spanX = Math.max(objs.maxX - objs.minX, 1);
      const spanY = Math.max(objs.maxY - objs.minY, 1);
      const padX = Math.max(24, spanX * 1.25);
      const padY = Math.max(24, spanY * 1.25);

      let minX = objs.minX - padX;
      let maxX = objs.maxX + padX;
      let minY = objs.minY - padY;
      let maxY = objs.maxY + padY;

      const minSpan = 48;
      if (maxX - minX < minSpan) {
        const cx = (minX + maxX) / 2;
        minX = cx - minSpan / 2;
        maxX = cx + minSpan / 2;
      }
      if (maxY - minY < minSpan) {
        const cy = (minY + maxY) / 2;
        minY = cy - minSpan / 2;
        maxY = cy + minSpan / 2;
      }

      minX = Math.max(bed.minX, minX);
      maxX = Math.min(bed.maxX, maxX);
      minY = Math.max(bed.minY, minY);
      maxY = Math.min(bed.maxY, maxY);
      return { minX, maxX, minY, maxY };
    }

    viewBox(bounds = this.mapBounds()) {
      const w = Math.max(bounds.maxX - bounds.minX, 1);
      const h = Math.max(bounds.maxY - bounds.minY, 1);
      return `${this.convertX(bounds.minX)} ${this.convertY(bounds.maxY)} ${w} ${h}`;
    }

    stripes(min, max) {
      const out = [];
      const start = Math.floor(min / STRIPE_OFFSET) * STRIPE_OFFSET;
      const end = Math.floor(max / STRIPE_OFFSET) * STRIPE_OFFSET;
      for (let v = start; v <= end; v += STRIPE_OFFSET) out.push(v);
      return out;
    }

    setStatus(msg) {
      const el = this.$('[data-ip="status-msg"]');
      if (el) el.textContent = msg || "";
    }

    printActive() {
      return ["printing", "paused"].includes(this.state.printState);
    }

    isScheduled(name) {
      if (!name || !this.state.scheduledObject) return false;
      return name.toLowerCase() === this.state.scheduledObject.toLowerCase();
    }

    hasOtherScheduled(name) {
      return !!(
        this.state.scheduledObject &&
        name &&
        !this.isScheduled(name)
      );
    }

    hasSlicerIron(name) {
      if (!name) return false;
      const entry = this.state.ironCache[name]
        || this.state.ironCache[Object.keys(this.state.ironCache).find(
          (k) => k.toLowerCase() === name.toLowerCase()
        )];
      return !!entry?.has_slicer_iron;
    }

    applySchedule(schedule) {
      if (!schedule || (!schedule.active && !(schedule.done || []).length)) {
        this.state.scheduledObject = null;
        return;
      }
      this.state.scheduledObject = schedule.object || null;
      if (this.state.selected && this.isScheduled(this.state.selected)) {
        this.closeModeDialog();
      }
    }

    async fetchSchedule() {
      if (!this.state.filename) {
        this.state.scheduledObject = null;
        return;
      }
      try {
        const res = await fetch(
          `/server/iron/schedule?file=${encodeURIComponent(this.state.filename)}`
        );
        const data = await this.readJsonResponse(res);
        const result = data.result ?? data;
        this.applySchedule(result?.schedule);
        this.renderMap();
      } catch (_) {
        /* schedule endpoint optional until moonraker restart */
      }
    }

    async fetchIronCache() {
      if (!this.state.filename) {
        this.state.ironCache = {};
        this.state.ironingSettings = null;
        return;
      }
      try {
        const res = await fetch(
          `/server/iron/cache?file=${encodeURIComponent(this.state.filename)}`
        );
        const data = await this.readJsonResponse(res);
        const result = data.result ?? data;
        if (result?.ok) {
          this.state.ironCache = result.objects || {};
          this.state.ironingSettings = result.ironing_settings || null;
        }
      } catch (_) {
        /* cache endpoint optional until moonraker restart */
      }
    }

    updateModeDialog() {
      const dlg = this.$('[data-ip="mode-dialog"]');
      if (!dlg) return;
      const active = this.printActive() && this.state.selected && !this.state.excluded.includes(this.state.selected);
      if (!this.state.selected || !active) {
        dlg.hidden = true;
        return;
      }
      dlg.hidden = false;
      const nameEl = this.$('[data-ip="mode-object"]');
      if (nameEl) nameEl.textContent = this.shortLabel(this.state.selected);
      const top = this.$('[data-ip="btn-topmost"]');
      const all = this.$('[data-ip="btn-all-top"]');
      if (top) top.disabled = this.state.busy;
      if (all) all.disabled = this.state.busy;
    }

    selectObject(name) {
      if (!name || this.state.excluded.includes(name) || !this.printActive()) {
        return;
      }
      if (this.hasSlicerIron(name)) {
        this.setStatus(`${this.shortLabel(name)} already has OrcaSlicer ironing in this file`);
        return;
      }
      if (this.isScheduled(name)) {
        this.setStatus(`Injector iron already scheduled for ${this.shortLabel(name)}`);
        return;
      }
      this.state.selected = name;
      this.renderMap();
      if (this.hasOtherScheduled(name)) {
        this.setStatus(
          `${this.shortLabel(this.state.scheduledObject)} is scheduled. ` +
            `You can view ${this.shortLabel(name)}, but only one object per print.`
        );
      } else {
        this.setStatus("");
      }
      this.updateModeDialog();
    }

    closeModeDialog() {
      this.state.selected = null;
      const dlg = this.$('[data-ip="mode-dialog"]');
      if (dlg) dlg.hidden = true;
      this.setStatus("");
      this.renderMap();
    }

    renderMap() {
      const svg = this.$('[data-ip="bed-svg"]');
      if (!svg) return;
      svg.innerHTML = "";
      const bounds = this.mapBounds();
      svg.setAttribute("viewBox", this.viewBox(bounds));
      svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

      const xmin = bounds.minX;
      const ymin = bounds.minY;
      const xmax = bounds.maxX;
      const ymax = bounds.maxY;
      const zoomed = !!this.objectBounds();

      const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
      const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
      marker.setAttribute("id", `arrowhead-${this._markerId}`);
      marker.setAttribute("markerWidth", "5");
      marker.setAttribute("markerHeight", "4");
      marker.setAttribute("refX", "2");
      marker.setAttribute("refY", "2");
      marker.setAttribute("orient", "auto");
      const arrow = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
      arrow.setAttribute("points", "0 0, 5 2, 0 4");
      arrow.setAttribute("fill", "var(--cross)");
      marker.appendChild(arrow);
      defs.appendChild(marker);
      svg.appendChild(defs);

      const outline = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      outline.setAttribute("x", this.convertX(xmin));
      outline.setAttribute("y", this.convertY(ymax));
      outline.setAttribute("width", xmax - xmin);
      outline.setAttribute("height", ymax - ymin);
      outline.setAttribute("class", "bed-outline");
      svg.appendChild(outline);

      const grid = document.createElementNS("http://www.w3.org/2000/svg", "g");
      for (const x of this.stripes(xmin, xmax)) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", this.convertX(x));
        line.setAttribute("x2", this.convertX(x));
        line.setAttribute("y1", this.convertY(ymin));
        line.setAttribute("y2", this.convertY(ymax));
        line.setAttribute("class", "grid-line");
        grid.appendChild(line);
      }
      for (const y of this.stripes(ymin, ymax)) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", this.convertX(xmin));
        line.setAttribute("x2", this.convertX(xmax));
        line.setAttribute("y1", this.convertY(y));
        line.setAttribute("y2", this.convertY(y));
        line.setAttribute("class", "grid-line");
        grid.appendChild(line);
      }
      svg.appendChild(grid);

      if (!zoomed) {
        const markerRef = `url(#arrowhead-${this._markerId})`;
        const xAxis = document.createElementNS("http://www.w3.org/2000/svg", "line");
        xAxis.setAttribute("x1", this.convertX(0));
        xAxis.setAttribute("y1", this.convertY(1));
        xAxis.setAttribute("x2", this.convertX(xmax / 4));
        xAxis.setAttribute("y2", this.convertY(1));
        xAxis.setAttribute("class", "axis-line");
        xAxis.setAttribute("marker-end", markerRef);
        svg.appendChild(xAxis);

        const yAxis = document.createElementNS("http://www.w3.org/2000/svg", "line");
        yAxis.setAttribute("x1", this.convertX(1));
        yAxis.setAttribute("y1", this.convertY(0));
        yAxis.setAttribute("x2", this.convertX(1));
        yAxis.setAttribute("y2", this.convertY(ymax / 4));
        yAxis.setAttribute("class", "axis-line");
        yAxis.setAttribute("marker-end", markerRef);
        svg.appendChild(yAxis);
      }

      const sorted = [...this.state.objects].sort((a, b) => this.polygonArea(a.polygon) - this.polygonArea(b.polygon));
      for (const obj of sorted) {
        if (!Array.isArray(obj.polygon) || obj.polygon.length < 3) continue;
        const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        const points = obj.polygon.map(([x, y]) => `${this.convertX(x)},${this.convertY(y)}`).join(" ");
        poly.setAttribute("points", points);

        let cls = "obj-shape available";
        if (this.state.excluded.includes(obj.name)) cls = "obj-shape excluded";
        else if (this.hasSlicerIron(obj.name)) cls = "obj-shape slicer-iron";
        else if (this.isScheduled(obj.name)) cls = "obj-shape scheduled";
        if (obj.name === this.state.current) cls += " current";
        if (obj.name === this.state.selected) cls = "obj-shape selected";
        poly.setAttribute("class", cls);

        const clickable =
          !this.state.excluded.includes(obj.name) &&
          !this.isScheduled(obj.name) &&
          !this.hasSlicerIron(obj.name) &&
          this.printActive();
        if (clickable) {
          poly.addEventListener("click", () => this.selectObject(obj.name));
        }
        if (!this.state.excluded.includes(obj.name) && this.printActive()) {
          let tip = this.shortLabel(obj.name);
          if (this.hasSlicerIron(obj.name)) {
            tip = `${tip} (slicer iron)`;
          } else if (this.isScheduled(obj.name)) {
            tip = `${tip} (iron scheduled)`;
          }
          poly.addEventListener("mousemove", (ev) => this.showTooltip(ev, tip));
          poly.addEventListener("mouseleave", () => this.hideTooltip());
        }
        svg.appendChild(poly);
      }

      const wrap = this.$('[data-ip="bed-wrap"]');
      if (!wrap) return;
      let empty = wrap.querySelector(".empty-overlay");
      if (!this.printActive() || this.state.objects.length === 0) {
        if (!empty) {
          empty = document.createElement("div");
          empty.className = "empty-overlay";
          wrap.appendChild(empty);
        }
        empty.textContent = !this.printActive()
          ? "Start or pause a print to pick an object."
          : "No labeled objects found. Enable label objects in OrcaSlicer.";
      } else if (empty) {
        empty.remove();
      }
    }

    showTooltip(ev, text) {
      const tip = this.$('[data-ip="tooltip"]');
      const wrap = this.$('[data-ip="bed-wrap"]');
      if (!tip || !wrap) return;
      tip.hidden = false;
      tip.textContent = text;
      const rect = wrap.getBoundingClientRect();
      tip.style.left = `${ev.clientX - rect.left}px`;
      tip.style.top = `${ev.clientY - rect.top}px`;
    }

    hideTooltip() {
      const tip = this.$('[data-ip="tooltip"]');
      if (tip) tip.hidden = true;
    }

    async moonrakerPost(path, body) {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error.message || "request failed");
      return data.result;
    }

    async fetchStatus() {
      const res = await fetch(
        "/printer/objects/query?exclude_object&toolhead&print_stats"
      );
      const data = await res.json();
      if (data.result?.status) this.applyStatus(data.result.status);
    }

    async runGcode(script) {
      if (this.embedded) {
        return this.moonrakerPost("/printer/gcode/script", { script });
      }
      return this.rpc("printer.gcode.script", { script });
    }

    async readJsonResponse(res) {
      const text = await res.text();
      try {
        return JSON.parse(text);
      } catch (_) {
        if (text.includes("<html") || text.includes("Bad Gateway")) {
          throw new Error("Iron service unavailable (server error)");
        }
        throw new Error("Invalid server response");
      }
    }

    async enableIron(mode) {
      if (!this.state.selected || this.state.busy) return;
      if (!this.state.filename) {
        this.setStatus("Failed: No active print file.");
        return;
      }
      this.state.busy = true;
      this.setStatus("Scheduling iron...");
      this.updateModeDialog();
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
        const res = await fetch("/server/iron/enable", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            file: this.state.filename,
            object: this.state.selected,
            mode,
          }),
          signal: controller.signal,
        });
        clearTimeout(timer);
        const data = await this.readJsonResponse(res);
        if (!res.ok || data.error) {
          throw new Error(
            data.error?.message || `Iron enable failed (${res.status})`
          );
        }
        const result = data.result;
        if (!result?.ok) {
          const err =
            typeof result?.error === "string"
              ? result.error
              : result?.error?.message;
          throw new Error(err || "Iron enable failed");
        }
        const layers = (result.scheduled_layers || []).join(", ");
        this.setStatus(
          `Iron scheduled for ${this.shortLabel(result.object || this.state.selected)}` +
            (layers ? ` at layer(s) ${layers}` : "")
        );
        this.state.scheduledObject = result.object || this.state.selected;
        this.fetchSchedule().catch(() => {});
        this.renderMap();
        setTimeout(() => this.closeModeDialog(), 1500);
      } catch (err) {
        let msg =
          err.name === "TimeoutError" || err.name === "AbortError"
            ? "Request timed out"
            : err.message;
        if (/only one object per print/i.test(msg)) {
          msg = `Only one object per print. ${this.shortLabel(this.state.scheduledObject || "")} is already scheduled.`;
        } else if (/already has slicer ironing/i.test(msg)) {
          msg = `${this.shortLabel(this.state.selected || "")} already has OrcaSlicer ironing in this gcode file`;
        }
        this.setStatus(`Failed: ${msg}`);
      } finally {
        this.state.busy = false;
        this.updateModeDialog();
      }
    }

    applyStatus(payload) {
      const ex = payload.exclude_object || {};
      const toolhead = payload.toolhead || {};
      const printStats = payload.print_stats || {};

      const prevFile = this.state.filename;
      const nextFile = printStats.filename || "";
      if (prevFile && nextFile && prevFile !== nextFile) {
        this.state.scheduledObject = null;
        this.state.ironCache = {};
        this.state.selected = null;
        this.closeModeDialog();
      }

      this.state.objects = ex.objects || [];
      this.state.excluded = ex.excluded_objects || [];
      this.state.current = ex.current_object || null;
      this.state.printState = printStats.state || "standby";
      this.state.filename = nextFile;

      if (toolhead.axis_minimum) this.state.axisMin = toolhead.axis_minimum;
      if (toolhead.axis_maximum) this.state.axisMax = toolhead.axis_maximum;

      const fname = this.$('[data-ip="filename"]');
      if (fname) {
        fname.textContent = this.state.filename ? `File: ${this.state.filename}` : "No active print file";
      }
      if (this.state.selected && this.state.excluded.includes(this.state.selected)) {
        this.closeModeDialog();
      }

      this.renderMap();
      this.updateModeDialog();
      this.fetchSchedule().catch(() => {});
      this.fetchIronCache().catch(() => {});
    }

    connect() {
      if (this.state.ws && (this.state.ws.readyState === WebSocket.OPEN || this.state.ws.readyState === WebSocket.CONNECTING)) {
        return;
      }
      this._markerId = Math.random().toString(36).slice(2, 8);
      this.state.ws = new WebSocket(this.wsUrl());
      this.state.ws.onopen = async () => {
        this.state.connected = true;
        try {
          await this.rpc("printer.objects.subscribe", {
            objects: {
              exclude_object: null,
              toolhead: ["axis_minimum", "axis_maximum"],
              print_stats: null,
            },
          });
          const res = await this.rpc("printer.objects.query", {
            objects: {
              exclude_object: null,
              toolhead: ["axis_minimum", "axis_maximum"],
              print_stats: null,
            },
          });
          if (res?.status) this.applyStatus(res.status);
        } catch (err) {
          this.setStatus(`Subscribe failed: ${err.message}`);
        }
      };

      this.state.ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id && this.state.pending.has(msg.id)) {
          const { resolve, reject } = this.state.pending.get(msg.id);
          this.state.pending.delete(msg.id);
          if (msg.error) reject(new Error(msg.error.message || "rpc error"));
          else resolve(msg.result);
          return;
        }
        if (msg.method === "notify_status_update" && msg.params?.[0]) {
          this.applyStatus(msg.params[0]);
        }
      };

      this.state.ws.onclose = () => {
        this.state.connected = false;
        if (this._keepAlive) setTimeout(() => this.connect(), 2000);
      };
    }

    open() {
      this._keepAlive = true;
      this._markerId = Math.random().toString(36).slice(2, 8);
      if (this._pollTimer) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (this.embedded) {
        this.fetchStatus().catch((err) => {
          this.setStatus(`Failed to load: ${err.message}`);
        });
        this.fetchSchedule().catch(() => {});
        this.fetchIronCache().catch(() => {});
        this._pollTimer = setInterval(() => {
          this.fetchStatus().catch(() => {});
          this.fetchSchedule().catch(() => {});
          this.fetchIronCache().catch(() => {});
        }, 1000);
        return;
      }
      this.connect();
    }

    close() {
      this._keepAlive = false;
      this.closeModeDialog();
      if (this._pollTimer) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (this.state.ws) {
        this.state.ws.onclose = null;
        this.state.ws.close();
        this.state.ws = null;
      }
    }

    static template() {
      return `
        <div data-ip="bed-wrap" class="bed-wrap">
          <svg data-ip="bed-svg" xmlns="http://www.w3.org/2000/svg" aria-label="Bed map"></svg>
          <div data-ip="tooltip" class="tooltip" hidden></div>
        </div>
        <p data-ip="filename" class="filename"></p>
        <div class="legend">
          <span><i class="swatch available"></i> Available</span>
          <span><i class="swatch selected"></i> Selected</span>
          <span><i class="swatch current"></i> Printing</span>
          <span><i class="swatch excluded"></i> Excluded</span>
          <span><i class="swatch scheduled"></i> Iron scheduled</span>
          <span><i class="swatch slicer-iron"></i> Slicer iron</span>
        </div>
        <div data-ip="mode-dialog" class="mode-dialog" hidden>
          <div data-ip="mode-backdrop" class="mode-dialog-backdrop"></div>
          <div class="mode-dialog-card" role="dialog">
            <h2>Iron Mode</h2>
            <p data-ip="mode-object" class="mode-object"></p>
            <p class="mode-hint">Choose which top layers to iron for the remaining print.</p>
            <div class="mode-buttons">
              <button data-ip="btn-topmost" class="action primary">Top Surface Only</button>
              <button data-ip="btn-all-top" class="action secondary">All Top Layers</button>
              <button data-ip="btn-cancel" class="action ghost">Cancel</button>
            </div>
            <p data-ip="status-msg" class="status-msg"></p>
          </div>
        </div>
      `;
    }
  }

  window.IronPicker = IronPicker;
})();
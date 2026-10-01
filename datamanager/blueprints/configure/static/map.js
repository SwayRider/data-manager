(function () {
  const el = document.getElementById("country-map");
  if (!el || typeof L === "undefined") return;

  const panel = document.getElementById("region-panel");
  const list = document.getElementById("region-list");
  const info = document.getElementById("region-info");
  const storageKey = "activeRegion:" + panel.dataset.configId;
  const GREY = "#8b95a5";

  const regions = [...list.querySelectorAll("li")].map((li) => ({
    id: Number(li.dataset.regionId),
    name: li.dataset.name,
    color: li.dataset.color,
    li,
  }));

  // Active region: remembered per configuration, defaults to the first region.
  let activeId = null;
  try {
    activeId = Number(localStorage.getItem(storageKey)) || null;
  } catch (e) {}
  if (!regions.some((r) => r.id === activeId)) activeId = regions.length ? regions[0].id : null;
  const activeRegion = () => regions.find((r) => r.id === activeId) || null;

  let assignments = {}; // iso2 -> {id, name, color}  (core membership)
  let summary = { regions: {}, borders: [] }; // overlap + border zones, derived server-side
  const layersByIso = {};
  let bufferLayer = null;
  let carves = {}; // iso2 -> kept part (GeoJSON geometry) of carved countries
  const keepLayers = {};

  // Overlap of the active region: iso2 -> {source, configured, name}
  function overlapMap() {
    const entry = summary.regions[activeId];
    return new Map(((entry && entry.effective) || []).map((o) => [o.iso2, o]));
  }

  // --- map + hatch patterns ---------------------------------------------------
  const map = L.map(el).setView([50, 10], 4);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  const renderer = L.svg().addTo(map);

  const patterns = {};
  function hatch(color) {
    const id = "hatch-" + color.replace("#", "");
    if (patterns[id]) return `url(#${id})`;
    const NS = "http://www.w3.org/2000/svg";
    const svg = renderer._container;
    let defs = svg.querySelector("defs");
    if (!defs) {
      defs = document.createElementNS(NS, "defs");
      svg.prepend(defs);
    }
    const pattern = document.createElementNS(NS, "pattern");
    pattern.setAttribute("id", id);
    pattern.setAttribute("width", "8");
    pattern.setAttribute("height", "8");
    pattern.setAttribute("patternUnits", "userSpaceOnUse");
    pattern.setAttribute("patternTransform", "rotate(45)");
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", "0");
    line.setAttribute("y1", "0");
    line.setAttribute("x2", "0");
    line.setAttribute("y2", "8");
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "3");
    pattern.appendChild(line);
    defs.appendChild(pattern);
    patterns[id] = true;
    return `url(#${id})`;
  }

  // --- styling ---------------------------------------------------------------
  function styleFor(props) {
    // A carved country: the whole outline is only a faded reference, the kept part is drawn on top.
    if (carves[props.iso2]) return { weight: 1, color: GREY, dashArray: "3 3", fillColor: GREY, fillOpacity: 0.05 };
    return keptStyle(props);
  }
  function keptStyle(props) {
    const a = assignments[props.iso2];
    const current = activeRegion();
    const overlap = current && overlapMap().has(props.iso2);
    if (a && current && a.id === current.id) {
      return { weight: 2, color: a.color, fillColor: a.color, fillOpacity: 0.55 };
    }
    if (overlap) {
      // Overlap of the active region: hatched, dashed outline in its color. A country
      // that is core elsewhere keeps its own color underneath.
      return {
        weight: 2,
        color: current.color,
        dashArray: "5 4",
        fillColor: a ? a.color : hatch(current.color),
        fillOpacity: a ? 0.25 : 0.8,
      };
    }
    if (a) return { weight: 1, color: a.color, fillColor: a.color, fillOpacity: 0.2, dashArray: null };
    return { weight: 1, color: GREY, fillColor: GREY, fillOpacity: 0.1, dashArray: null };
  }
  function tooltipFor(props) {
    const a = assignments[props.iso2];
    const current = activeRegion();
    const ov = current && overlapMap().get(props.iso2);
    const parts = [props.name];
    if (a) parts.push(`${a.name} (core)`);
    if (ov) parts.push(`overlap of ${current.name} (${ov.source})`);
    let text = parts.join(" — ");
    if (!a && !props.curated) text += " (not configured)";
    return text;
  }
  function restyle(iso2) {
    const l = layersByIso[iso2];
    if (!l) return;
    l.setStyle(styleFor(l.feature.properties));
    l.setTooltipContent(tooltipFor(l.feature.properties));
    const k = keepLayers[iso2];
    if (k) {
      k.setStyle(keptStyle(l.feature.properties));
      k.setTooltipContent(tooltipFor(l.feature.properties) + " (carved)");
    }
  }
  function drawKept(iso2) {
    if (keepLayers[iso2]) {
      map.removeLayer(keepLayers[iso2].layer);
      delete keepLayers[iso2];
    }
    const base = layersByIso[iso2];
    if (!carves[iso2] || !base) return;
    const feature = { type: "Feature", properties: base.feature.properties, geometry: carves[iso2] };
    const layer = L.geoJSON(feature, { renderer, style: () => keptStyle(feature.properties) }).addTo(map);
    layer.eachLayer((l) => {
      l.bindTooltip("", { sticky: true });
      l.on("contextmenu", (e) => openMenu(base.feature, e.latlng));
    });
    keepLayers[iso2] = { setStyle: (s) => layer.setStyle(s), setTooltipContent: (t) => layer.eachLayer((l) => l.setTooltipContent(t)) };
    keepLayers[iso2].layer = layer;
    restyle(iso2);
  }
  const restyleAll = () => Object.keys(layersByIso).forEach(restyle);

  // --- panel -------------------------------------------------------------------
  function renderInfo() {
    info.replaceChildren();
    const current = activeRegion();
    if (!current) return;
    const entry = summary.regions[current.id] || { effective: [], excluded: [], needs_config: [] };
    const add = (tag, text, className) => {
      const n = document.createElement(tag);
      n.textContent = text;
      if (className) n.className = className;
      info.append(n);
      return n;
    };
    add("h4", `Overlap of ${current.name}`);
    if (!entry.effective.length && !entry.excluded.length) add("p", "None", "muted");
    const ul = document.createElement("ul");
    entry.effective.forEach((o) => {
      const li = document.createElement("li");
      li.textContent = o.name + (o.source === "forced" ? " (forced)" : "");
      ul.append(li);
    });
    entry.excluded.forEach((iso) => {
      const li = document.createElement("li");
      li.className = "excluded";
      li.textContent = layersByIso[iso] ? layersByIso[iso].feature.properties.name : iso;
      ul.append(li);
    });
    info.append(ul);
    if (entry.needs_config.length) {
      const p = add("p", "Needs configuring: ", "warn");
      entry.needs_config.forEach((iso, i) => {
        if (i) p.append(", ");
        const a = document.createElement("a");
        a.textContent = layersByIso[iso] ? layersByIso[iso].feature.properties.name : iso;
        a.addEventListener("click", () => openDetails(iso));
        p.append(a);
      });
    }
    if (summary.borders.length) {
      add("h4", "Border zones");
      const bl = document.createElement("ul");
      summary.borders.forEach(([a, b]) => {
        const li = document.createElement("li");
        li.textContent = `${a} ↔ ${b}`;
        bl.append(li);
      });
      info.append(bl);
    }
  }

  async function refreshBuffer() {
    if (bufferLayer) {
      map.removeLayer(bufferLayer);
      bufferLayer = null;
    }
    const current = activeRegion();
    if (!current) return;
    const url = el.dataset.bufferUrl.replace("/regions/0/", `/regions/${current.id}/`);
    const geometry = await (await fetch(url)).json();
    if (!geometry || current.id !== activeId) return;
    bufferLayer = L.geoJSON(geometry, {
      renderer,
      interactive: false,
      style: { color: current.color, weight: 2, dashArray: "8 6", fill: false },
    }).addTo(map);
  }

  async function refreshDerived() {
    summary = await (await fetch(el.dataset.overlapUrl)).json();
    restyleAll();
    renderInfo();
    refreshBuffer();
  }

  function markActive() {
    regions.forEach((r) => r.li.classList.toggle("active", r.id === activeId));
  }
  regions.forEach((r) =>
    r.li.querySelector(".region-select").addEventListener("click", () => {
      activeId = r.id;
      try {
        localStorage.setItem(storageKey, String(activeId));
      } catch (e) {}
      markActive();
      restyleAll();
      renderInfo();
      refreshBuffer();
    })
  );
  markActive();

  // --- actions -----------------------------------------------------------------
  async function call(url, method, body, headers) {
    const response = await fetch(url, { method, body, headers });
    const data = await response.json();
    if (!response.ok) {
      alert(data.error || "Request failed");
      return null;
    }
    return data;
  }
  const regionUrl = (iso2) =>
    el.dataset.regionUrl.replace("/regions/0/", `/regions/${activeId}/`).replace("__ISO__", iso2);
  const overrideUrl = (iso2) =>
    el.dataset.overrideUrl.replace("/regions/0/", `/regions/${activeId}/`).replace("__ISO__", iso2);

  async function changeMembership(iso2, method) {
    const body = await call(regionUrl(iso2), method);
    if (!body) return;
    if (body.region) assignments[iso2] = body.region;
    else delete assignments[iso2];
    restyle(iso2);
    refreshDerived();
  }
  async function changeOverlap(iso2, mode) {
    const form = mode ? new URLSearchParams({ mode }) : undefined;
    if (await call(overrideUrl(iso2), mode ? "POST" : "DELETE", form)) refreshDerived();
  }
  const carveUrl = (iso2) => el.dataset.carveUrl.replace("__ISO__", iso2);
  async function clearCarve(iso2) {
    if (!(await call(carveUrl(iso2), "DELETE"))) return;
    delete carves[iso2];
    drawKept(iso2);
    restyle(iso2);
    refreshDerived();
  }

  // Draw a polygon around the part of a country to keep; Finish saves it.
  const bar = document.getElementById("carve-bar");
  let drawing = null; // {iso2, points: [], line, handler}
  function startCarve(feature) {
    cancelCarve();
    const iso2 = feature.properties.iso2;
    drawing = { iso2, points: [], shape: null };
    map.getContainer().style.cursor = "crosshair";
    map.fitBounds(layersByIso[iso2].getBounds());
    document.getElementById("carve-text").textContent =
      `Carving ${feature.properties.name}: click around the part you want to keep, then Finish.`;
    bar.hidden = false;
    map.on("click", addPoint);
  }
  function redrawShape() {
    if (drawing.shape) map.removeLayer(drawing.shape);
    const pts = drawing.points;
    drawing.shape = pts.length > 2 ? L.polygon(pts, { color: "#c62828", weight: 2, interactive: false })
      : L.polyline(pts, { color: "#c62828", weight: 2, interactive: false });
    drawing.shape.addTo(map);
  }
  function addPoint(e) {
    drawing.points.push([e.latlng.lat, e.latlng.lng]);
    redrawShape();
  }
  function cancelCarve() {
    if (!drawing) return;
    map.off("click", addPoint);
    if (drawing.shape) map.removeLayer(drawing.shape);
    map.getContainer().style.cursor = "";
    bar.hidden = true;
    drawing = null;
  }
  async function finishCarve() {
    if (!drawing) return;
    if (drawing.points.length < 3) return alert("Click at least three points");
    const ring = drawing.points.map(([lat, lng]) => [lng, lat]);
    ring.push(ring[0]);
    const iso2 = drawing.iso2;
    const body = await call(carveUrl(iso2), "POST", JSON.stringify({ geometry: { type: "Polygon", coordinates: [ring] } }), {
      "Content-Type": "application/json",
    });
    if (!body) return;
    cancelCarve();
    carves[iso2] = body.kept;
    drawKept(iso2);
    restyle(iso2);
    refreshDerived();
  }
  document.getElementById("carve-finish").addEventListener("click", finishCarve);
  document.getElementById("carve-undo").addEventListener("click", () => {
    if (drawing) {
      drawing.points.pop();
      redrawShape();
    }
  });
  document.getElementById("carve-cancel").addEventListener("click", cancelCarve);

  function openDetails(iso2) {
    htmx.ajax("GET", el.dataset.detailsUrl.replace("__ISO__", iso2), { target: "#modal-slot" });
  }

  // Right-click a country: region membership, overlap overrides, details.
  function openMenu(feature, latlng) {
    const props = feature.properties;
    const menu = document.createElement("div");
    menu.className = "map-menu";
    const title = document.createElement("strong");
    title.textContent = props.name;
    menu.append(title);

    const addButton = (label, onClick, disabled = false) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.disabled = disabled;
      b.addEventListener("click", () => {
        map.closePopup();
        onClick();
      });
      menu.append(b);
    };
    const hint = (text) => {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = text;
      menu.append(p);
    };

    const current = activeRegion();
    const assigned = assignments[props.iso2];
    if (props.curated) {
      if (!current) hint("Create a region first");
      else if (!assigned) addButton(`Add to ${current.name}`, () => changeMembership(props.iso2, "POST"));
      else if (assigned.id === current.id)
        addButton(`Remove from ${current.name}`, () => changeMembership(props.iso2, "DELETE"));
      else addButton(`In ${assigned.name}`, () => {}, true);
    }
    // Overlap only makes sense for countries that are not core in the active region.
    if (current && !(assigned && assigned.id === current.id)) {
      const ov = overlapMap().get(props.iso2);
      const excluded = ((summary.regions[current.id] || {}).excluded || []).includes(props.iso2);
      if (ov && ov.source === "auto")
        addButton(`Exclude from overlap of ${current.name}`, () => changeOverlap(props.iso2, "exclude"));
      else if (ov) addButton("Reset overlap (automatic)", () => changeOverlap(props.iso2, null));
      else if (excluded) addButton("Include in overlap again", () => changeOverlap(props.iso2, null));
      else if (props.curated)
        addButton(`Force into overlap of ${current.name}`, () => changeOverlap(props.iso2, "include"));
    }
    addButton(carves[props.iso2] ? "Redraw carve-out…" : "Carve out…", () => startCarve(feature));
    if (carves[props.iso2]) addButton("Remove carve-out", () => clearCarve(props.iso2));
    addButton("Details / edit…", () => openDetails(props.iso2));
    L.popup({ closeButton: false }).setLatLng(latlng).setContent(menu).openOn(map);
  }

  Promise.all([
    fetch(el.dataset.url).then((r) => r.json()),
    fetch(el.dataset.assignmentsUrl).then((r) => r.json()),
    fetch(el.dataset.overlapUrl).then((r) => r.json()),
    fetch(el.dataset.carvesUrl).then((r) => r.json()),
  ]).then(([fc, assigned, derived, carved]) => {
    assignments = assigned;
    carves = carved;
    summary = derived;
    if (!fc.features.length) {
      document.getElementById("map-empty").hidden = false;
      return;
    }
    const layer = L.geoJSON(fc, {
      renderer,
      style: (f) => styleFor(f.properties),
      onEachFeature: (f, l) => {
        layersByIso[f.properties.iso2] = l;
        l.bindTooltip(tooltipFor(f.properties), { sticky: true });
        l.on("contextmenu", (e) => openMenu(f, e.latlng));
      },
    }).addTo(map);
    Object.keys(carves).forEach(drawKept);
    restyleAll();
    map.fitBounds(layer.getBounds());
    renderInfo();
    refreshBuffer();
  });

  // Fired (via HX-Trigger) after a country was saved: its "configured" state may have changed.
  // The script re-runs on every tab load, so drop the previous listener first.
  if (window.__countryUpdatedHandler) {
    document.body.removeEventListener("country-updated", window.__countryUpdatedHandler);
  }
  window.__countryUpdatedHandler = (e) => {
    const l = layersByIso[e.detail.iso2];
    if (!l) return;
    l.feature.properties.curated = e.detail.curated;
    refreshDerived();
  };
  document.body.addEventListener("country-updated", window.__countryUpdatedHandler);
})();

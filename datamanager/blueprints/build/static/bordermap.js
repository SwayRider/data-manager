// Review map for the border stage: region outlines (core solid, extended dashed) and the road crossings per pair.
// Called from the run partial each time it is (re)rendered; data comes from #border-data.
const BORDER_ROAD_COLORS = { motorway: "#c62828", trunk: "#e08a00", primary: "#2563eb", secondary: "#2e7d32" };
const BORDER_REGION_COLORS = ["#2563eb", "#7b1fa2", "#00838f", "#ef6c00", "#558b2f", "#ad1457"];

function initBorderMap() {
  const el = document.getElementById("border-map");
  const dataEl = document.getElementById("border-data");
  if (!el || !dataEl || el._map) return;
  const data = JSON.parse(dataEl.textContent);
  const map = L.map(el);
  el._map = map;
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap", maxZoom: 14 }).addTo(map);
  const regions = [...new Set(data.outlines.map((o) => o.region))];
  const layers = {};
  const group = L.featureGroup();
  data.outlines.forEach((o) => {
    const color = BORDER_REGION_COLORS[regions.indexOf(o.region) % BORDER_REGION_COLORS.length];
    const extended = o.kind === "extended";
    layers[o.name] = L.geoJSON(o.preview, {
      style: { color, weight: extended ? 1.5 : 2, fillColor: color, fillOpacity: extended ? 0.04 : 0.15, dashArray: extended ? "6 4" : null },
    }).bindTooltip(`${o.region} (${o.kind})`, { sticky: true }).addTo(group);
  });
  data.crossings.forEach((c) => {
    const pair = L.featureGroup();
    c.points.forEach(([lon, lat, type, id]) => {
      L.circleMarker([lat, lon], { radius: 5, color: "#222", weight: 1, fillColor: BORDER_ROAD_COLORS[type] || "#555", fillOpacity: 0.9 })
        .bindTooltip(`${c.pair} · ${type} · ${id}`).addTo(pair);
    });
    layers[c.pair] = pair.addTo(group);
  });
  group.addTo(map);
  const fit = (bounds) => bounds.isValid() && map.fitBounds(bounds, { padding: [20, 20], animate: false, maxZoom: 12 });
  fit(group.getBounds());
  const observer = new ResizeObserver(() => {
    if (!el.isConnected) return observer.disconnect();  // replaced by an htmx swap
    map.invalidateSize({ animate: false });
  });
  observer.observe(el);
  requestAnimationFrame(() => { map.invalidateSize({ animate: false }); fit(group.getBounds()); });
  document.querySelectorAll("[data-border]").forEach((row) => {
    row.addEventListener("click", () => { const layer = layers[row.dataset.border]; if (layer) fit(layer.getBounds()); });
  });
}

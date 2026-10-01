// Review map for the polygons stage: draws every polygon of the run report, coloured by kind.
// Called from the run partial each time it is (re)rendered; data comes from #poly-data.
const POLY_COLORS = { core: "#2563eb", overlap: "#e08a00", border: "#c62828", carve: "#2e7d32" };

function initPolyMap() {
  const el = document.getElementById("poly-map");
  const dataEl = document.getElementById("poly-data");
  if (!el || !dataEl || el._map) return;
  const polygons = JSON.parse(dataEl.textContent);
  const map = L.map(el);
  el._map = map;
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap", maxZoom: 12 }).addTo(map);
  const layers = {};
  const group = L.featureGroup();
  polygons.forEach((p) => {
    const color = POLY_COLORS[p.kind] || "#555";
    layers[p.name] = L.geoJSON(p.preview, {
      style: { color, weight: p.kind === "core" ? 2 : 1.5, fillColor: color, fillOpacity: p.kind === "overlap" ? 0.08 : 0.25,
               dashArray: p.kind === "overlap" ? "6 4" : null },
    }).bindTooltip(`${p.name} · ${p.area_km2.toLocaleString()} km²`, { sticky: true }).addTo(group);
  });
  group.addTo(map);
  const fit = (bounds) => map.fitBounds(bounds, { padding: [20, 20], animate: false });

  // The map is created while the page is still being laid out (the table below it, a scrollbar that
  // appears later), so its size must be re-read whenever the container changes, not only on window resize.
  fit(group.getBounds());
  const observer = new ResizeObserver(() => {
    if (!el.isConnected) return observer.disconnect();  // replaced by an htmx swap
    map.invalidateSize({ animate: false });
  });
  observer.observe(el);
  requestAnimationFrame(() => { map.invalidateSize({ animate: false }); fit(group.getBounds()); });

  document.querySelectorAll("[data-poly]").forEach((row) => {
    row.addEventListener("click", () => {
      const layer = layers[row.dataset.poly];
      if (layer) fit(layer.getBounds());
    });
  });
}

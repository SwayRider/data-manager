(function () {
  const root = document.getElementById("style-preview");
  if (!root) return;

  function loadScript(src, ready, callback) {
    if (ready()) return callback();
    const script = document.createElement("script");
    script.src = src;
    script.onload = callback;
    document.head.append(script);
  }

  // MapLibre plus the PMTiles protocol (the tiles are one local file read with HTTP Range requests).
  function whenMapLibre(callback) {
    loadScript("https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js", () => typeof maplibregl !== "undefined", () =>
      loadScript("https://unpkg.com/pmtiles@4/dist/pmtiles.js", () => typeof pmtiles !== "undefined", () => {
        if (!window.pmtilesProtocol) {
          window.pmtilesProtocol = new pmtiles.Protocol();
          maplibregl.addProtocol("pmtiles", window.pmtilesProtocol.tile);
        }
        callback();
      })
    );
  }

  whenMapLibre(() => {
    const zoomLabel = document.getElementById("preview-zoom");
    const inputs = [...document.querySelectorAll("input[data-key]")];
    let mode = "light";
    let map = null;
    let info = null;
    let view = { center: [4.35, 50.85], zoom: 8 };

    const typed = (key) => {
      const input = inputs.find((i) => i.dataset.key === key);
      return input && input.value !== "" ? Number(input.value) : null;
    };

    // Same rule as the server: earliest zoom over the label kinds a layer draws; untouched
    // layers keep the base style's own minzoom.
    function applyOverrides() {
      if (!map || !info) return;
      const original = Object.fromEntries(info.style.layers.map((l) => [l.id, l]));
      Object.entries(info.layers).forEach(([id, keys]) => {
        if (!map.getLayer(id)) return;
        const base = original[id];
        const anyTyped = keys.some((k) => typed(k) !== null);
        const min = anyTyped
          ? Math.min(...keys.map((k) => (typed(k) !== null ? typed(k) : info.defaults[k] || 0)))
          : base.minzoom || 0;
        const max = base.maxzoom && base.maxzoom > min ? base.maxzoom : 24;
        map.setLayerZoomRange(id, min, max);
      });
    }

    async function show() {
      const key = document.getElementById(mode + "_style").value;
      info = await (await fetch(root.dataset.previewUrl.replace("__KEY__", key))).json();
      inputs.forEach((i) => {
        const d = info.defaults[i.dataset.key];
        i.placeholder = d === undefined ? "–" : d === 0 ? "tile" : String(d);
      });
      if (map) {
        view = { center: map.getCenter().toArray(), zoom: map.getZoom() };
        map.remove();
        map = null;
      }
      document.getElementById("style-notice").hidden = info.tiles_available;
      if (!info.tiles_available) return;
      map = new maplibregl.Map({ container: "style-map", style: info.style, ...view });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
      const update = () => (zoomLabel.textContent = map.getZoom().toFixed(1));
      map.on("zoom", update);
      map.on("load", () => {
        update();
        applyOverrides();
      });
    }

    root.querySelectorAll("[data-mode]").forEach((b) =>
      b.addEventListener("click", () => {
        mode = b.dataset.mode;
        root.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("primary", x === b));
        show();
      })
    );
    root.querySelectorAll("[data-jump]").forEach((b) =>
      b.addEventListener("click", () => {
        if (!map) return;
        const [lng, lat] = b.dataset.jump.split(",").map(Number);
        map.jumpTo({ center: [lng, lat], zoom: Math.max(map.getZoom(), 7) });
      })
    );
    inputs.forEach((i) => i.addEventListener("input", applyOverrides));
    ["light_style", "dark_style"].forEach((id) =>
      document.getElementById(id).addEventListener("change", () => {
        if (id.startsWith(mode)) show();
      })
    );
    show();
  });
})();

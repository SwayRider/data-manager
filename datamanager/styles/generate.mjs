// Regenerates protomaps-<flavor>.json (the five Protomaps flavors plus our own `classic` and `vivid-dark`) from the @protomaps/basemaps package (BSD-3, see NOTICE). Run from a scratch
// directory where `npm i @protomaps/basemaps` was done:   node generate.mjs <out dir>
// The tiles source URL is a placeholder that data-manager replaces at build time.
import { layers, namedFlavor } from "@protomaps/basemaps";
import fs from "fs";

// Our own flavors: a named base flavor with colors overridden (road hierarchy: highway orange, major yellow).
const CUSTOM = {
  classic: {
    base: "light", sprite: "light",
    colors: {
      military: "#e6dfd2", scrub_a: "#d9e6bf", scrub_b: "#cfe2b2", sand: "#f0e6c8", beach: "#f5e9c6", zoo: "#e2ecd0", aerodrome: "#e5e3df",
      landcover: { grassland: "#d9ecc0", barren: "#f0e8d4", urban_area: "#e9e5dd", farmland: "#e8f0d3", glacier: "#ffffff", scrub: "#dbe8c0", forest: "#c3e0a8" },
      earth: "#f2efe9", water: "#aad3df", park_a: "#cdebb0", park_b: "#c4e3a6", wood_a: "#b8dca0", wood_b: "#b0d598",
      hospital: "#f3dcdc", school: "#efe6d0", industrial: "#ece3e9", pedestrian: "#ece8df", buildings: "#e0dad0",
      highway: "#fcae48", highway_casing_early: "#d98a1c", highway_casing_late: "#d98a1c",
      major: "#fde68c", major_casing_early: "#d6b94a", major_casing_late: "#d6b94a",
      link: "#fcd28a", link_casing: "#d6a850",
      minor_a: "#ffffff", minor_b: "#ffffff", minor_casing: "#c9c3b6", minor_service: "#fafafa", minor_service_casing: "#d2ccc0",
      other: "#f6f4ef",
      tunnel_highway: "#fdd9a3", tunnel_highway_casing: "#d9b077", tunnel_major: "#fdf0c2", tunnel_major_casing: "#d9c98a",
      tunnel_link: "#fde3b8", tunnel_link_casing: "#d9b077", tunnel_minor: "#f4f1ea", tunnel_minor_casing: "#d2ccc0",
      bridges_highway: "#fcae48", bridges_highway_casing: "#b87410", bridges_major: "#fde68c", bridges_major_casing: "#b49a2c",
      bridges_link: "#fcd28a", bridges_link_casing: "#b88a30", bridges_minor: "#ffffff", bridges_minor_casing: "#a8a294",
    },
  },
  "vivid-dark": {
    base: "dark", sprite: "dark",
    colors: {
      military: "#26262b", scrub_a: "#2a3a2c", scrub_b: "#2c3d2e", sand: "#34322a", beach: "#38352b",
      landcover: { grassland: "#202e24", barren: "#2a2a28", urban_area: "#22242b", farmland: "#222d25", glacier: "#2b2b2b", scrub: "#26332a", forest: "#1f3526" },
      earth: "#1d2027", water: "#1f4a78", park_a: "#27503a", park_b: "#2a563e", wood_a: "#234a33", wood_b: "#20442f",
      hospital: "#33232a", school: "#2c2b25", industrial: "#262630", pedestrian: "#252830", buildings: "#2a2e38",
      highway: "#f0a63a", highway_casing_early: "#5c3d0c", highway_casing_late: "#5c3d0c",
      major: "#d9c45e", major_casing_early: "#5a5020", major_casing_late: "#5a5020",
      link: "#c79a4a", link_casing: "#4f3a14",
      minor_a: "#707a90", minor_b: "#707a90", minor_casing: "#2a2e38", minor_service: "#59627a", minor_service_casing: "#2a2e38",
      other: "#3c4250",
      tunnel_highway: "#7a5518", tunnel_highway_casing: "#2f2208", tunnel_major: "#6f6528", tunnel_major_casing: "#2e2a10",
      tunnel_link: "#6b5424", tunnel_link_casing: "#2b200a", tunnel_minor: "#3a4050", tunnel_minor_casing: "#23262f",
      bridges_highway: "#f0a63a", bridges_highway_casing: "#3a2506", bridges_major: "#d9c45e", bridges_major_casing: "#3a3410",
      bridges_link: "#c79a4a", bridges_link_casing: "#34260a", bridges_minor: "#707a90", bridges_minor_casing: "#20232b",
      roads_label_minor: "#a8afc0", roads_label_minor_halo: "#1d2027", roads_label_major: "#e0d9bd", roads_label_major_halo: "#1d2027",
      city_label: "#e6e8ee", city_label_halo: "#1d2027", subplace_label: "#9aa3b5", state_label: "#8b95aa", country_label: "#b5bdcc",
    },
  },
};

const ASSETS = "https://protomaps.github.io/basemaps-assets";
const out = process.argv[2] || ".";
const flavors = ["light", "white", "grayscale", "dark", "black", ...Object.keys(CUSTOM)];
for (const flavor of flavors) {
  const custom = CUSTOM[flavor];
  const colors = custom ? { ...namedFlavor(custom.base), ...custom.colors } : namedFlavor(flavor);
  const style = {
    version: 8,
    name: `Protomaps ${flavor}`,
    glyphs: `${ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${ASSETS}/sprites/v4/${custom ? custom.sprite : flavor}`,
    sources: {
      protomaps: {
        type: "vector",
        url: "pmtiles://__TILES__",
        attribution: '<a href="https://github.com/protomaps/basemaps">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
      },
    },
    layers: layers("protomaps", colors, { lang: "en" }),
  };
  fs.writeFileSync(`${out}/protomaps-${flavor}.json`, JSON.stringify(style, null, 1) + "\n");
}

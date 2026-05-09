(function () {
  "use strict";

  function readNumber(element, name, fallback) {
    const value = Number.parseFloat(element.dataset[name]);
    return Number.isFinite(value) ? value : fallback;
  }

  function initializeGameMap() {
    const mapElement = document.getElementById("game-map");
    if (!mapElement || !window.L || mapElement.dataset.initialized === "true") {
      return;
    }

    const latitude = readNumber(mapElement, "centerLat", 46.8182);
    const longitude = readNumber(mapElement, "centerLng", 8.2275);
    const zoom = readNumber(mapElement, "zoom", 8);
    const map = window.L.map(mapElement, {
      attributionControl: true,
      zoomControl: true,
    });

    map.setView([latitude, longitude], zoom);
    window.L.control.scale({ imperial: false, metric: true }).addTo(map);
    mapElement.dataset.initialized = "true";

    window.setTimeout(function () {
      map.invalidateSize();
    }, 0);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeGameMap);
  } else {
    initializeGameMap();
  }
})();

(function () {
  "use strict";

  function readNumber(element, name, fallback) {
    const value = Number.parseFloat(element.dataset[name]);
    return Number.isFinite(value) ? value : fallback;
  }

  function addBoundaryLayer(map, url, options) {
    if (!url) {
      return Promise.resolve(null);
    }

    return window.fetch(url, {
      credentials: "same-origin",
      headers: {
        Accept: "application/geo+json, application/json",
      },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Boundary request failed with status " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        const layer = window.L.geoJSON(data, {
          interactive: false,
          style: options.style,
        }).addTo(map);

        if (options.fitBounds && layer.getLayers().length > 0) {
          map.fitBounds(layer.getBounds(), {
            animate: false,
            padding: [24, 24],
          });
        }

        return layer;
      })
      .catch(function () {
        map.getContainer().classList.add("game-map--boundary-error");
        return null;
      });
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
    addBoundaryLayer(map, mapElement.dataset.municipalityBoundariesUrl, {
      fitBounds: true,
      style: {
        color: "#7f98a8",
        fillColor: "#dce8ee",
        fillOpacity: 0.28,
        opacity: 0.9,
        weight: 1,
      },
    }).then(function () {
      return addBoundaryLayer(map, mapElement.dataset.cantonBoundariesUrl, {
        fitBounds: false,
        style: {
          color: "#214e63",
          fillOpacity: 0,
          opacity: 0.95,
          weight: 2.5,
        },
      });
    });

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

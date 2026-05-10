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
        showMapStatus(map, options.errorMessage);
        return null;
      });
  }

  function showMapStatus(map, message) {
    const statusElement = map.getContainer().parentElement.querySelector(
      "[data-map-status]"
    );
    if (statusElement) {
      statusElement.textContent = message;
    }
  }

  function addBaseMapLayer(map, url) {
    if (!url) {
      return null;
    }

    return window.L.tileLayer(url, {
      attribution: "Map data &copy; swisstopo",
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
    }).addTo(map);
  }

  function formatCoordinate(value) {
    return value.toFixed(5);
  }

  function createGuessMarker(map) {
    const marker = document.createElement("div");
    marker.className = "guess-marker";
    marker.setAttribute("aria-hidden", "true");
    map.getContainer().appendChild(marker);
    return marker;
  }

  function positionGuessMarker(map, marker, latlng) {
    const point = map.latLngToContainerPoint(latlng);
    marker.style.transform = (
      "translate(" + point.x + "px, " + point.y + "px) translate(-50%, -50%)"
    );
  }

  function initializeGuessInteraction(map) {
    const form = document.querySelector("[data-guess-form]");
    if (!form) {
      return;
    }

    const latitudeInput = form.querySelector("[data-guess-lat]");
    const longitudeInput = form.querySelector("[data-guess-lng]");
    const coordinatesOutput = form.querySelector("[data-guess-coordinates]");
    const confirmButton = form.querySelector("[data-confirm-guess]");
    let marker = null;
    let selectedLatLng = null;

    function updateMarkerPosition() {
      if (marker !== null && selectedLatLng !== null) {
        positionGuessMarker(map, marker, selectedLatLng);
      }
    }

    map.on("move zoom resize viewreset", updateMarkerPosition);

    map.on("click", function (event) {
      const latitude = formatCoordinate(event.latlng.lat);
      const longitude = formatCoordinate(event.latlng.lng);
      selectedLatLng = event.latlng;

      if (marker === null) {
        marker = createGuessMarker(map);
      }
      positionGuessMarker(map, marker, selectedLatLng);

      latitudeInput.value = latitude;
      longitudeInput.value = longitude;
      coordinatesOutput.textContent = "Selected point: " + latitude + ", " + longitude;
      confirmButton.disabled = false;
    });
  }

  function initializeGameMap() {
    const mapElement = document.getElementById("game-map");
    if (!mapElement || !window.L || mapElement.dataset.initialized === "true") {
      return;
    }

    const switzerlandBounds = window.L.latLngBounds(
      [45.55, 5.5],
      [48.15, 10.9]
    );
    const latitude = readNumber(mapElement, "centerLat", 46.8182);
    const longitude = readNumber(mapElement, "centerLng", 8.2275);
    const zoom = readNumber(mapElement, "zoom", 8);
    const map = window.L.map(mapElement, {
      attributionControl: true,
      maxBounds: switzerlandBounds,
      maxBoundsViscosity: 1,
      minZoom: 8,
      preferCanvas: true,
      zoomControl: true,
    });

    map.setView([latitude, longitude], zoom);
    addBaseMapLayer(map, mapElement.dataset.baseMapUrl);
    window.L.control.scale({ imperial: false, metric: true }).addTo(map);
    initializeGuessInteraction(map);
    mapElement.dataset.initialized = "true";
    addBoundaryLayer(map, mapElement.dataset.municipalityBoundariesUrl, {
      errorMessage: "Municipality boundaries could not be loaded.",
      fitBounds: true,
      style: {
        color: "#ffffff",
        fillOpacity: 0,
        opacity: 0.75,
        weight: 0.6,
      },
    }).then(function () {
      return addBoundaryLayer(map, mapElement.dataset.cantonBoundariesUrl, {
        errorMessage: "Canton boundaries could not be loaded.",
        fitBounds: false,
        style: {
          color: "#ffcf4a",
          fillOpacity: 0,
          opacity: 0.9,
          weight: 1.4,
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

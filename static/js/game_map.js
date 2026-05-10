(function () {
  "use strict";

  function readNumber(element, name, fallback) {
    const value = Number.parseFloat(element.dataset[name]);
    return Number.isFinite(value) ? value : fallback;
  }

  function readCookie(name) {
    const cookies = document.cookie ? document.cookie.split(";") : [];
    for (let index = 0; index < cookies.length; index += 1) {
      const cookie = cookies[index].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return "";
  }

  function sendTrackingEvent(mapElement, eventType, payload) {
    const url = mapElement.dataset.trackingUrl;
    if (!url || !window.fetch) {
      return;
    }

    window.fetch(url, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": readCookie("csrftoken"),
      },
      body: JSON.stringify({
        event_type: eventType,
        payload: payload || {},
      }),
    }).catch(function () {
      return null;
    });
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
          onEachFeature: options.onEachFeature,
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

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function updateLayerVisibility(map, layer, minZoom) {
    if (!layer) {
      return;
    }

    if (map.getZoom() >= minZoom) {
      if (!map.hasLayer(layer)) {
        layer.addTo(map);
      }
      return;
    }

    if (map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
  }

  function buildLabelLayer(data) {
    return window.L.geoJSON(data, {
      interactive: false,
      pointToLayer: function (feature, latlng) {
        const properties = feature.properties || {};
        return window.L.marker(latlng, {
          icon: window.L.divIcon({
            className: "leaflet-div-icon municipality-label-marker",
            html: (
              '<span class="municipality-label">' +
              escapeHtml(properties.name) +
              "</span>"
            ),
          }),
          interactive: false,
        });
      },
    });
  }

  function initializeLabelLayer(map, url, minZoom) {
    if (!url) {
      return;
    }

    let labelLayer = null;
    let labelRequest = null;

    function loadLabels() {
      if (labelRequest !== null) {
        return labelRequest;
      }

      labelRequest = window.fetch(url, {
        credentials: "same-origin",
        headers: {
          Accept: "application/geo+json, application/json",
        },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Label request failed with status " + response.status);
          }
          return response.json();
        })
        .then(function (data) {
          labelLayer = buildLabelLayer(data);
          updateLayerVisibility(map, labelLayer, minZoom);
          return labelLayer;
        })
        .catch(function () {
          showMapStatus(map, "Municipality labels could not be loaded.");
          return null;
        });
      return labelRequest;
    }

    function syncLabelLayer() {
      if (map.getZoom() < minZoom) {
        updateLayerVisibility(map, labelLayer, minZoom);
        return;
      }

      if (labelLayer === null) {
        loadLabels();
        return;
      }
      updateLayerVisibility(map, labelLayer, minZoom);
    }

    map.on("zoomend", syncLabelLayer);
    syncLabelLayer();
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

  function createRevealedGuessMarker(map) {
    const marker = createGuessMarker(map);
    marker.classList.add("guess-marker--revealed");
    return marker;
  }

  function positionGuessMarker(map, marker, latlng) {
    const point = map.latLngToContainerPoint(latlng);
    marker.style.transform = (
      "translate(" + point.x + "px, " + point.y + "px) translate(-50%, -50%)"
    );
  }

  function initializeGuessInteraction(map, mapElement) {
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
      const previousLatLng = selectedLatLng;
      const hadMarker = marker !== null;
      selectedLatLng = event.latlng;

      if (marker === null) {
        marker = createGuessMarker(map);
      }
      positionGuessMarker(map, marker, selectedLatLng);

      latitudeInput.value = latitude;
      longitudeInput.value = longitude;
      coordinatesOutput.textContent = "Selected point: " + latitude + ", " + longitude;
      confirmButton.disabled = false;

      sendTrackingEvent(mapElement, "MAP_CLICKED", {
        had_existing_pin: hadMarker,
        latitude: Number(latitude),
        longitude: Number(longitude),
        previous_latitude: previousLatLng
          ? Number(formatCoordinate(previousLatLng.lat))
          : null,
        previous_longitude: previousLatLng
          ? Number(formatCoordinate(previousLatLng.lng))
          : null,
        zoom: map.getZoom(),
      });
    });
  }

  function readRevealState(mapElement) {
    const targetId = mapElement.dataset.revealTargetId;
    const latitude = Number.parseFloat(mapElement.dataset.revealLat);
    const longitude = Number.parseFloat(mapElement.dataset.revealLng);
    if (!targetId || !Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return null;
    }
    return {
      latlng: window.L.latLng(latitude, longitude),
      targetId: targetId,
    };
  }

  function initializeReveal(map, revealState, mapElement) {
    const marker = createRevealedGuessMarker(map);

    function updateMarkerPosition() {
      positionGuessMarker(map, marker, revealState.latlng);
    }

    map.getContainer().classList.add("game-map--reveal");
    map.on("move zoom resize viewreset", updateMarkerPosition);
    updateMarkerPosition();
    sendTrackingEvent(mapElement, "REVEAL_SHOWN", {
      latitude: revealState.latlng.lat,
      longitude: revealState.latlng.lng,
      target_municipality_id: Number(revealState.targetId),
      zoom: map.getZoom(),
    });
  }

  function initializeNextTurnTracking(mapElement) {
    const nextTurnLink = document.querySelector("[data-next-turn-link]");
    if (!nextTurnLink) {
      return;
    }

    nextTurnLink.addEventListener("click", function () {
      sendTrackingEvent(mapElement, "NEXT_TURN_CLICKED", {
        href: nextTurnLink.getAttribute("href"),
      });
    });
  }

  function isTargetFeature(feature, targetId) {
    return Boolean(
      feature &&
      feature.properties &&
      String(feature.properties.id) === String(targetId)
    );
  }

  function municipalityStyle(revealState) {
    return function (feature) {
      if (revealState && isTargetFeature(feature, revealState.targetId)) {
        return {
          color: "#7cff8b",
          fillColor: "#1b8f5a",
          fillOpacity: 0.28,
          opacity: 1,
          weight: 2,
        };
      }
      return {
        color: "#ffffff",
        fillOpacity: 0,
        opacity: 0.75,
        weight: 0.6,
      };
    };
  }

  function findTargetLayer(layer, targetId) {
    let targetLayer = null;
    if (!layer) {
      return null;
    }
    layer.eachLayer(function (featureLayer) {
      if (targetLayer === null && isTargetFeature(featureLayer.feature, targetId)) {
        targetLayer = featureLayer;
      }
    });
    return targetLayer;
  }

  function fitRevealBounds(map, municipalityLayer, revealState) {
    const bounds = window.L.latLngBounds([revealState.latlng]);
    const targetLayer = findTargetLayer(municipalityLayer, revealState.targetId);
    if (targetLayer !== null) {
      bounds.extend(targetLayer.getBounds());
    }

    map.fitBounds(bounds, {
      animate: false,
      maxZoom: 12,
      padding: [42, 42],
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
    const labelMinZoom = readNumber(mapElement, "labelMinZoom", 11);
    const revealState = readRevealState(mapElement);
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
    if (revealState) {
      initializeReveal(map, revealState, mapElement);
      initializeNextTurnTracking(mapElement);
    } else {
      initializeGuessInteraction(map, mapElement);
    }
    mapElement.dataset.initialized = "true";
    addBoundaryLayer(map, mapElement.dataset.municipalityBoundariesUrl, {
      errorMessage: "Municipality boundaries could not be loaded.",
      fitBounds: !revealState,
      style: municipalityStyle(revealState),
    }).then(function (municipalityLayer) {
      if (revealState && municipalityLayer !== null) {
        fitRevealBounds(map, municipalityLayer, revealState);
      }
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
    }).then(function () {
      if (revealState) {
        initializeLabelLayer(
          map,
          mapElement.dataset.municipalityLabelsUrl,
          labelMinZoom
        );
      }
      return null;
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

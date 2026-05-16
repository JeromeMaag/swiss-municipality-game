(function () {
  "use strict";

  const BACKGROUND_MAP_STORAGE_KEY = "gemeindeguess.backgroundMap";
  const BOUNDARY_LINE_STORAGE_KEY = "gemeindeguess.boundaryLines";
  const OUTLINE_STORAGE_KEY = "gemeindeguess.outlines";
  const DEFAULT_BACKGROUND_MAP_ID = "swissimage";
  const DEFAULT_BOUNDARY_LINE_MODE = "auto";
  const BOUNDARY_LINE_MODES = new Set(["auto", "white", "black"]);
  const OUTLINE_LAYER_ORDER = [
    "cantons",
    "municipalities",
    "villages",
  ];
  const OUTLINE_LAYERS = new Set([
    "all",
    "cantons",
    "municipalities",
    "villages",
    "off",
  ]);
  const AUTO_BOUNDARY_LINE_THEME = {
    cartoVoyager: "black",
    lightRelief: "black",
    none: "black",
    surfaceRelief: "black",
    swissimage: "white",
  };
  const DEFAULT_MIN_ZOOM = 8;
  const DESKTOP_SIDEBAR_WIDTH = 360;
  const MOBILE_BREAKPOINT_WIDTH = 920;
  const COMPACT_MIN_FIT_PADDING = 12;
  const COMPACT_MIN_BOTTOM_PADDING = 180;
  const COMPACT_MIN_AVAILABLE_MAP_HEIGHT = 96;
  const COMPACT_SHORT_AVAILABLE_MAP_HEIGHT = 48;
  const COMPACT_SIDEBAR_HEIGHT_RATIO = 0.5;
  const COMPACT_TALL_SIDEBAR_HEIGHT_RATIO = 0.64;
  const COMPACT_TOP_PADDING_RATIO = 0.18;
  const VECTOR_RENDERER_PADDING = 0.2;

  function swisstopoWmtsUrl(layer, extension) {
    return (
      "https://wmts.geo.admin.ch/1.0.0/" +
      layer +
      "/default/current/3857/{z}/{x}/{y}." +
      extension
    );
  }

  const BACKGROUND_MAPS = {
    swissimage: {
      attribution: "Map data &copy; swisstopo",
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
      url: swisstopoWmtsUrl("ch.swisstopo.swissimage", "jpeg"),
    },
    surfaceRelief: {
      attribution: "Map data &copy; swisstopo",
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
      url: swisstopoWmtsUrl(
        "ch.swisstopo.swisssurface3d-reliefschattierung-multidirektional",
        "png"
      ),
    },
    lightRelief: {
      attribution: "Map data &copy; swisstopo",
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
      url: swisstopoWmtsUrl(
        "ch.swisstopo.leichte-basiskarte_reliefschattierung",
        "png"
      ),
    },
    cartoVoyager: {
      attribution:
        '&copy; <a href="https://carto.com/attributions">CARTO</a> ' +
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxNativeZoom: 20,
      maxZoom: 20,
      minZoom: 0,
      subdomains: "abcd",
      url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png",
    },
    none: {
      attribution: "",
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
      url: "",
    },
  };

  function readNumber(element, name, fallback) {
    const value = Number.parseFloat(element.dataset[name]);
    return Number.isFinite(value) ? value : fallback;
  }

  function isCompactMap(map) {
    return map.getSize().x <= MOBILE_BREAKPOINT_WIDTH;
  }

  function compactMapEdgePadding(map, padding) {
    return Math.min(
      padding,
      Math.max(
        COMPACT_MIN_FIT_PADDING,
        Math.floor(map.getSize().y * COMPACT_TOP_PADDING_RATIO)
      )
    );
  }

  function compactMapBottomPadding(map, padding) {
    const mapHeight = map.getSize().y;
    const layout = map.getContainer().closest(".game-layout");
    const hasTallSidebar = layout
      ? Boolean(
          layout.querySelector(".game-entry-sidebar") ||
            layout.querySelector(".history-sidebar")
        )
      : false;
    const sidebarRatio = hasTallSidebar
      ? COMPACT_TALL_SIDEBAR_HEIGHT_RATIO
      : COMPACT_SIDEBAR_HEIGHT_RATIO;
    const desiredPadding = Math.max(
      COMPACT_MIN_BOTTOM_PADDING,
      Math.ceil(mapHeight * sidebarRatio + padding)
    );
    const preferredMaxPadding =
      mapHeight - padding - COMPACT_MIN_AVAILABLE_MAP_HEIGHT;
    const fallbackMaxPadding =
      mapHeight - padding - COMPACT_SHORT_AVAILABLE_MAP_HEIGHT;
    const maxPadding =
      preferredMaxPadding >= 0 ? preferredMaxPadding : fallbackMaxPadding;
    return Math.max(
      0,
      Math.min(desiredPadding, maxPadding)
    );
  }

  function mapFitOptions(map, maxZoom, padding) {
    if (isCompactMap(map)) {
      const compactPadding = compactMapEdgePadding(map, padding);
      return {
        animate: false,
        maxZoom: maxZoom,
        paddingBottomRight: [
          compactPadding,
          compactMapBottomPadding(map, compactPadding),
        ],
        paddingTopLeft: [compactPadding, compactPadding],
      };
    }

    return {
      animate: false,
      maxZoom: maxZoom,
      paddingBottomRight: [padding, padding],
      paddingTopLeft: [DESKTOP_SIDEBAR_WIDTH + padding, padding],
    };
  }

  function fitBoundaryLayerBounds(map, layer) {
    if (layer.getLayers().length > 0) {
      map.fitBounds(layer.getBounds(), mapFitOptions(map, 10, 24));
    }
  }

  function constrainMapToBounds(map, bounds) {
    map.setMinZoom(DEFAULT_MIN_ZOOM);
    if (map.getZoom() < DEFAULT_MIN_ZOOM) {
      map.setZoom(DEFAULT_MIN_ZOOM, { animate: false });
    }
    map.panInsideBounds(bounds, { animate: false });
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
      cache: "no-cache",
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
          renderer: options.renderer,
          style: options.style,
        }).addTo(map);

        if (options.fitBounds && layer.getLayers().length > 0) {
          fitBoundaryLayerBounds(map, layer);
        }

        return layer;
      })
      .catch(function () {
        if (!options.suppressGlobalError) {
          map.getContainer().classList.add("game-map--boundary-error");
          showMapStatus(map, options.errorMessage);
        }
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
          labelRequest = null;
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

  function normalizeBackgroundMapId(mapId) {
    return Object.prototype.hasOwnProperty.call(BACKGROUND_MAPS, mapId)
      ? mapId
      : DEFAULT_BACKGROUND_MAP_ID;
  }

  function normalizeBoundaryLineMode(mode) {
    return BOUNDARY_LINE_MODES.has(mode) ? mode : DEFAULT_BOUNDARY_LINE_MODE;
  }

  function defaultOutlineLayers(hasVillageLayer) {
    const layers = new Set(["cantons", "municipalities"]);
    if (hasVillageLayer) {
      layers.add("villages");
    }
    return layers;
  }

  function serializeOutlineLayers(layers) {
    if (layers.size === 0) {
      return "off";
    }
    return OUTLINE_LAYER_ORDER.filter(function (layer) {
      return layers.has(layer);
    }).join(",");
  }

  function normalizeOutlineLayers(value, hasVillageLayer) {
    if (!value) {
      return defaultOutlineLayers(hasVillageLayer);
    }
    if (value === "all") {
      return defaultOutlineLayers(hasVillageLayer);
    }
    if (value === "off") {
      return new Set();
    }

    const layers = new Set();
    String(value)
      .split(",")
      .map(function (layer) {
        return layer.trim();
      })
      .forEach(function (layer) {
        if (!OUTLINE_LAYERS.has(layer) || layer === "all" || layer === "off") {
          return;
        }
        if (layer === "villages" && !hasVillageLayer) {
          return;
        }
        layers.add(layer);
      });

    if (layers.size === 0) {
      return defaultOutlineLayers(hasVillageLayer);
    }
    return layers;
  }

  function resolveBoundaryLineTheme(mapId, mode) {
    const normalizedMode = normalizeBoundaryLineMode(mode);
    if (normalizedMode !== "auto") {
      return normalizedMode;
    }
    return AUTO_BOUNDARY_LINE_THEME[normalizeBackgroundMapId(mapId)] || "black";
  }

  function readStoredBackgroundMapId() {
    try {
      return normalizeBackgroundMapId(
        window.localStorage.getItem(BACKGROUND_MAP_STORAGE_KEY)
      );
    } catch (error) {
      return DEFAULT_BACKGROUND_MAP_ID;
    }
  }

  function readStoredBoundaryLineMode() {
    try {
      return normalizeBoundaryLineMode(
        window.localStorage.getItem(BOUNDARY_LINE_STORAGE_KEY)
      );
    } catch (error) {
      return DEFAULT_BOUNDARY_LINE_MODE;
    }
  }

  function readStoredOutlineLayers(hasVillageLayer) {
    try {
      return normalizeOutlineLayers(
        window.localStorage.getItem(OUTLINE_STORAGE_KEY),
        hasVillageLayer
      );
    } catch (error) {
      return defaultOutlineLayers(hasVillageLayer);
    }
  }

  function storeBackgroundMapId(mapId) {
    try {
      window.localStorage.setItem(BACKGROUND_MAP_STORAGE_KEY, mapId);
    } catch (error) {
      return;
    }
  }

  function storeBoundaryLineMode(mode) {
    try {
      window.localStorage.setItem(BOUNDARY_LINE_STORAGE_KEY, mode);
    } catch (error) {
      return;
    }
  }

  function storeOutlineLayers(layers) {
    try {
      window.localStorage.setItem(
        OUTLINE_STORAGE_KEY,
        serializeOutlineLayers(layers)
      );
    } catch (error) {
      return;
    }
  }

  function syncBackgroundMapPickers(mapId) {
    document.querySelectorAll("[data-background-map-picker]").forEach(
      function (picker) {
        picker.value = mapId;
      }
    );
  }

  function syncBoundaryLinePickers(mode) {
    document.querySelectorAll("[data-boundary-line-picker]").forEach(
      function (picker) {
        picker.value = mode;
      }
    );
  }

  function syncOutlineLayerPickers(layers, hasVillageLayer) {
    document.querySelectorAll("[data-outline-layer-picker]").forEach(
      function (picker) {
        const isVillagePicker = picker.value === "villages";
        const isAvailable = !isVillagePicker || hasVillageLayer;
        const setting = picker.closest("[data-outline-layer-setting]");
        if (setting) {
          setting.hidden = !isAvailable;
        }
        picker.disabled = !isAvailable;
        picker.checked = isAvailable && layers.has(picker.value);
      }
    );
  }

  function hasOutlineLayer(layers, layer) {
    return layers instanceof Set && layers.has(layer);
  }

  function boundaryLineColors(theme) {
    if (theme === "black") {
      return {
        canton: "#e5322d",
        municipality: "#05080a",
        village: "#f6d64a",
      };
    }
    return {
      canton: "#e5322d",
      municipality: "#ffffff",
      village: "#f6d64a",
    };
  }

  function applyBoundaryLineTheme(map, boundaryState, revealState, summaryState) {
    const theme = resolveBoundaryLineTheme(
      boundaryState.mapId,
      boundaryState.lineMode
    );
    const colors = boundaryLineColors(theme);
    if (boundaryState.municipalityLayer !== null) {
      boundaryState.municipalityLayer.setStyle(
        municipalityStyle(
          revealState,
          summaryState,
          colors,
          boundaryState.outlineLayers,
          boundaryState.hasMunicipalityOverlay
        )
      );
    }
    if (boundaryState.municipalityOverlayLayer !== null) {
      boundaryState.municipalityOverlayLayer.setStyle(
        municipalityOverlayStyle(
          colors,
          boundaryState.outlineLayers
        )
      );
    }
    if (boundaryState.cantonLayer !== null) {
      boundaryState.cantonLayer.setStyle(
        cantonStyle(colors, boundaryState.outlineLayers)
      );
    }
  }

  function addBaseMapLayer(map, mapId, fallbackUrl) {
    const normalizedMapId = normalizeBackgroundMapId(mapId);
    const backgroundMap = BACKGROUND_MAPS[normalizedMapId];
    const url = backgroundMap.url || fallbackUrl;
    if (normalizedMapId === "none") {
      return null;
    }
    if (!url) {
      return null;
    }

    const layerOptions = {
      attribution: backgroundMap.attribution,
      maxNativeZoom: backgroundMap.maxNativeZoom,
      maxZoom: backgroundMap.maxZoom,
      minZoom: backgroundMap.minZoom,
    };
    if (backgroundMap.subdomains) {
      layerOptions.subdomains = backgroundMap.subdomains;
    }

    return window.L.tileLayer(url, layerOptions).addTo(map);
  }

  function initializeBackgroundMapPicker(
    map,
    baseLayerState,
    boundaryState,
    revealState,
    summaryState,
    fallbackUrl
  ) {
    const pickers = document.querySelectorAll("[data-background-map-picker]");
    if (!pickers.length) {
      return;
    }

    syncBackgroundMapPickers(baseLayerState.mapId);
    pickers.forEach(function (picker) {
      picker.addEventListener("change", function () {
        const mapId = normalizeBackgroundMapId(picker.value);
        storeBackgroundMapId(mapId);
        syncBackgroundMapPickers(mapId);
        if (baseLayerState.layer !== null) {
          map.removeLayer(baseLayerState.layer);
        }
        baseLayerState.mapId = mapId;
        boundaryState.mapId = mapId;
        baseLayerState.layer = addBaseMapLayer(map, mapId, fallbackUrl);
        applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      });
    });
  }

  function initializeBoundaryLinePicker(
    map,
    boundaryState,
    revealState,
    summaryState
  ) {
    const pickers = document.querySelectorAll("[data-boundary-line-picker]");
    if (!pickers.length) {
      return;
    }

    syncBoundaryLinePickers(boundaryState.lineMode);
    pickers.forEach(function (picker) {
      picker.addEventListener("change", function () {
        const lineMode = normalizeBoundaryLineMode(picker.value);
        storeBoundaryLineMode(lineMode);
        syncBoundaryLinePickers(lineMode);
        boundaryState.lineMode = lineMode;
        applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      });
    });
  }

  function initializeOutlineLayerPickers(
    map,
    boundaryState,
    revealState,
    summaryState
  ) {
    const pickers = document.querySelectorAll("[data-outline-layer-picker]");
    if (!pickers.length) {
      return;
    }

    syncOutlineLayerPickers(
      boundaryState.outlineLayers,
      boundaryState.hasMunicipalityOverlay
    );
    pickers.forEach(function (picker) {
      picker.addEventListener("change", function () {
        const layer = picker.value;
        const outlineLayers = new Set(boundaryState.outlineLayers);
        if (picker.checked) {
          outlineLayers.add(layer);
        } else {
          outlineLayers.delete(layer);
        }
        const normalizedLayers = normalizeOutlineLayers(
          serializeOutlineLayers(outlineLayers),
          boundaryState.hasMunicipalityOverlay
        );
        storeOutlineLayers(normalizedLayers);
        syncOutlineLayerPickers(
          normalizedLayers,
          boundaryState.hasMunicipalityOverlay
        );
        boundaryState.outlineLayers = normalizedLayers;
        applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      });
    });
  }

  function initializeMapSettingsMenu() {
    const toggle = document.querySelector("[data-map-settings-toggle]");
    const panel = document.querySelector("[data-map-settings-panel]");
    if (!toggle || !panel || toggle.dataset.initialized === "true") {
      return;
    }

    function setOpen(isOpen) {
      panel.hidden = !isOpen;
      toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
      if (isOpen) {
        const picker = panel.querySelector("select");
        if (picker) {
          picker.focus();
        }
      }
    }

    toggle.addEventListener("click", function (event) {
      event.stopPropagation();
      setOpen(panel.hidden);
    });
    panel.addEventListener("click", function (event) {
      event.stopPropagation();
    });
    document.addEventListener("click", function () {
      if (!panel.hidden) {
        setOpen(false);
      }
    });
    document.addEventListener(
      "keydown",
      function (event) {
        if (event.repeat) {
          return;
        }
        if (
          event.key !== "Escape" ||
          hasOpenAuthModal() ||
          isFormShortcutTarget(event.target)
        ) {
          return;
        }
        event.preventDefault();
        setOpen(panel.hidden);
        if (panel.hidden) {
          toggle.focus();
        }
      },
      true
    );
    toggle.dataset.initialized = "true";
  }

  function hasOpenAuthModal() {
    const modal = document.querySelector("[data-auth-modal]");
    return Boolean(modal && !modal.hidden);
  }

  function isEditableShortcutTarget(target) {
    if (!(target instanceof Element)) {
      return false;
    }
    return Boolean(
      target.closest(
        'a[href], button, input, select, textarea, [contenteditable="true"]'
      )
    );
  }

  function isFormShortcutTarget(target) {
    if (!(target instanceof Element)) {
      return false;
    }
    return Boolean(
      target.closest('input, select, textarea, [contenteditable="true"]')
    );
  }

  function isVisibleKeyboardAction(element) {
    if (!element || element.disabled || element.hidden) {
      return false;
    }
    if (element.getAttribute("aria-disabled") === "true") {
      return false;
    }
    return element.offsetParent !== null;
  }

  function currentGameKeyboardAction() {
    return Array.from(
      document.querySelectorAll("[data-game-keyboard-action]")
    ).find(isVisibleKeyboardAction);
  }

  function isActionKey(event) {
    return (
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.shiftKey &&
      (event.key === "Enter" || event.key === " " || event.key === "Spacebar")
    );
  }

  function initializeGameKeyboardShortcuts() {
    if (document.documentElement.dataset.gameKeyboardShortcuts === "true") {
      return;
    }

    document.addEventListener(
      "keydown",
      function (event) {
        if (event.repeat) {
          return;
        }
        if (
          hasOpenAuthModal() ||
          isEditableShortcutTarget(event.target) ||
          !isActionKey(event)
        ) {
          return;
        }

        const action = currentGameKeyboardAction();
        if (!action) {
          return;
        }
        event.preventDefault();
        action.click();
      },
      true
    );

    document.documentElement.dataset.gameKeyboardShortcuts = "true";
  }

  function formatCoordinate(value) {
    return value.toFixed(5);
  }

  function createGuessMarkerIcon(label, revealed) {
    let className = "guess-marker";
    if (label) {
      className += " guess-marker--numbered";
    }
    if (revealed) {
      className += " guess-marker--revealed";
    }
    const markerLabel = label
      ? '<span class="guess-marker-label">' + escapeHtml(label) + "</span>"
      : "";
    return window.L.divIcon({
      className: className,
      html: (
        '<span class="guess-marker-head">' +
        markerLabel +
        "</span>" +
        '<span class="guess-marker-stem"></span>'
      ),
      iconAnchor: [14, 36],
      iconSize: [29, 36],
    });
  }

  function createGuessMarker(map, latlng, label) {
    return window.L.marker(latlng, {
      icon: createGuessMarkerIcon(label, false),
      interactive: false,
      keyboard: false,
      zIndexOffset: 750,
    }).addTo(map);
  }

  function createRevealedGuessMarker(map, latlng, label) {
    return window.L.marker(latlng, {
      icon: createGuessMarkerIcon(label, true),
      interactive: false,
      keyboard: false,
      zIndexOffset: 760,
    }).addTo(map);
  }

  function initializeGuessInteraction(map, mapElement) {
    const form = document.querySelector("[data-guess-form]");
    if (!form) {
      return;
    }

    const latitudeInput = form.querySelector("[data-guess-lat]");
    const longitudeInput = form.querySelector("[data-guess-lng]");
    const confirmButton = form.querySelector("[data-confirm-guess]");
    let marker = null;
    let selectedLatLng = null;

    map.on("click", function (event) {
      const latitude = formatCoordinate(event.latlng.lat);
      const longitude = formatCoordinate(event.latlng.lng);
      const previousLatLng = selectedLatLng;
      const hadMarker = marker !== null;
      selectedLatLng = event.latlng;

      if (marker === null) {
        marker = createGuessMarker(map, selectedLatLng);
      } else {
        marker.setLatLng(selectedLatLng);
      }

      latitudeInput.value = latitude;
      longitudeInput.value = longitude;
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
    const boundaryLatitude = Number.parseFloat(mapElement.dataset.revealBoundaryLat);
    const boundaryLongitude = Number.parseFloat(mapElement.dataset.revealBoundaryLng);
    const latitude = Number.parseFloat(mapElement.dataset.revealLat);
    const longitude = Number.parseFloat(mapElement.dataset.revealLng);
    const distance = Number.parseFloat(mapElement.dataset.revealDistance);
    if (!targetId || !Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return null;
    }
    return {
      boundaryLatLng:
        Number.isFinite(boundaryLatitude) && Number.isFinite(boundaryLongitude)
          ? window.L.latLng(boundaryLatitude, boundaryLongitude)
          : null,
      distance: Number.isFinite(distance) ? distance : null,
      latlng: window.L.latLng(latitude, longitude),
      targetId: targetId,
    };
  }

  function readSummaryState() {
    const summaryElement = document.getElementById("game-summary-reveals");
    if (!summaryElement) {
      return null;
    }

    let rawReveals = [];
    try {
      rawReveals = JSON.parse(summaryElement.textContent || "[]");
    } catch (error) {
      rawReveals = [];
    }

    const reveals = (Array.isArray(rawReveals) ? rawReveals : [])
      .map(function (reveal) {
        if (!reveal || typeof reveal !== "object") {
          return null;
        }
        const boundaryLatitude = Number.parseFloat(reveal.boundaryLat);
        const boundaryLongitude = Number.parseFloat(reveal.boundaryLng);
        const latitude = Number.parseFloat(reveal.lat);
        const longitude = Number.parseFloat(reveal.lng);
        const distance = Number.parseFloat(reveal.distance);
        const score = Number.parseInt(reveal.score, 10);
        const turnNumber = Number.parseInt(reveal.turnNumber, 10);
        const targetId = reveal.targetId;
        if (
          !targetId ||
          !Number.isFinite(latitude) ||
          !Number.isFinite(longitude) ||
          !Number.isFinite(distance) ||
          !Number.isInteger(score) ||
          !Number.isInteger(turnNumber)
        ) {
          return null;
        }
        return {
          boundaryLatLng:
            Number.isFinite(boundaryLatitude) && Number.isFinite(boundaryLongitude)
              ? window.L.latLng(boundaryLatitude, boundaryLongitude)
              : null,
          distance: distance,
          latlng: window.L.latLng(latitude, longitude),
          score: score,
          targetId: String(targetId),
          turnNumber: turnNumber,
        };
      })
      .filter(Boolean);

    if (reveals.length === 0) {
      return null;
    }

    return {
      reveals: reveals,
      targetIds: new Set(
        reveals.map(function (reveal) {
          return reveal.targetId;
        })
      ),
    };
  }

  function initializeReveal(map, revealState, mapElement) {
    createRevealedGuessMarker(map, revealState.latlng);
    map.getContainer().classList.add("game-map--reveal");
    sendTrackingEvent(mapElement, "REVEAL_SHOWN", {
      latitude: revealState.latlng.lat,
      longitude: revealState.latlng.lng,
      target_municipality_id: Number(revealState.targetId),
      zoom: map.getZoom(),
    });
  }

  function initializeSummary(map, summaryState) {
    map.getContainer().classList.add("game-map--summary");
    summaryState.reveals.forEach(function (reveal) {
      createRevealedGuessMarker(map, reveal.latlng, String(reveal.turnNumber));
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

  function isSummaryTargetFeature(feature, summaryState) {
    return Boolean(
      summaryState &&
      feature &&
      feature.properties &&
      summaryState.targetIds.has(String(feature.properties.id))
    );
  }

  function hiddenBoundaryStyle() {
    return {
      fillOpacity: 0,
      opacity: 0,
      weight: 0,
    };
  }

  function municipalityStyle(
    revealState,
    summaryState,
    colors,
    outlineLayers,
    isVillageLayer
  ) {
    return function (feature) {
      if (
        (revealState && isTargetFeature(feature, revealState.targetId)) ||
        isSummaryTargetFeature(feature, summaryState)
      ) {
        return {
          color: "#7cff8b",
          fillColor: "#1b8f5a",
          fillOpacity: 0.28,
          opacity: 1,
          weight: 2,
        };
      }
      const visibleMode = isVillageLayer ? "villages" : "municipalities";
      if (!hasOutlineLayer(outlineLayers, visibleMode)) {
        return hiddenBoundaryStyle();
      }
      if (isVillageLayer) {
        return {
          color: colors.village,
          fillOpacity: 0,
          opacity: 0.86,
          weight: 1.15,
        };
      }
      return {
        color: colors.municipality,
        fillOpacity: 0,
        opacity: colors.municipality === "#ffffff" ? 0.75 : 0.84,
        weight: colors.municipality === "#ffffff" ? 0.6 : 0.75,
      };
    };
  }

  function municipalityOverlayStyle(colors, outlineLayers) {
    if (!hasOutlineLayer(outlineLayers, "municipalities")) {
      return hiddenBoundaryStyle();
    }
    return {
      color: colors.municipality,
      dashArray: "4 5",
      fillOpacity: 0,
      opacity: colors.municipality === "#ffffff" ? 0.62 : 0.7,
      weight: colors.municipality === "#ffffff" ? 1 : 1.15,
    };
  }

  function cantonStyle(colors, outlineLayers) {
    if (!hasOutlineLayer(outlineLayers, "cantons")) {
      return hiddenBoundaryStyle();
    }
    return {
      color: colors.canton,
      fillOpacity: 0,
      opacity: 0.92,
      weight: 2.6,
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

  function latLngFromCoordinate(coordinate) {
    return window.L.latLng(coordinate[1], coordinate[0]);
  }

  function geometryRings(geometry) {
    if (!geometry) {
      return [];
    }
    if (geometry.type === "Polygon") {
      return geometry.coordinates;
    }
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.reduce(function (rings, polygon) {
        return rings.concat(polygon);
      }, []);
    }
    return [];
  }

  function closestPointOnSegment(point, segmentStart, segmentEnd) {
    const delta = segmentEnd.subtract(segmentStart);
    const lengthSquared = delta.x * delta.x + delta.y * delta.y;
    if (lengthSquared === 0) {
      return segmentStart;
    }

    const ratio = Math.max(
      0,
      Math.min(
        1,
        ((point.x - segmentStart.x) * delta.x +
          (point.y - segmentStart.y) * delta.y) /
          lengthSquared
      )
    );
    return window.L.point(
      segmentStart.x + ratio * delta.x,
      segmentStart.y + ratio * delta.y
    );
  }

  function squaredDistance(firstPoint, secondPoint) {
    const deltaX = firstPoint.x - secondPoint.x;
    const deltaY = firstPoint.y - secondPoint.y;
    return deltaX * deltaX + deltaY * deltaY;
  }

  function closestBoundaryPoint(map, feature, latlng) {
    const guessPoint = map.latLngToLayerPoint(latlng);
    let bestPoint = null;
    let bestDistance = Number.POSITIVE_INFINITY;

    geometryRings(feature.geometry).forEach(function (ring) {
      for (let index = 0; index < ring.length - 1; index += 1) {
        const segmentStart = map.latLngToLayerPoint(
          latLngFromCoordinate(ring[index])
        );
        const segmentEnd = map.latLngToLayerPoint(
          latLngFromCoordinate(ring[index + 1])
        );
        const candidate = closestPointOnSegment(
          guessPoint,
          segmentStart,
          segmentEnd
        );
        const distance = squaredDistance(guessPoint, candidate);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestPoint = candidate;
        }
      }
    });

    return bestPoint ? map.layerPointToLatLng(bestPoint) : null;
  }

  function shouldDrawRevealDistanceLine(distance) {
    return Number.isFinite(distance) && Math.round(distance) >= 1;
  }

  function drawRevealDistanceLine(map, municipalityLayer, revealState) {
    if (!shouldDrawRevealDistanceLine(revealState.distance)) {
      return;
    }

    let boundaryLatLng = revealState.boundaryLatLng;
    if (boundaryLatLng === null) {
      const targetLayer = findTargetLayer(municipalityLayer, revealState.targetId);
      if (targetLayer === null) {
        return;
      }

      boundaryLatLng = closestBoundaryPoint(
        map,
        targetLayer.feature,
        revealState.latlng
      );
    }
    if (boundaryLatLng === null) {
      return;
    }

    if (!map.getPane("revealDistancePane")) {
      map.createPane("revealDistancePane");
      map.getPane("revealDistancePane").style.zIndex = 590;
      map.getPane("revealDistancePane").style.pointerEvents = "none";
    }

    window.L.polyline([revealState.latlng, boundaryLatLng], {
      className: "reveal-distance-line reveal-distance-line-outline",
      color: "#ffffff",
      dashArray: "7 7",
      interactive: false,
      opacity: 0.58,
      pane: "revealDistancePane",
      weight: 4,
    }).addTo(map);
    window.L.polyline([revealState.latlng, boundaryLatLng], {
      className: "reveal-distance-line",
      color: "#05080a",
      dashArray: "7 7",
      interactive: false,
      opacity: 0.95,
      pane: "revealDistancePane",
      weight: 2.5,
    }).addTo(map);
  }

  function fitRevealBounds(map, municipalityLayer, revealState) {
    const bounds = window.L.latLngBounds([revealState.latlng]);
    const targetLayer = findTargetLayer(municipalityLayer, revealState.targetId);
    if (targetLayer !== null) {
      bounds.extend(targetLayer.getBounds());
    }

    map.fitBounds(bounds, mapFitOptions(map, 12, 42));
  }

  function fitSummaryBounds(map, municipalityLayer, summaryState) {
    const bounds = window.L.latLngBounds([]);
    summaryState.reveals.forEach(function (reveal) {
      bounds.extend(reveal.latlng);
      const targetLayer = findTargetLayer(municipalityLayer, reveal.targetId);
      if (targetLayer !== null) {
        bounds.extend(targetLayer.getBounds());
      }
    });

    if (bounds.isValid()) {
      map.fitBounds(bounds, mapFitOptions(map, 10, 64));
    }
  }

  function refitMapView(map, municipalityLayer, revealState, summaryState) {
    if (municipalityLayer === null) {
      return;
    }
    if (revealState) {
      fitRevealBounds(map, municipalityLayer, revealState);
    } else if (summaryState) {
      fitSummaryBounds(map, municipalityLayer, summaryState);
    } else {
      fitBoundaryLayerBounds(map, municipalityLayer);
    }
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
    const summaryState = readSummaryState();
    const vectorRenderer = window.L.canvas({
      padding: VECTOR_RENDERER_PADDING,
    });
    const map = window.L.map(mapElement, {
      attributionControl: true,
      maxBounds: switzerlandBounds,
      maxBoundsViscosity: 1,
      minZoom: DEFAULT_MIN_ZOOM,
      preferCanvas: true,
      renderer: vectorRenderer,
      worldCopyJump: false,
      zoomControl: true,
    });

    map.setView([latitude, longitude], zoom);
    constrainMapToBounds(map, switzerlandBounds);
    let municipalityLayerForFit = null;
    let resizeFitTimeout = null;
    function refreshMapFit() {
      map.invalidateSize();
      constrainMapToBounds(map, switzerlandBounds);
      refitMapView(map, municipalityLayerForFit, revealState, summaryState);
    }
    map.on("resize", function () {
      window.clearTimeout(resizeFitTimeout);
      resizeFitTimeout = window.setTimeout(refreshMapFit, 0);
    });
    const backgroundMapId = readStoredBackgroundMapId();
    const boundaryLineMode = readStoredBoundaryLineMode();
    const hasMunicipalityOverlay = Boolean(
      mapElement.dataset.municipalityOverlayUrl
    );
    const outlineLayers = readStoredOutlineLayers(hasMunicipalityOverlay);
    const baseLayerState = {
      layer: addBaseMapLayer(
        map,
        backgroundMapId,
        mapElement.dataset.baseMapUrl
      ),
      mapId: backgroundMapId,
    };
    const boundaryState = {
      cantonLayer: null,
      hasMunicipalityOverlay: hasMunicipalityOverlay,
      lineMode: boundaryLineMode,
      mapId: backgroundMapId,
      municipalityLayer: null,
      municipalityOverlayLayer: null,
      outlineLayers: outlineLayers,
    };
    const initialBoundaryColors = boundaryLineColors(
      resolveBoundaryLineTheme(boundaryState.mapId, boundaryState.lineMode)
    );
    applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
    initializeBackgroundMapPicker(
      map,
      baseLayerState,
      boundaryState,
      revealState,
      summaryState,
      mapElement.dataset.baseMapUrl
    );
    initializeBoundaryLinePicker(map, boundaryState, revealState, summaryState);
    initializeOutlineLayerPickers(map, boundaryState, revealState, summaryState);
    initializeMapSettingsMenu();
    window.L.control.scale({ imperial: false, metric: true }).addTo(map);
    if (revealState) {
      initializeReveal(map, revealState, mapElement);
      initializeNextTurnTracking(mapElement);
    } else if (summaryState) {
      initializeSummary(map, summaryState);
    } else {
      initializeGuessInteraction(map, mapElement);
    }
    mapElement.dataset.initialized = "true";
    addBoundaryLayer(map, mapElement.dataset.targetBoundariesUrl, {
      errorMessage: "Target boundaries could not be loaded.",
      fitBounds: !revealState && !summaryState,
      renderer: vectorRenderer,
      style: municipalityStyle(
        revealState,
        summaryState,
        initialBoundaryColors,
        boundaryState.outlineLayers,
        boundaryState.hasMunicipalityOverlay
      ),
    }).then(function (municipalityLayer) {
      boundaryState.municipalityLayer = municipalityLayer;
      municipalityLayerForFit = municipalityLayer;
      applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      if (revealState && municipalityLayer !== null) {
        fitRevealBounds(map, municipalityLayer, revealState);
        drawRevealDistanceLine(map, municipalityLayer, revealState);
      }
      if (summaryState && municipalityLayer !== null) {
        fitSummaryBounds(map, municipalityLayer, summaryState);
        summaryState.reveals.forEach(function (reveal) {
          drawRevealDistanceLine(map, municipalityLayer, reveal);
        });
      }
      return addBoundaryLayer(map, mapElement.dataset.municipalityOverlayUrl, {
        errorMessage: "Municipality overlay could not be loaded.",
        fitBounds: false,
        renderer: vectorRenderer,
        suppressGlobalError: true,
        style: municipalityOverlayStyle(
          initialBoundaryColors,
          boundaryState.outlineLayers
        ),
      });
    }).then(function (municipalityOverlayLayer) {
      boundaryState.municipalityOverlayLayer = municipalityOverlayLayer;
      applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      return addBoundaryLayer(map, mapElement.dataset.cantonBoundariesUrl, {
        errorMessage: "Canton boundaries could not be loaded.",
        fitBounds: false,
        renderer: vectorRenderer,
        style: cantonStyle(initialBoundaryColors, boundaryState.outlineLayers),
      });
    }).then(function (cantonLayer) {
      boundaryState.cantonLayer = cantonLayer;
      applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
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
      refreshMapFit();
    }, 0);
  }

  function initializeAuthChoiceModal() {
    const trigger = document.querySelector("[data-auth-modal-trigger]");
    const modal = document.querySelector("[data-auth-modal]");
    if (!trigger || !modal) {
      return;
    }

    const closeButtons = modal.querySelectorAll("[data-auth-modal-close]");
    const guestModeChoice = modal.querySelector("[data-guest-mode-choice]");
    const guestStartForm = document.querySelector("[data-guest-start-form]");
    const modePicker = document.querySelector("[data-game-mode-picker]");
    let returnFocusElement = trigger;

    function modalFocusableElements() {
      return Array.from(
        modal.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter(function (element) {
        return element.offsetParent !== null;
      });
    }

    function openModal() {
      returnFocusElement = document.activeElement || trigger;
      modal.hidden = false;
      const firstAction = modal.querySelector("[data-auth-primary], a, button");
      if (firstAction) {
        firstAction.focus();
      }
    }

    function closeModal() {
      modal.hidden = true;
      if (returnFocusElement && typeof returnFocusElement.focus === "function") {
        returnFocusElement.focus();
      }
    }

    function showGuestModePicker() {
      modal.hidden = true;
      trigger.hidden = true;
      if (guestStartForm) {
        guestStartForm.hidden = false;
      }

      const firstModeChoice = modePicker
        ? modePicker.querySelector("[data-mode-choice]")
        : null;
      if (firstModeChoice) {
        firstModeChoice.focus();
      } else if (guestStartForm) {
        const submitButton = guestStartForm.querySelector("button");
        if (submitButton) {
          submitButton.focus();
        }
      }
    }

    trigger.addEventListener("click", openModal);
    closeButtons.forEach(function (button) {
      button.addEventListener("click", closeModal);
    });
    if (guestModeChoice) {
      guestModeChoice.addEventListener("click", function (event) {
        event.preventDefault();
        showGuestModePicker();
      });
    }
    modal.addEventListener("click", function (event) {
      if (event.target === modal) {
        closeModal();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (modal.hidden) {
        return;
      }
      if (event.key === "Escape") {
        closeModal();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }

      const focusableElements = modalFocusableElements();
      if (focusableElements.length === 0) {
        event.preventDefault();
        return;
      }

      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];
      if (!modal.contains(document.activeElement)) {
        event.preventDefault();
        if (event.shiftKey) {
          lastElement.focus();
        } else {
          firstElement.focus();
        }
        return;
      }
      if (event.shiftKey && document.activeElement === firstElement) {
        event.preventDefault();
        lastElement.focus();
      } else if (!event.shiftKey && document.activeElement === lastElement) {
        event.preventDefault();
        firstElement.focus();
      }
    });
    if (modal.dataset.authModalOpen === "true") {
      openModal();
    }
  }

  function initializeGameModePicker() {
    const picker = document.querySelector("[data-game-mode-picker]");
    if (!picker) {
      return;
    }

    const modeChoices = picker.querySelectorAll("[data-mode-choice]");
    const cantonSelect = picker.querySelector("[data-canton-select]");
    const selectedCantonLabel = picker.querySelector("[data-selected-canton-label]");
    const mapLabel = document.querySelector("[data-game-mode-map-label]");
    if (!modeChoices.length || !cantonSelect) {
      return;
    }

    function selectedMode() {
      const checkedChoice = picker.querySelector("[data-mode-choice]:checked");
      return checkedChoice ? checkedChoice.value : "switzerland";
    }

    function updateModePreview() {
      const cantonMode = selectedMode() === "canton";
      const cantonCode = cantonSelect.value || "";
      const mapCode = cantonCode || "-";

      cantonSelect.disabled = !cantonMode;
      if (selectedCantonLabel) {
        selectedCantonLabel.textContent = mapCode;
      }
      if (mapLabel) {
        mapLabel.textContent = cantonMode ? mapCode : "CH";
      }
    }

    modeChoices.forEach(function (choice) {
      choice.addEventListener("change", updateModePreview);
    });
    cantonSelect.addEventListener("change", updateModePreview);
    updateModePreview();
  }

  function initializePage() {
    initializeGameMap();
    initializeGameModePicker();
    initializeAuthChoiceModal();
    initializeGameKeyboardShortcuts();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializePage);
  } else {
    initializePage();
  }
})();

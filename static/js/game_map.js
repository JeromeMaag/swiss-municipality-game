(function () {
  "use strict";

  const BACKGROUND_MAP_STORAGE_KEY = "gemeindeguess.backgroundMap";
  const BOUNDARY_LINE_STORAGE_KEY = "gemeindeguess.boundaryLines";
  const OUTLINE_STORAGE_KEY = "gemeindeguess.outlines";
  const DEFAULT_BACKGROUND_MAP_ID = "swissimage";
  const DEFAULT_BOUNDARY_LINE_MODE = "auto";
  const BOUNDARY_LINE_MODES = new Set(["auto", "white", "black"]);
  const BOUNDARY_CACHE_LIMIT = 32;
  const BOUNDARY_CACHE_NAME = "gemeindeguess.boundaries.v1";
  const BOUNDARY_DETAIL_FULL = "full";
  const BOUNDARY_DETAIL_SIMPLE = "simple";
  const BOUNDARY_FULL_ZOOM = {
    cantons: 8,
    municipalities: 10,
    villages: 12,
  };
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
  const FALLBACK_MAP_MIN_ZOOM = 8;
  const MIN_ZOOM_MODE_FIXED = "fixed";
  const MIN_ZOOM_MODE_COVER_SWITZERLAND = "coverSwitzerland";
  const DESKTOP_SIDEBAR_WIDTH = 360;
  const MOBILE_BREAKPOINT_WIDTH = 920;
  const BOUNDS_COVER_PADDING = 24;
  const COMPACT_MIN_FIT_PADDING = 12;
  const COMPACT_MIN_BOTTOM_PADDING = 180;
  const COMPACT_MIN_AVAILABLE_MAP_HEIGHT = 96;
  const COMPACT_SHORT_AVAILABLE_MAP_HEIGHT = 48;
  const COMPACT_SIDEBAR_HEIGHT_RATIO = 0.5;
  const COMPACT_TALL_SIDEBAR_HEIGHT_RATIO = 0.64;
  const COMPACT_TOP_PADDING_RATIO = 0.18;
  const VECTOR_RENDERER_PADDING = 0.2;
  const GUESS_CLICK_TOLERANCE = 14;
  const GUESS_TAP_TOLERANCE = 26;
  const GUESS_DRAG_SUPPRESSION_MS = 160;
  const REVEAL_LINE_DELAY_MS = 420;
  const REVEAL_PIN_DELAY_MS = 120;
  const REVEAL_TARGET_DELAY_MS = 260;

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
      minZoomFloor: 7,
      minZoomMode: MIN_ZOOM_MODE_COVER_SWITZERLAND,
      maxNativeZoom: 18,
      maxZoom: 18,
      minZoom: 6,
      url: swisstopoWmtsUrl("ch.swisstopo.swissimage", "jpeg"),
    },
    surfaceRelief: {
      attribution: "Map data &copy; swisstopo",
      minZoomFloor: 7,
      minZoomMode: MIN_ZOOM_MODE_COVER_SWITZERLAND,
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
      minZoomFloor: 7,
      minZoomMode: MIN_ZOOM_MODE_COVER_SWITZERLAND,
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
      mapMinZoom: 7,
      minZoomMode: MIN_ZOOM_MODE_FIXED,
      maxNativeZoom: 20,
      maxZoom: 20,
      minZoom: 0,
      subdomains: "abcd",
      url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png",
    },
    none: {
      attribution: "",
      mapMinZoom: 7,
      minZoomMode: MIN_ZOOM_MODE_FIXED,
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

  function readMapScopeBounds(element) {
    const rawBounds = element.dataset.scopeBounds || "";
    const parts = rawBounds.split(",").map(function (part) {
      return Number.parseFloat(part);
    });
    if (parts.length !== 4 || parts.some(function (part) {
      return !Number.isFinite(part);
    })) {
      return null;
    }

    return window.L.latLngBounds(
      [parts[0], parts[1]],
      [parts[2], parts[3]]
    );
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

  function backgroundMapConfig(mapId) {
    return BACKGROUND_MAPS[normalizeBackgroundMapId(mapId)];
  }

  function initialBackgroundMinZoom(mapId) {
    const backgroundMap = backgroundMapConfig(mapId);
    if (backgroundMap.minZoomMode === MIN_ZOOM_MODE_FIXED) {
      return backgroundMap.mapMinZoom || FALLBACK_MAP_MIN_ZOOM;
    }
    if (backgroundMap.minZoomMode === MIN_ZOOM_MODE_COVER_SWITZERLAND) {
      return backgroundMap.minZoomFloor || FALLBACK_MAP_MIN_ZOOM;
    }
    return FALLBACK_MAP_MIN_ZOOM;
  }

  function effectiveBoundsCoverSize(map) {
    const size = map.getSize();
    if (isCompactMap(map)) {
      const compactPadding = compactMapEdgePadding(map, BOUNDS_COVER_PADDING);
      return {
        x: Math.max(1, size.x - compactPadding * 2),
        y: Math.max(
          1,
          size.y -
            compactPadding -
            compactMapBottomPadding(map, compactPadding)
        ),
      };
    }

    return {
      x: Math.max(
        1,
        size.x - DESKTOP_SIDEBAR_WIDTH - BOUNDS_COVER_PADDING * 2
      ),
      y: Math.max(1, size.y - BOUNDS_COVER_PADDING * 2),
    };
  }

  function projectedBoundsSize(map, bounds, zoom) {
    const northWest = map.project(bounds.getNorthWest(), zoom);
    const southEast = map.project(bounds.getSouthEast(), zoom);
    return {
      x: Math.abs(southEast.x - northWest.x),
      y: Math.abs(southEast.y - northWest.y),
    };
  }

  function resolveBackgroundMinZoom(map, mapId, bounds) {
    const backgroundMap = backgroundMapConfig(mapId);
    if (backgroundMap.minZoomMode === MIN_ZOOM_MODE_FIXED) {
      return backgroundMap.mapMinZoom || FALLBACK_MAP_MIN_ZOOM;
    }
    if (backgroundMap.minZoomMode !== MIN_ZOOM_MODE_COVER_SWITZERLAND) {
      return FALLBACK_MAP_MIN_ZOOM;
    }

    const minZoomFloor = backgroundMap.minZoomFloor || FALLBACK_MAP_MIN_ZOOM;
    const maxZoom = Number.isFinite(backgroundMap.maxZoom)
      ? backgroundMap.maxZoom
      : FALLBACK_MAP_MIN_ZOOM;
    const visibleSize = effectiveBoundsCoverSize(map);
    for (let zoom = minZoomFloor; zoom <= maxZoom; zoom += 1) {
      const boundsSize = projectedBoundsSize(map, bounds, zoom);
      if (boundsSize.x >= visibleSize.x && boundsSize.y >= visibleSize.y) {
        return zoom;
      }
    }
    return maxZoom;
  }

  function applyBackgroundMinZoom(map, mapId, bounds) {
    const minZoom = resolveBackgroundMinZoom(map, mapId, bounds);
    map.setMinZoom(minZoom);
    map.fire("minzoomchange");
    if (map.getZoom() < minZoom) {
      map.setZoom(minZoom, { animate: false });
    }
    return minZoom;
  }

  function constrainMapToBounds(map, bounds, mapId) {
    applyBackgroundMinZoom(map, mapId, bounds);
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

  function isSameOriginUrl(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (error) {
      return false;
    }
  }

  function trimBoundaryCache(cache) {
    if (!cache || typeof cache.keys !== "function") {
      return;
    }

    cache.keys().then(function (keys) {
      if (keys.length <= BOUNDARY_CACHE_LIMIT) {
        return null;
      }
      return Promise.all(
        keys.slice(0, keys.length - BOUNDARY_CACHE_LIMIT).map(function (key) {
          return cache.delete(key);
        })
      );
    }).catch(function () {
      return null;
    });
  }

  function boundaryUrlWithDetail(url, detail) {
    const boundaryUrl = new URL(url, window.location.href);
    boundaryUrl.searchParams.set("detail", detail);
    return boundaryUrl.toString();
  }

  function fetchBoundaryData(url) {
    if (!window.caches || !isSameOriginUrl(url)) {
      return window.fetch(url, {
        credentials: "same-origin",
        headers: {
          Accept: "application/geo+json, application/json",
        },
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("Boundary request failed with status " + response.status);
        }
        return response.json();
      });
    }

    return window.caches.open(BOUNDARY_CACHE_NAME).then(function (cache) {
      return cache.match(url).then(function (cachedResponse) {
        if (cachedResponse) {
          return cachedResponse.json();
        }

        return window.fetch(url, {
          credentials: "same-origin",
          headers: {
            Accept: "application/geo+json, application/json",
          },
        }).then(function (response) {
          if (!response.ok) {
            throw new Error(
              "Boundary request failed with status " + response.status
            );
          }
          cache.put(url, response.clone()).then(function () {
            trimBoundaryCache(cache);
            return null;
          }).catch(function () {
            return null;
          });
          return response.json();
        });
      });
    });
  }

  function addBoundaryLayer(map, url, options) {
    if (!url) {
      return Promise.resolve(null);
    }

    return fetchBoundaryData(url)
      .then(function (response) {
        const layer = window.L.geoJSON(response, {
          interactive: false,
          onEachFeature: options.onEachFeature,
          renderer: options.renderer,
          style: options.style,
        });

        if (options.addToMap !== false) {
          layer.addTo(map);
        }

        if (
          options.fitBounds &&
          options.addToMap !== false &&
          layer.getLayers().length > 0
        ) {
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

  function boundaryDetailForZoom(map, layerId) {
    const fullZoom = BOUNDARY_FULL_ZOOM[layerId] || Number.POSITIVE_INFINITY;
    return map.getZoom() >= fullZoom ? BOUNDARY_DETAIL_FULL : BOUNDARY_DETAIL_SIMPLE;
  }

  function isBoundaryLayerVisible(boundaryState, layerId) {
    return hasOutlineLayer(boundaryState.outlineLayers, layerId);
  }

  function createBoundaryLayerManager(map, options) {
    const layersByDetail = {};
    const requestsByDetail = {};
    let currentDetail = null;
    let syncToken = 0;

    function currentLayer() {
      return currentDetail ? layersByDetail[currentDetail] || null : null;
    }

    function desiredDetail() {
      return boundaryDetailForZoom(map, options.layerId);
    }

    function shouldShow() {
      return options.required() || options.visible();
    }

    function removeCurrentLayer() {
      const layer = currentLayer();
      if (layer !== null && map.hasLayer(layer)) {
        map.removeLayer(layer);
      }
      currentDetail = null;
    }

    function loadLayer(detail) {
      if (layersByDetail[detail]) {
        return Promise.resolve(layersByDetail[detail]);
      }
      if (requestsByDetail[detail]) {
        return requestsByDetail[detail];
      }

      requestsByDetail[detail] = addBoundaryLayer(
        map,
        boundaryUrlWithDetail(options.url, detail),
        {
          addToMap: false,
          errorMessage: options.errorMessage,
          onEachFeature: options.onEachFeature,
          renderer: options.renderer,
          style: options.style,
          suppressGlobalError: options.suppressGlobalError,
        }
      ).then(function (layer) {
        requestsByDetail[detail] = null;
        if (layer !== null) {
          layersByDetail[detail] = layer;
        }
        return layer;
      });
      return requestsByDetail[detail];
    }

    function showLayer(layer, detail, fitBounds) {
      const previousLayer = currentLayer();
      if (previousLayer !== null && previousLayer !== layer && map.hasLayer(previousLayer)) {
        map.removeLayer(previousLayer);
      }
      currentDetail = detail;
      layer.setStyle(options.style());
      if (!map.hasLayer(layer)) {
        layer.addTo(map);
      }
      if (fitBounds && layer.getLayers().length > 0) {
        fitBoundaryLayerBounds(map, layer);
      }
      return layer;
    }

    function sync(syncOptions) {
      const fitBounds = Boolean(syncOptions && syncOptions.fitBounds);
      syncToken += 1;
      const token = syncToken;
      if (!options.url || (!shouldShow() && !fitBounds)) {
        removeCurrentLayer();
        return Promise.resolve(null);
      }

      const detail = desiredDetail();
      return loadLayer(detail).then(function (layer) {
        if (token !== syncToken || layer === null || detail !== desiredDetail()) {
          return currentLayer();
        }
        if (!shouldShow()) {
          removeCurrentLayer();
          // Use hidden target data for scope fitting without rendering the layer.
          if (fitBounds && layer.getLayers().length > 0) {
            fitBoundaryLayerBounds(map, layer);
          }
          return layer;
        }
        return showLayer(layer, detail, fitBounds);
      });
    }

    function setStyle() {
      Object.keys(layersByDetail).forEach(function (detail) {
        layersByDetail[detail].setStyle(options.style());
      });
    }

    return {
      getLayer: currentLayer,
      setStyle: setStyle,
      sync: sync,
    };
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

  function defaultOutlineLayers(hasMunicipalityLayer, hasVillageLayer) {
    const layers = new Set(["cantons"]);
    if (hasMunicipalityLayer) {
      layers.add("municipalities");
    }
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

  function normalizeOutlineLayers(value, hasMunicipalityLayer, hasVillageLayer) {
    if (!value) {
      return defaultOutlineLayers(hasMunicipalityLayer, hasVillageLayer);
    }
    if (value === "all") {
      return defaultOutlineLayers(hasMunicipalityLayer, hasVillageLayer);
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
        if (layer === "municipalities" && !hasMunicipalityLayer) {
          return;
        }
        if (layer === "villages" && !hasVillageLayer) {
          return;
        }
        layers.add(layer);
      });

    if (layers.size === 0) {
      return defaultOutlineLayers(hasMunicipalityLayer, hasVillageLayer);
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

  function readStoredOutlineLayers(hasMunicipalityLayer, hasVillageLayer) {
    try {
      return normalizeOutlineLayers(
        window.localStorage.getItem(OUTLINE_STORAGE_KEY),
        hasMunicipalityLayer,
        hasVillageLayer
      );
    } catch (error) {
      return defaultOutlineLayers(hasMunicipalityLayer, hasVillageLayer);
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

  function syncOutlineLayerPickers(
    layers,
    hasMunicipalityLayer,
    hasVillageLayer
  ) {
    document.querySelectorAll("[data-outline-layer-picker]").forEach(
      function (picker) {
        const isMunicipalityPicker = picker.value === "municipalities";
        const isVillagePicker = picker.value === "villages";
        const isAvailable =
          (!isMunicipalityPicker || hasMunicipalityLayer) &&
          (!isVillagePicker || hasVillageLayer);
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

  function currentBoundaryLineColors(boundaryState) {
    return boundaryLineColors(
      resolveBoundaryLineTheme(boundaryState.mapId, boundaryState.lineMode)
    );
  }

  function applyBoundaryLineTheme(map, boundaryState, revealState, summaryState) {
    const colors = currentBoundaryLineColors(boundaryState);
    if (boundaryState.targetLayerManager !== null) {
      boundaryState.targetLayerManager.setStyle();
    } else if (boundaryState.municipalityLayer !== null) {
      boundaryState.municipalityLayer.setStyle(
        municipalityStyle(
          revealState,
          summaryState,
          colors,
          boundaryState.outlineLayers,
          boundaryState.hasVillageLayer,
          boundaryState.revealTargetVisible
        )
      );
    }
    if (boundaryState.municipalityOverlayLayerManager !== null) {
      boundaryState.municipalityOverlayLayerManager.setStyle();
    } else if (boundaryState.municipalityOverlayLayer !== null) {
      boundaryState.municipalityOverlayLayer.setStyle(
        municipalityOverlayStyle(
          colors,
          boundaryState.outlineLayers
        )
      );
    }
    if (boundaryState.cantonLayerManager !== null) {
      boundaryState.cantonLayerManager.setStyle();
    } else if (boundaryState.cantonLayer !== null) {
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
    bounds,
    scopeBounds,
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
        constrainMapToBounds(map, bounds, mapId);
        refitMapView(
          map,
          boundaryState.municipalityLayer,
          revealState,
          summaryState,
          scopeBounds
        );
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
    summaryState,
    syncBoundaryLayers
  ) {
    const pickers = document.querySelectorAll("[data-outline-layer-picker]");
    if (!pickers.length) {
      return;
    }

    syncOutlineLayerPickers(
      boundaryState.outlineLayers,
      boundaryState.hasMunicipalityLayer,
      boundaryState.hasVillageLayer
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
          boundaryState.hasMunicipalityLayer,
          boundaryState.hasVillageLayer
        );
        storeOutlineLayers(normalizedLayers);
        syncOutlineLayerPickers(
          normalizedLayers,
          boundaryState.hasMunicipalityLayer,
          boundaryState.hasVillageLayer
        );
        boundaryState.outlineLayers = normalizedLayers;
        applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
        syncBoundaryLayers();
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

  function initializeMapZoomControls(map) {
    const zoomInButton = document.querySelector("[data-map-zoom-in]");
    const zoomOutButton = document.querySelector("[data-map-zoom-out]");
    if (!zoomInButton || !zoomOutButton) {
      return function () {
        return null;
      };
    }

    function syncZoomButtons() {
      zoomInButton.disabled = map.getZoom() >= map.getMaxZoom();
      zoomOutButton.disabled = map.getZoom() <= map.getMinZoom();
    }

    zoomInButton.addEventListener("click", function () {
      if (!zoomInButton.disabled) {
        map.zoomIn();
      }
    });
    zoomOutButton.addEventListener("click", function () {
      if (!zoomOutButton.disabled) {
        map.zoomOut();
      }
    });
    map.on("zoomend zoomlevelschange minzoomchange", syncZoomButtons);
    syncZoomButtons();
    return syncZoomButtons;
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

  function eventClientPoint(event) {
    const touch =
      event.touches && event.touches.length
        ? event.touches[0]
        : event.changedTouches && event.changedTouches.length
          ? event.changedTouches[0]
          : null;
    if (touch) {
      return { x: touch.clientX, y: touch.clientY };
    }
    if (
      Number.isFinite(event.clientX) &&
      Number.isFinite(event.clientY)
    ) {
      return { x: event.clientX, y: event.clientY };
    }
    return null;
  }

  function isPrimaryGuessPress(event) {
    if (event.isPrimary === false) {
      return false;
    }
    return event.button === undefined || event.button === 0;
  }

  function guessPressTolerance(event) {
    return event.pointerType === "touch" || event.type.startsWith("touch")
      ? GUESS_TAP_TOLERANCE
      : GUESS_CLICK_TOLERANCE;
  }

  function pointsMovedPastTolerance(startPoint, currentPoint, tolerance) {
    const deltaX = currentPoint.x - startPoint.x;
    const deltaY = currentPoint.y - startPoint.y;
    return deltaX * deltaX + deltaY * deltaY > tolerance * tolerance;
  }

  function supportsGhostGuessPin() {
    return (
      window.matchMedia &&
      window.matchMedia("(hover: hover) and (pointer: fine)").matches
    );
  }

  function shouldShowGhostPinForEvent(event) {
    return event.pointerType !== "touch" && !event.type.startsWith("touch");
  }

  function isLeafletControlTarget(target) {
    return Boolean(
      target &&
        target.closest &&
        target.closest(".leaflet-control-container")
    );
  }

  function guessMarkerHtml(label) {
    const markerLabel = label
      ? '<span class="guess-marker-label">' + escapeHtml(label) + "</span>"
      : "";
    return (
      '<span class="guess-marker-head">' +
      markerLabel +
      "</span>" +
      '<span class="guess-marker-stem"></span>'
    );
  }

  function createGuessMarkerIcon(label, revealed) {
    let className = "guess-marker";
    if (label) {
      className += " guess-marker--numbered";
    }
    if (revealed) {
      className += " guess-marker--revealed";
    }
    return window.L.divIcon({
      className: className,
      html: guessMarkerHtml(label),
      iconAnchor: [14, 36],
      iconSize: [29, 36],
    });
  }

  function createGhostGuessMarker(mapContainer) {
    const ghostMarker = document.createElement("div");
    ghostMarker.className = "guess-marker guess-marker--ghost";
    ghostMarker.hidden = true;
    ghostMarker.setAttribute("aria-hidden", "true");
    ghostMarker.innerHTML = guessMarkerHtml("");
    mapContainer.appendChild(ghostMarker);
    return ghostMarker;
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

    const mapContainer = map.getContainer();
    const latitudeInput = form.querySelector("[data-guess-lat]");
    const longitudeInput = form.querySelector("[data-guess-lng]");
    const confirmButton = form.querySelector("[data-confirm-guess]");
    let marker = null;
    let selectedLatLng = null;
    let pressState = null;
    let pressMovedPastTolerance = false;
    let mapDragging = false;
    let lastDragAt = 0;
    const ghostMarker = supportsGhostGuessPin()
      ? createGhostGuessMarker(mapContainer)
      : null;

    if (ghostMarker) {
      mapContainer.classList.add("game-map--guessing");
    }

    function resetPressState() {
      pressState = null;
      pressMovedPastTolerance = false;
    }

    function hideGhostMarker() {
      if (ghostMarker) {
        ghostMarker.hidden = true;
      }
    }

    function updateGhostMarker(event) {
      if (
        !ghostMarker ||
        mapDragging ||
        pressState !== null ||
        isLeafletControlTarget(event.target) ||
        !shouldShowGhostPinForEvent(event)
      ) {
        hideGhostMarker();
        return;
      }
      const point = eventClientPoint(event);
      if (!point) {
        hideGhostMarker();
        return;
      }
      const mapRect = mapContainer.getBoundingClientRect();
      ghostMarker.style.left = point.x - mapRect.left + "px";
      ghostMarker.style.top = point.y - mapRect.top + "px";
      ghostMarker.hidden = false;
    }

    function beginGuessPress(event) {
      if (!isPrimaryGuessPress(event)) {
        return;
      }
      hideGhostMarker();
      const point = eventClientPoint(event);
      if (!point) {
        resetPressState();
        return;
      }
      pressState = {
        point: point,
        tolerance: guessPressTolerance(event),
      };
      pressMovedPastTolerance = false;
    }

    function updateGuessPress(event) {
      if (!pressState) {
        return;
      }
      const point = eventClientPoint(event);
      if (
        point &&
        pointsMovedPastTolerance(
          pressState.point,
          point,
          pressState.tolerance
        )
      ) {
        pressMovedPastTolerance = true;
        lastDragAt = Date.now();
        hideGhostMarker();
      }
    }

    function endGuessPress(event) {
      updateGuessPress(event);
      window.setTimeout(resetPressState, 0);
    }

    function shouldIgnoreGuessClick() {
      const recentlyDragged =
        Date.now() - lastDragAt < GUESS_DRAG_SUPPRESSION_MS;
      return mapDragging || pressMovedPastTolerance || recentlyDragged;
    }

    function placeGuessPin(latlng) {
      const latitude = formatCoordinate(latlng.lat);
      const longitude = formatCoordinate(latlng.lng);
      const previousLatLng = selectedLatLng;
      const hadMarker = marker !== null;
      selectedLatLng = latlng;

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
    }

    if (window.PointerEvent) {
      mapContainer.addEventListener("pointerenter", updateGhostMarker);
      mapContainer.addEventListener("pointerleave", hideGhostMarker);
      mapContainer.addEventListener("pointerdown", beginGuessPress);
      mapContainer.addEventListener("pointermove", updateGuessPress);
      mapContainer.addEventListener("pointermove", updateGhostMarker);
      mapContainer.addEventListener("pointerup", endGuessPress);
      mapContainer.addEventListener("pointercancel", resetPressState);
    } else {
      mapContainer.addEventListener("mouseenter", updateGhostMarker);
      mapContainer.addEventListener("mouseleave", hideGhostMarker);
      mapContainer.addEventListener("mousedown", beginGuessPress);
      mapContainer.addEventListener("mousemove", updateGuessPress);
      mapContainer.addEventListener("mousemove", updateGhostMarker);
      mapContainer.addEventListener("mouseup", endGuessPress);
      mapContainer.addEventListener("touchstart", beginGuessPress, {
        passive: true,
      });
      mapContainer.addEventListener("touchstart", hideGhostMarker, {
        passive: true,
      });
      mapContainer.addEventListener("touchmove", updateGuessPress, {
        passive: true,
      });
      mapContainer.addEventListener("touchmove", hideGhostMarker, {
        passive: true,
      });
      mapContainer.addEventListener("touchend", endGuessPress);
      mapContainer.addEventListener("touchcancel", resetPressState);
    }

    map.on("dragstart", function () {
      mapDragging = true;
      lastDragAt = Date.now();
      mapContainer.classList.add("game-map--dragging");
      hideGhostMarker();
    });

    map.on("dragend", function () {
      mapDragging = false;
      lastDragAt = Date.now();
      mapContainer.classList.remove("game-map--dragging");
      resetPressState();
      hideGhostMarker();
    });

    map.on("click", function (event) {
      if (shouldIgnoreGuessClick()) {
        resetPressState();
        hideGhostMarker();
        return;
      }
      placeGuessPin(event.latlng);
      resetPressState();
      hideGhostMarker();
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

  function prefersReducedMotion() {
    return Boolean(
      window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function trackRevealShown(map, revealState, mapElement) {
    const targetType =
      mapElement.dataset.targetBoundaryLayer === "villages"
        ? "village"
        : "municipality";
    const payload = {
      latitude: revealState.latlng.lat,
      longitude: revealState.latlng.lng,
      target_id: Number(revealState.targetId),
      target_type: targetType,
      zoom: map.getZoom(),
    };
    if (targetType === "municipality") {
      payload.target_municipality_id = Number(revealState.targetId);
    }
    sendTrackingEvent(mapElement, "REVEAL_SHOWN", payload);
  }

  function initializeReveal(map, boundaryState, revealState, mapElement) {
    map.getContainer().classList.add("game-map--reveal");
    trackRevealShown(map, revealState, mapElement);

    if (prefersReducedMotion()) {
      createRevealedGuessMarker(map, revealState.latlng);
      boundaryState.revealTargetVisible = true;
      applyBoundaryLineTheme(map, boundaryState, revealState, null);
      drawRevealDistanceLine(map, boundaryState.municipalityLayer, revealState);
      return;
    }

    window.setTimeout(function () {
      createRevealedGuessMarker(map, revealState.latlng);
    }, REVEAL_PIN_DELAY_MS);
    window.setTimeout(function () {
      boundaryState.revealTargetVisible = true;
      applyBoundaryLineTheme(map, boundaryState, revealState, null);
    }, REVEAL_TARGET_DELAY_MS);
    window.setTimeout(function () {
      drawRevealDistanceLine(map, boundaryState.municipalityLayer, revealState);
    }, REVEAL_LINE_DELAY_MS);
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
    isVillageLayer,
    revealTargetVisible
  ) {
    return function (feature) {
      const isRevealTarget =
        revealState && isTargetFeature(feature, revealState.targetId);
      if (
        (isRevealTarget && revealTargetVisible !== false) ||
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

  function fitScopeBounds(map, scopeBounds) {
    if (scopeBounds && scopeBounds.isValid()) {
      map.fitBounds(scopeBounds, mapFitOptions(map, 10, 24));
    }
  }

  function refitMapView(
    map,
    municipalityLayer,
    revealState,
    summaryState,
    scopeBounds
  ) {
    if (municipalityLayer === null) {
      if (!revealState && !summaryState) {
        fitScopeBounds(map, scopeBounds);
      }
      return;
    }
    if (revealState) {
      fitRevealBounds(map, municipalityLayer, revealState);
    } else if (summaryState) {
      fitSummaryBounds(map, municipalityLayer, summaryState);
    } else if (scopeBounds) {
      fitScopeBounds(map, scopeBounds);
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
    const scopeBounds = readMapScopeBounds(mapElement);
    const labelMinZoom = readNumber(mapElement, "labelMinZoom", 11);
    const revealState = readRevealState(mapElement);
    const summaryState = readSummaryState();
    const backgroundMapId = readStoredBackgroundMapId();
    const boundaryLineMode = readStoredBoundaryLineMode();
    const vectorRenderer = window.L.canvas({
      padding: VECTOR_RENDERER_PADDING,
    });
    const map = window.L.map(mapElement, {
      attributionControl: true,
      clickTolerance: GUESS_CLICK_TOLERANCE,
      maxBounds: switzerlandBounds,
      maxBoundsViscosity: 1,
      minZoom: initialBackgroundMinZoom(backgroundMapId),
      preferCanvas: true,
      renderer: vectorRenderer,
      tapTolerance: GUESS_TAP_TOLERANCE,
      worldCopyJump: false,
      zoomControl: false,
    });

    map.setView([latitude, longitude], zoom);
    constrainMapToBounds(map, switzerlandBounds, backgroundMapId);
    if (scopeBounds) {
      map.invalidateSize();
      fitScopeBounds(map, scopeBounds);
    }
    let resizeFitTimeout = null;
    let baseLayerState = null;
    let boundaryState = null;
    function refreshMapFit() {
      map.invalidateSize();
      constrainMapToBounds(
        map,
        switzerlandBounds,
        baseLayerState ? baseLayerState.mapId : backgroundMapId
      );
      refitMapView(
        map,
        boundaryState ? boundaryState.municipalityLayer : null,
        revealState,
        summaryState,
        scopeBounds
      );
    }
    map.on("resize", function () {
      window.clearTimeout(resizeFitTimeout);
      resizeFitTimeout = window.setTimeout(refreshMapFit, 0);
    });
    const hasVillageLayer =
      mapElement.dataset.targetBoundaryLayer === "villages";
    const hasMunicipalityLayer =
      mapElement.dataset.targetBoundaryLayer === "municipalities" ||
      Boolean(mapElement.dataset.municipalityOverlayUrl);
    const outlineLayers = readStoredOutlineLayers(
      hasMunicipalityLayer,
      hasVillageLayer
    );
    baseLayerState = {
      layer: addBaseMapLayer(
        map,
        backgroundMapId,
        mapElement.dataset.baseMapUrl
      ),
      mapId: backgroundMapId,
    };
    boundaryState = {
      cantonLayer: null,
      cantonLayerManager: null,
      hasMunicipalityLayer: hasMunicipalityLayer,
      hasVillageLayer: hasVillageLayer,
      lineMode: boundaryLineMode,
      mapId: backgroundMapId,
      municipalityLayer: null,
      municipalityOverlayLayer: null,
      municipalityOverlayLayerManager: null,
      outlineLayers: outlineLayers,
      revealTargetVisible: !revealState,
      targetLayerManager: null,
    };

    const targetLayerId = hasVillageLayer ? "villages" : "municipalities";
    const requiresTargetLayer = Boolean(revealState || summaryState);
    boundaryState.targetLayerManager = createBoundaryLayerManager(map, {
      errorMessage: "Target boundaries could not be loaded.",
      layerId: targetLayerId,
      renderer: vectorRenderer,
      required: function () {
        return requiresTargetLayer;
      },
      style: function () {
        return municipalityStyle(
          revealState,
          summaryState,
          currentBoundaryLineColors(boundaryState),
          boundaryState.outlineLayers,
          boundaryState.hasVillageLayer,
          boundaryState.revealTargetVisible
        );
      },
      url: mapElement.dataset.targetBoundariesUrl,
      visible: function () {
        return isBoundaryLayerVisible(boundaryState, targetLayerId);
      },
    });
    boundaryState.municipalityOverlayLayerManager = createBoundaryLayerManager(
      map,
      {
        errorMessage: "Municipality overlay could not be loaded.",
        layerId: "municipalities",
        renderer: vectorRenderer,
        required: function () {
          return false;
        },
        style: function () {
          return municipalityOverlayStyle(
            currentBoundaryLineColors(boundaryState),
            boundaryState.outlineLayers
          );
        },
        suppressGlobalError: true,
        url: mapElement.dataset.municipalityOverlayUrl,
        visible: function () {
          return isBoundaryLayerVisible(boundaryState, "municipalities");
        },
      }
    );
    boundaryState.cantonLayerManager = createBoundaryLayerManager(map, {
      errorMessage: "Canton boundaries could not be loaded.",
      layerId: "cantons",
      renderer: vectorRenderer,
      required: function () {
        return false;
      },
      style: function () {
        return cantonStyle(
          currentBoundaryLineColors(boundaryState),
          boundaryState.outlineLayers
        );
      },
      url: mapElement.dataset.cantonBoundariesUrl,
      visible: function () {
        return isBoundaryLayerVisible(boundaryState, "cantons");
      },
    });

    function syncBoundaryLayers(syncOptions) {
      const options = syncOptions || {};
      return Promise.all([
        boundaryState.targetLayerManager.sync({
          fitBounds: Boolean(options.fitTarget),
        }).then(function (layer) {
          boundaryState.municipalityLayer = layer;
          return layer;
        }),
        boundaryState.municipalityOverlayLayerManager.sync().then(
          function (layer) {
            boundaryState.municipalityOverlayLayer = layer;
            return layer;
          }
        ),
        boundaryState.cantonLayerManager.sync().then(function (layer) {
          boundaryState.cantonLayer = layer;
          return layer;
        }),
      ]);
    }

    function syncBoundaryDetailForZoom() {
      syncBoundaryLayers();
    }

    map.on("zoomend", syncBoundaryDetailForZoom);
    const syncZoomControls = initializeMapZoomControls(map);
    function runStableMapFit() {
      refreshMapFit();
      syncZoomControls();
    }
    function scheduleStableMapFit() {
      const runOnFrame = function () {
        if (window.requestAnimationFrame) {
          window.requestAnimationFrame(runStableMapFit);
        } else {
          window.setTimeout(runStableMapFit, 0);
        }
      };
      map.whenReady(runOnFrame);
      if (baseLayerState && baseLayerState.layer) {
        baseLayerState.layer.once("load", runStableMapFit);
      }
      window.addEventListener("load", runStableMapFit, { once: true });
      [80, 220, 420, 900, 1500].forEach(function (delay) {
        window.setTimeout(runStableMapFit, delay);
      });
    }
    applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
    initializeBackgroundMapPicker(
      map,
      baseLayerState,
      boundaryState,
      revealState,
      summaryState,
      switzerlandBounds,
      scopeBounds,
      mapElement.dataset.baseMapUrl
    );
    initializeBoundaryLinePicker(map, boundaryState, revealState, summaryState);
    initializeOutlineLayerPickers(
      map,
      boundaryState,
      revealState,
      summaryState,
      syncBoundaryLayers
    );
    initializeMapSettingsMenu();
    window.L.control.scale({ imperial: false, metric: true }).addTo(map);
    if (summaryState) {
      initializeSummary(map, summaryState);
    } else if (!revealState) {
      initializeGuessInteraction(map, mapElement);
    }
    mapElement.dataset.initialized = "true";

    syncBoundaryLayers({
      fitTarget: !revealState && !summaryState && !scopeBounds,
    }).then(function () {
      const targetLayer = boundaryState.municipalityLayer;
      if (revealState) {
        if (targetLayer !== null) {
          fitRevealBounds(map, targetLayer, revealState);
        }
        initializeReveal(map, boundaryState, revealState, mapElement);
        initializeNextTurnTracking(mapElement);
        initializeLabelLayer(
          map,
          mapElement.dataset.municipalityLabelsUrl,
          labelMinZoom
        );
      } else if (summaryState && targetLayer !== null) {
        fitSummaryBounds(map, targetLayer, summaryState);
        summaryState.reveals.forEach(function (reveal) {
          drawRevealDistanceLine(map, targetLayer, reveal);
        });
      } else {
        refitMapView(map, targetLayer, revealState, summaryState, scopeBounds);
      }
      applyBoundaryLineTheme(map, boundaryState, revealState, summaryState);
      syncZoomControls();
      return null;
    });

    scheduleStableMapFit();
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

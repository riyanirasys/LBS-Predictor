const cleanMapName = "{map_name}";
let cleanMap = null;
const cleanPayload = {json.dumps(payload, ensure_ascii=False)};
const cleanState = {
        district: "",
        psKey: "",
        showDistricts: true,
        showPs: true,
        showPsMarkers: true,
        showFrv: true,
      };
const cleanLayers = {
        districts: [],
        psBoundaries: [],
        psMarkers: [],
        frvMarkers: [],
      };
let cleanMapInitialized = false;

function resolveCleanMap() {
    if (cleanMap) return cleanMap;
    if (window[cleanMapName] && typeof window[cleanMapName].fitBounds === "function") {{
        cleanMap = window[cleanMapName];
        return cleanMap;
    }
    for (const key in window) {{
          const candidate = window[key];
          if (
            candidate &&
            typeof candidate.fitBounds === "function" &&
            typeof candidate.setView === "function" &&
            typeof candidate.addLayer === "function"
          ) {{
            cleanMap = candidate;
            return cleanMap;
          }}
        }}
        return null;
      }}

      function cleanEscape(value) {{
        return String(value == null ? "" : value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;");
      }}

      function districtStyle(feature) {
        const name = feature.properties.map_district;
        const active = cleanState.district && cleanState.district === name;
        return {
          color: active ? "#1f6feb" : "#334155",
          weight: active ? 3 : 1.2,
          opacity: active ? 1 : .42,
          fillColor: "#60a5fa",
          fillOpacity: active ? .12 : .025,
        };
      }

      function psStyle(feature) {
        const key = feature.properties.map_ps_key;
        const activeDistrict = cleanState.district && feature.properties.map_district === cleanState.district;
        const selected = cleanState.psKey && key === cleanState.psKey;
        return {
          color: selected ? "#e11d48" : activeDistrict ? "#f97316" : "#94a3b8",
          weight: selected ? 4 : activeDistrict ? 2.1 : 1,
          opacity: selected || activeDistrict ? 1 : .32,
          fillColor: selected ? "#fb7185" : "#fdba74",
          fillOpacity: selected ? .20 : activeDistrict ? .10 : .02,
        };
      }

      function addGeoJsonLayers() {
        L.geoJSON(cleanPayload.districtGeojson, {
          style: districtStyle,
          onEachFeature: function(feature, layer) {{
            layer.cleanKind = "district";
            layer.cleanDistrict = feature.properties.map_district;
            layer.bindTooltip(feature.properties.map_district);
            layer.bindPopup(
              "<b>District:</b> " + cleanEscape(feature.properties.map_district) +
              "<br><b>Incidents:</b> " + cleanEscape(feature.properties.incidents) +
              "<br><b>FRVs:</b> " + cleanEscape(feature.properties.frvs) +
              "<br><b>Avg response:</b> " + cleanEscape(feature.properties.avg_resp)
            );
            cleanLayers.districts.push(layer);
          }}
        }).addTo(cleanMap);

        L.geoJSON(cleanPayload.psGeojson, {
          style: psStyle,
          onEachFeature: function(feature, layer) {{
            layer.cleanKind = "ps";
            layer.cleanDistrict = feature.properties.map_district;
            layer.cleanPsKey = feature.properties.map_ps_key;
            layer.bindTooltip(feature.properties.map_ps);
            layer.bindPopup(
              "<b>Police Station:</b> " + cleanEscape(feature.properties.map_ps) +
              "<br><b>District:</b> " + cleanEscape(feature.properties.map_district)
            );
            cleanLayers.psBoundaries.push(layer);
          }}
        }).addTo(cleanMap);
      }

      function makePsIcon() {
          return L.icon({
              iconUrl: 'assets/house.svg',
              iconSize: [28, 28],
              iconAnchor: [14, 14]
          });
      }

      function makeFrvIcon(point) {{

          const isGood = Number(point.avgResponse || 0) <= 10;

        //   # return L.icon({{
        //   #     iconUrl: isGood
        //   #         ? 'assets/car_green.svg'
        //   #         : 'assets/car_red.svg',

        //   #     iconSize: [32, 32],
        //   #     iconAnchor: [16, 16]
        //   # }});

        return L.divIcon({
            html: `
                <svg>
                    <use href="assets/car.svg" fill="${'#16a34a' if isGood else '#dc2626'}" />
                </svg>
            `,
            className: '',
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        });
      }

      function addPointLayers() {{
        cleanPayload.psPoints.forEach(function(point) {
          const marker = L.marker([point.lat, point.lon], { icon: makePsIcon(), zIndexOffset: 500 });
          marker.cleanDistrict = point.district;
          marker.cleanPsKey = point.psKey;
          marker.bindTooltip("PS: " + point.ps);
          marker.bindPopup(point.popup);
          marker.addTo(cleanMap);
          cleanLayers.psMarkers.push(marker);
        });

        cleanPayload.frvPoints.forEach(function(point) {
          const marker = L.marker([point.lat, point.lon], { icon: makeFrvIcon(point), zIndexOffset: 1000 });
          marker.cleanDistrict = point.district;
          marker.cleanPsKey = point.psKey;
          marker.bindTooltip("FRV " + point.frvId);
          marker.bindPopup(point.popup);
          marker.addTo(cleanMap);
          cleanLayers.frvMarkers.push(marker);
        });
      }

      function layerAllowed(layer, kind) {{
        if (kind === "district") {{
          return cleanState.showDistricts && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "ps") {{
          return cleanState.showPs && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "psMarker") {{
          return cleanState.showPsMarkers && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "frv") {{
          return cleanState.showFrv &&
            (!cleanState.district || layer.cleanDistrict === cleanState.district) &&
            (!cleanState.psKey || layer.cleanPsKey === cleanState.psKey);
        }}
        return true;
      }}

      function setLayerPresence(layer, shouldShow) {{
        const visible = cleanMap.hasLayer(layer);
        if (shouldShow && !visible) cleanMap.addLayer(layer);
        if (!shouldShow && visible) cleanMap.removeLayer(layer);
      }}

      function applyCleanFilters() {{
        cleanLayers.districts.forEach(function(layer) {{
          if (layer.setStyle) layer.setStyle(districtStyle(layer.feature));
          setLayerPresence(layer, layerAllowed(layer, "district"));
        }});
        cleanLayers.psBoundaries.forEach(function(layer) {{
          if (layer.setStyle) layer.setStyle(psStyle(layer.feature));
          setLayerPresence(layer, layerAllowed(layer, "ps"));
        }});
        cleanLayers.psMarkers.forEach(function(layer) {{
          setLayerPresence(layer, layerAllowed(layer, "psMarker"));
        }});
        cleanLayers.frvMarkers.forEach(function(layer) {{
          setLayerPresence(layer, layerAllowed(layer, "frv"));
        }});
      }}

      function populateDistricts() {{
        const districtSelect = document.getElementById("district-select-clean");
        districtSelect.innerHTML = '<option value="">View all MP</option>';
        Object.keys(cleanPayload.districtPsMap).sort().forEach(function(district) {{
          const option = document.createElement("option");
          option.value = district;
          option.textContent = district;
          districtSelect.appendChild(option);
        }});
      }}

      function populatePoliceStations(district) {{
        const psSelect = document.getElementById("ps-select-clean");
        psSelect.innerHTML = "";
        if (!district) {{
          psSelect.disabled = true;
          psSelect.innerHTML = '<option value="">Select district first</option>';
          return;
        }}
        psSelect.disabled = false;
        const allOption = document.createElement("option");
        allOption.value = "";
        allOption.textContent = "All police stations in " + district;
        psSelect.appendChild(allOption);
        (cleanPayload.districtPsMap[district] || []).forEach(function(item) {{
          const option = document.createElement("option");
          option.value = item.value;
          option.textContent = item.label;
          psSelect.appendChild(option);
        }});
      }}

      function fitCleanSelection() {{
        if (cleanState.psKey && cleanPayload.psBounds[cleanState.psKey]) {{
          cleanMap.fitBounds(cleanPayload.psBounds[cleanState.psKey], { padding: [24, 24] });
          return;
        }}
        if (cleanState.district && cleanPayload.districtBounds[cleanState.district]) {{
          cleanMap.fitBounds(cleanPayload.districtBounds[cleanState.district], { padding: [24, 24] });
          return;
        }}
        if (cleanPayload.allBounds) {{
          cleanMap.fitBounds(cleanPayload.allBounds, { padding: [18, 18] });
        }} else {{
          cleanMap.setView(cleanPayload.defaultCenter, cleanPayload.defaultZoom);
        }}
      }}

      function updateSelectionNote() {{
        const note = document.getElementById("selection-note");
        const frvCount = cleanLayers.frvMarkers.filter(function(layer) {{
          return cleanMap.hasLayer(layer);
        }}).length;
        const psCount = cleanLayers.psMarkers.filter(function(layer) {{
          return cleanMap.hasLayer(layer);
        }}).length;
        if (cleanState.psKey) {{
          const label = document.getElementById("ps-select-clean").selectedOptions[0].textContent;
          note.textContent = "Showing " + frvCount + " FRV marker(s) for " + label + ".";
        }} else if (cleanState.district) {{
          note.textContent = "Showing " + psCount + " police station marker(s) and " + frvCount + " FRV marker(s) only for " + cleanState.district + ".";
        }} else {{
          note.textContent = "Select a district to show only that district's police stations and FRVs.";
        }}
      }}

      function openPanel(panelId, buttonId) {{
        const panel = document.getElementById(panelId);
        const shouldOpen = !panel.classList.contains("open");
        ["area-panel", "filter-panel-clean", "legend-panel"].forEach(function(id) {{
          document.getElementById(id).classList.toggle("open", id === panelId && shouldOpen);
        }});
        ["area-btn", "filter-btn", "legend-btn"].forEach(function(id) {{
          document.getElementById(id).classList.remove("active");
        }});
        document.getElementById(buttonId).classList.toggle("active", shouldOpen);
      }}

      function wireControls() {{
        const ui = document.getElementById("clean-map-ui");
        if (L.DomEvent) {{
          L.DomEvent.disableClickPropagation(ui);
          L.DomEvent.disableScrollPropagation(ui);
        }}

        document.getElementById("area-btn").onclick = function() {{ openPanel("area-panel", "area-btn"); }};
        document.getElementById("filter-btn").onclick = function() {{ openPanel("filter-panel-clean", "filter-btn"); }};
        document.getElementById("legend-btn").onclick = function() {{ openPanel("legend-panel", "legend-btn"); }};

        document.getElementById("district-select-clean").onchange = function(event) {{
          cleanState.district = event.target.value;
          cleanState.psKey = "";
          populatePoliceStations(cleanState.district);
          applyCleanFilters();
          fitCleanSelection();
          updateSelectionNote();
        }};

        document.getElementById("ps-select-clean").onchange = function(event) {{
          cleanState.psKey = event.target.value;
          applyCleanFilters();
          fitCleanSelection();
          updateSelectionNote();
        }};

        [
          ["toggle-districts", "showDistricts"],
          ["toggle-ps", "showPs"],
          ["toggle-ps-markers", "showPsMarkers"],
          ["toggle-frv", "showFrv"],
        ].forEach(function(pair) {{
          document.getElementById(pair[0]).onchange = function(event) {{
            cleanState[pair[1]] = event.target.checked;
            applyCleanFilters();
          }};
        }});
      }}

      function initCleanMap() {{
        if (cleanMapInitialized) return;
        if (!window.L || !resolveCleanMap()) {{
          window.setTimeout(initCleanMap, 80);
          return;
        }}
        cleanMapInitialized = true;
        addGeoJsonLayers();
        addPointLayers();
        populateDistricts();
        populatePoliceStations("");
        wireControls();
        applyCleanFilters();
      }}

      if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", initCleanMap);
      }} else {{
        initCleanMap();
      }}
      window.addEventListener("load", initCleanMap);
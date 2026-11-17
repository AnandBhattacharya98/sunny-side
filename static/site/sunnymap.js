/* Sunny Side's illustrated base map for Leaflet (see sunnymap.css).
   SunnyMap.paint(map, places) draws neighbouring countries, India with dashed state lines,
   city and area names, and soft halos around job areas. No tile server, no API key.
   Shapes: Natural Earth (public domain), via datamaps (MIT) and world-atlas (ISC). */
(function () {
  var MAJOR = { 'Bengaluru': 1, 'Mumbai': 1, 'New Delhi': 1, 'Hyderabad': 1, 'Chennai': 1, 'Kolkata': 1, 'Pune': 1 };
  var BIG_STATES = ['Rajasthan', 'Madhya Pradesh', 'Maharashtra', 'Uttar Pradesh', 'Gujarat', 'Karnataka', 'Andhra Pradesh',
                    'Telangana', 'Odisha', 'Chhattisgarh', 'Tamil Nadu', 'Bihar', 'West Bengal', 'Jharkhand', 'Kerala'];
  var RIVERS = { 'Ganges': 'Ganga', 'Yamuna': 'Yamuna', 'Godavari': 'Godavari', 'God\u00e4vari': 'Godavari', 'Krishna': 'Krishna', 'Narmada': 'Narmada',
                 'Brahmaputra': 'Brahmaputra', 'Cauvery': 'Kaveri', 'Mah\u00e4na Nad\u00ef': 'Mahanadi', 'Indus': 'Indus', 'Chambal': 'Chambal', 'Sutlej': 'Sutlej' };
  var cache = null, detail = null;
  function load() {
    if (!cache) cache = fetch('/static/vendor/geo/india-map.json?v=1').then(function (r) { return r.json(); });
    return cache;
  }
  function loadDetail() {
    if (!detail) detail = fetch('/static/vendor/geo/india-detail.json?v=1').then(function (r) { return r.json(); });
    return detail;
  }
  function flip(line) { return line.map(function (p) { return [p[1], p[0]]; }); }
  function paintDetail(map, d) {
    var lines = function (arr) { return arr.map(flip); };
    L.polyline(lines(d.rd), { pane: 'smDetail', className: 'sm-rd', interactive: false, smoothFactor: 1.5 }).addTo(map);
    var major = d.rv.filter(function (r) { return r.r <= 6; }), minor = d.rv.filter(function (r) { return r.r > 6; });
    L.polyline(minor.map(function (r) { return flip(r.c); }), { pane: 'smDetail', className: 'sm-river minor', interactive: false }).addTo(map);
    L.polyline(major.map(function (r) { return flip(r.c); }), { pane: 'smDetail', className: 'sm-river', interactive: false }).addTo(map);
    L.polyline(lines(d.hw), { pane: 'smDetail', className: 'sm-hw-edge', interactive: false, smoothFactor: 1.5 }).addTo(map);
    L.polyline(lines(d.hw), { pane: 'smDetail', className: 'sm-hw', interactive: false, smoothFactor: 1.5 }).addTo(map);
    // One label per big river, at the middle of its longest stretch
    var best = {};
    d.rv.forEach(function (r) {
      var n = RIVERS[r.n]; if (!n) return;
      if (!best[n] || r.c.length > best[n].length) best[n] = r.c;
    });
    Object.keys(best).forEach(function (n) {
      var c = best[n], m = c[Math.floor(c.length / 2)];
      label(map, m[1], m[0], n, 'river');
    });
  }
  function label(map, lat, lng, text, cls) {
    return L.marker([lat, lng], { interactive: false, keyboard: false, pane: 'smLabels',
      icon: L.divIcon({ className: 'sm-lbl ' + cls, html: '<div>' + (cls === 'state' || cls === 'river' ? '' : '<i></i>') + '<span>' + text + '</span></div>', iconSize: null }) }).addTo(map);
  }
  function zoomClass(map) {
    var z = map.getZoom(), el = map.getContainer();
    el.setAttribute('data-z', z >= 11 ? 'near' : z >= 7 ? 'mid' : 'far');
  }
  window.SunnyMap = {
    paint: function (map, places) {
      var el = map.getContainer();
      el.classList.add('sunnymap');
      map.createPane('smBase').style.zIndex = 150;
      map.createPane('smDetail').style.zIndex = 155;
      map.getPane('smDetail').style.pointerEvents = 'none';
      map.createPane('smHalo').style.zIndex = 160;
      map.getPane('smHalo').style.pointerEvents = 'none';
      map.createPane('smLabels').style.zIndex = 450;
      map.getPane('smLabels').style.pointerEvents = 'none';
      map.attributionControl.addAttribution('Map shapes: Natural Earth');
      zoomClass(map);
      map.on('zoomend', function () { zoomClass(map); });
      (places || []).forEach(function (p) {
        if (p.kind === 'area') {
          L.circle([p.lat, p.lng], { radius: 2600, pane: 'smHalo', className: 'sm-halo', interactive: false }).addTo(map);
          label(map, p.lat, p.lng, p.name, 'area');
        } else {
          label(map, p.lat, p.lng, p.name, MAJOR[p.name] ? 'major' : 'minor');
        }
      });
      return load().then(function (g) {
        L.geoJSON(g.neighbors, { pane: 'smBase', interactive: false, style: function () { return { className: 'sm-other' }; } }).addTo(map);
        L.geoJSON(g.india, { pane: 'smBase', interactive: false, style: function () { return { className: 'sm-land' }; } }).addTo(map);
        var states = L.geoJSON(g.states, { pane: 'smBase', interactive: false, style: function () { return { className: 'sm-state' }; } }).addTo(map);
        states.eachLayer(function (l) {
          var n = l.feature.properties.n;
          if (BIG_STATES.indexOf(n) >= 0) { var c = l.getBounds().getCenter(); label(map, c.lat, c.lng, n, 'state'); }
        });
        return loadDetail().then(function (d) { paintDetail(map, d); });
      }).catch(function () {});
    }
  };
})();

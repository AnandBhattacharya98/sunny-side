/* Sunny Side's illustrated base map for Leaflet (see sunnymap.css).
   SunnyMap.paint(map, places) draws neighbouring countries, India with dashed state lines,
   city and area names, and soft halos around job areas. No tile server, no API key.
   Shapes: Natural Earth (public domain), via datamaps (MIT) and world-atlas (ISC). */
(function () {
  var MAJOR = { 'Bengaluru': 1, 'Mumbai': 1, 'New Delhi': 1, 'Hyderabad': 1, 'Chennai': 1, 'Kolkata': 1, 'Pune': 1 };
  var BIG_STATES = ['Rajasthan', 'Madhya Pradesh', 'Maharashtra', 'Uttar Pradesh', 'Gujarat', 'Karnataka', 'Andhra Pradesh',
                    'Telangana', 'Odisha', 'Chhattisgarh', 'Tamil Nadu', 'Bihar', 'West Bengal', 'Jharkhand', 'Kerala'];
  var cache = null;
  function load() {
    if (!cache) cache = fetch('/static/vendor/geo/india-map.json?v=1').then(function (r) { return r.json(); });
    return cache;
  }
  function label(map, lat, lng, text, cls) {
    return L.marker([lat, lng], { interactive: false, keyboard: false, pane: 'smLabels',
      icon: L.divIcon({ className: 'sm-lbl ' + cls, html: '<div>' + (cls === 'state' ? '' : '<i></i>') + '<span>' + text + '</span></div>', iconSize: null }) }).addTo(map);
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
      }).catch(function () {});
    }
  };
})();

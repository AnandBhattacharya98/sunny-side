# Vendored front-end libraries

Served from our own server so pages never depend on a CDN at run time.

| File | Contents | License |
| --- | --- | --- |
| `sunny-anim.min.js` | [Anime.js](https://github.com/juliangarnier/anime) 4.5.0, [AutoAnimate](https://github.com/formkit/auto-animate) 0.10.2 and [canvas-confetti](https://github.com/catdad/canvas-confetti) 1.9.4, bundled as `window.SunnyAnim` | MIT, MIT, ISC |
| `leaflet/` | [Leaflet](https://github.com/Leaflet/Leaflet) 1.9.4 (`leaflet.js`, `leaflet.css`, unmodified apart from the source-map comment). Loaded by the homepage's "jobs near you" map only when the map scrolls into view | BSD-2-Clause |
| `geo/india-map.json` | India state outlines and neighbouring countries, simplified for the illustrated map (`static/site/sunnymap.js`). Built from [datamaps](https://github.com/markmarkoh/datamaps) `ind.topo.json` and [world-atlas](https://github.com/topojson/world-atlas) `countries-110m`, both drawn from Natural Earth (public domain) | MIT / ISC |
| `lenis.min.js` | [Lenis](https://github.com/darkroomengineering/lenis) 1.3.27 smooth scrolling. Not loaded by any page since Nov 2026: it made scrolling feel sluggish | MIT |

Rebuild: `cd tools/anim-bundle && npm install && npm run build`.
The app's own motion rules live in `static/motion/` (all of it is switched off by the device's reduce-motion setting).

# Vendored front-end libraries

Served from our own server so pages never depend on a CDN at run time.

| File | Contents | License |
| --- | --- | --- |
| `sunny-anim.min.js` | [Anime.js](https://github.com/juliangarnier/anime) 4.5.0, [AutoAnimate](https://github.com/formkit/auto-animate) 0.10.2 and [canvas-confetti](https://github.com/catdad/canvas-confetti) 1.9.4, bundled as `window.SunnyAnim` | MIT, MIT, ISC |
| `lenis.min.js` | [Lenis](https://github.com/darkroomengineering/lenis) 1.3.27 smooth scrolling (homepage only, mouse and trackpad only) | MIT |

Rebuild: `cd tools/anim-bundle && npm install && npm run build`.
The app's own motion rules live in `static/motion/` (all of it is switched off by the device's reduce-motion setting).

// The animation libraries the site uses, bundled into one file so pages load a single
// script from our own server (no CDN at run time, works in the installed app too).
//   Anime.js v4      MIT  https://github.com/juliangarnier/anime
//   AutoAnimate      MIT  https://github.com/formkit/auto-animate
//   canvas-confetti  ISC  https://github.com/catdad/canvas-confetti
// Rebuild with: cd tools/anim-bundle && npm install && npm run build
import { animate, stagger, createTimeline, splitText, onScroll, svg, utils, eases, spring } from 'animejs';
import autoAnimate from '@formkit/auto-animate';
import confetti from 'canvas-confetti';

window.SunnyAnim = { animate, stagger, createTimeline, splitText, onScroll, svg, utils, eases, spring, autoAnimate, confetti };

// Material-style click ripple for the site — a circle that expands from the
// cursor on buttons, nav links and the "next steps" cards. Matches the app's
// ripple (scriber/frontend/src/ripple.js). Honors prefers-reduced-motion.
(function () {
  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
  var SELECTOR = ".btn, .nav a, .next-links a";

  function spawn(el, clientX, clientY) {
    var rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    var x = clientX - rect.left;
    var y = clientY - rect.top;
    var d = 2 * Math.hypot(Math.max(x, rect.width - x), Math.max(y, rect.height - y));
    var ink = document.createElement("span");
    ink.className = "ripple-ink";
    ink.style.width = ink.style.height = d + "px";
    ink.style.left = x - d / 2 + "px";
    ink.style.top = y - d / 2 + "px";
    ink.addEventListener("animationend", function () { ink.remove(); }, { once: true });
    el.appendChild(ink);
  }

  document.addEventListener("pointerdown", function (e) {
    if (typeof e.button === "number" && e.button !== 0) return;
    if (REDUCED.matches) return;
    var t = e.target;
    if (!(t instanceof Element)) return;
    var el = t.closest(SELECTOR);
    if (el) spawn(el, e.clientX, e.clientY);
  }, { passive: true });
})();

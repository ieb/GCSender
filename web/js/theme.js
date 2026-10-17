// Applied before first paint (classic script in <head>): theme and touch size
// from the display preferences, so the page never flashes the wrong theme.
(function () {
  var p = {};
  try { p = JSON.parse(localStorage.getItem("gcs-display") || "{}") || {}; } catch (e) { p = {}; }
  var root = document.documentElement;
  if (p.theme === "light" || p.theme === "dark") root.dataset.theme = p.theme;
  var touch = p.largeTouch === "on" || (p.largeTouch !== "off" && window.matchMedia("(pointer: coarse)").matches);
  if (touch) root.classList.add("touch");
})();

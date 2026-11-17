/* Pure filter logic for the neighborhood map (no DOM, so it can be unit-tested).
   A home shows the cases that match the chosen stages and age; its color is worked out from only those cases. */
(function (root) {
  var SERIOUS = ["board", "committee", "imposed", "legal"];   // red
  var ACTIVE = ["cure", "hold"];                              // orange; "resolved" alone is green

  /* "Issued in the last N days" counts whole days: a case issued N days ago (any time that day) is still in. */
  function cutoffMs(days, nowMs) {
    if (!days) return -Infinity;
    var d = new Date(nowMs);
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - days);
    return d.getTime();
  }

  function matches(c, f) {
    return f.stages.indexOf(c.stage) >= 0 && new Date(c.date + "T00:00:00").getTime() >= cutoffMs(f.days, f.nowMs);
  }

  function colorOf(cases) {
    if (!cases.length) return "gray";
    if (cases.some(function (c) { return SERIOUS.indexOf(c.stage) >= 0; })) return "red";
    if (cases.some(function (c) { return ACTIVE.indexOf(c.stage) >= 0; })) return "orange";
    return "green";
  }

  /* f = {stages: [keys], days: 0|30|60|90|180, nowMs: Date.now(), showEmpty: bool}
     -> {visible, cases (matching), hidden (other cases of this home), color} */
  function homeView(home, f) {
    var cases = home.cases.filter(function (c) { return matches(c, f); });
    var visible = cases.length > 0 || (f.showEmpty && home.cases.length === 0);
    return { visible: visible, cases: cases, hidden: home.cases.length - cases.length, color: colorOf(cases) };
  }

  /* how many cases fall in each stage once the age filter is applied (for the numbers beside the checkboxes) */
  function stageCounts(homes, days, nowMs) {
    var counts = {};
    homes.forEach(function (h) {
      h.cases.forEach(function (c) {
        if (new Date(c.date + "T00:00:00").getTime() >= cutoffMs(days, nowMs)) counts[c.stage] = (counts[c.stage] || 0) + 1;
      });
    });
    return counts;
  }

  var api = { homeView: homeView, stageCounts: stageCounts, colorOf: colorOf };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.MapFilter = api;
})(typeof window !== "undefined" ? window : this);

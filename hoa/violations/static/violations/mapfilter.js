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

  var ABBR = [[/\bBoulevard\b/gi, "Blvd"], [/\bDrive\b/gi, "Dr"], [/\bCourt\b/gi, "Ct"], [/\bStreet\b/gi, "St"], [/\bLane\b/gi, "Ln"],
              [/\bRoad\b/gi, "Rd"], [/\bAvenue\b/gi, "Ave"], [/\bCircle\b/gi, "Cir"], [/\bTerrace\b/gi, "Ter"]];

  /* Compact form for the crowded site plan: "6632 Dovekey Drive #13" -> "6632 Dovekey Dr" (the unit is the lot number shown in the box). */
  function shortAddress(a, lot) {
    var t = String(a || "");
    ABBR.forEach(function (r) { t = t.replace(r[0], r[1]); });
    if (lot) t = t.replace(new RegExp("\\s*#\\s*" + String(parseInt(lot, 10)) + "\\s*$"), "");
    return t.trim();
  }

  /* Text beside a home when the Address, Lot no. and/or Resident name toggles are on ("" when neither is, or the data is missing).
     o.short gives the compact address used on the site plan. */
  function labelText(home, o) {
    var parts = [];
    if (o.lot && home.lot) parts.push("Lot " + home.lot);
    if (o.address && home.address) parts.push(o.short ? shortAddress(home.address, home.lot) : home.address);
    if (o.name && home.owner) parts.push(home.owner);
    return parts.join(" · ");
  }

  var api = { homeView: homeView, stageCounts: stageCounts, colorOf: colorOf, labelText: labelText, shortAddress: shortAddress };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.MapFilter = api;
})(typeof window !== "undefined" ? window : this);

/* Photo chooser: pick photos (as many times as you like), see a thumbnail of each, and remove a wrong one with the x before sending. */
(function () {
  function attach(input, thumbs) {
    var picked = [];
    function sync() {
      var dt = new DataTransfer(); picked.forEach(function (f) { dt.items.add(f); }); input.files = dt.files;
      thumbs.innerHTML = "";
      picked.forEach(function (f, i) {
        var d = document.createElement("div"); d.style.cssText = "position:relative";
        var im = document.createElement("img"); im.src = URL.createObjectURL(f); im.alt = f.name;
        im.style.cssText = "height:96px;border-radius:8px;border:1px solid var(--line);display:block";
        var x = document.createElement("button"); x.type = "button"; x.innerHTML = "&times;"; x.title = "Remove this photo"; x.setAttribute("aria-label", "Remove " + f.name);
        x.style.cssText = "position:absolute;top:3px;right:3px;width:24px;height:24px;padding:0;border-radius:50%;line-height:1;font-size:17px;background:#111;color:#fff;border:2px solid #fff";
        x.addEventListener("click", function () { picked.splice(i, 1); sync(); });
        d.appendChild(im); d.appendChild(x); thumbs.appendChild(d);
      });
    }
    input.addEventListener("change", function () {
      var seen = {}; picked.forEach(function (f) { seen[f.name + f.size + f.lastModified] = 1; });
      [].forEach.call(input.files, function (f) { if (!seen[f.name + f.size + f.lastModified]) picked.push(f); });
      sync();
    });
  }
  window.attachPhotoPicker = attach;
})();

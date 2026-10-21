// Fills <pre data-src="/projects/out/…"> with a transcript the image build
// produced (see the captures and pyhw4 stages in Dockerfile.web).
//
// Fetched rather than pasted into the HTML because the pages are hand-written
// and the transcripts are not: pasting would freeze one build's output into a
// file that later builds no longer regenerate, which is exactly the
// "screenshot" this site is trying not to be. Dockerfile.web checks that every
// data-src names a file the image contains.

(function () {
  "use strict";

  var blocks = document.querySelectorAll("pre[data-src]");
  Array.prototype.forEach.call(blocks, function (pre) {
    var src = pre.getAttribute("data-src");
    fetch(src, { credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (text) {
        // textContent, never innerHTML: these are program outputs, and one of
        // them is a scrape of 2016 web pages.
        pre.textContent = text.replace(/\s+$/, "");
      })
      .catch(function (e) {
        pre.textContent = "(could not load " + src + ": " + e.message + ")";
      });
  });
})();

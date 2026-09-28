/* Dynamic Alerting Platform — custom scripts */

/*
 * Legacy heading anchors (#1375).
 *
 * Heading ids on this site used Python-Markdown's default ASCII-only slugify
 * until #1375 switched them to github.com's rule (CJK kept, one `-` per space).
 * Deep links written against the old ids — bookmarks, chat messages, other
 * sites — would now land at the top of the page. When the URL's #fragment
 * names no element, recompute every heading's OLD id and scroll to the match.
 *
 * The old rule, from markdown.extensions.toc.slugify + toc.unique:
 *   NFKD → drop non-ASCII → drop [^\w\s-] → trim → lower → [-\s]+ → "-",
 *   duplicates (and the empty id) get _1, _2, …
 */
(function () {
  function legacySlug(text) {
    return text
      .normalize("NFKD")
      .replace(/[^\x00-\x7F]/g, "")
      .replace(/[^\w\s-]/g, "")
      .trim()
      .toLowerCase()
      .replace(/[-\s]+/g, "-");
  }

  function legacyUnique(id, used) {
    while (used.has(id) || !id) {
      var m = /^(.*)_([0-9]+)$/.exec(id);
      id = m ? m[1] + "_" + (parseInt(m[2], 10) + 1) : id + "_1";
    }
    used.add(id);
    return id;
  }

  function followLegacyAnchor() {
    var raw = window.location.hash.slice(1);
    if (!raw) return;
    var wanted;
    try {
      wanted = decodeURIComponent(raw);
    } catch (e) {
      return;
    }
    if (document.getElementById(wanted)) return;

    var used = new Set();
    var headings = document.querySelectorAll(
      ".md-content h1, .md-content h2, .md-content h3, .md-content h4, .md-content h5, .md-content h6"
    );
    for (var i = 0; i < headings.length; i++) {
      var h = headings[i];
      var text = h.textContent.replace(/¶\s*$/, "");
      if (legacyUnique(legacySlug(text), used) === wanted && h.id) {
        history.replaceState(null, "", "#" + h.id);
        h.scrollIntoView();
        return;
      }
    }
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(followLegacyAnchor); // mkdocs-material instant navigation
  } else {
    document.addEventListener("DOMContentLoaded", followLegacyAnchor);
  }
  window.addEventListener("hashchange", followLegacyAnchor);
})();

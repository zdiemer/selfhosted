// Shared by the terminal pages (projects/cloysta.html, projects/myds.html, and
// the /console pages projects/cop3014.html and projects/cda3100.html).
//
// THE PROBLEM THIS SOLVES. /cloysta and /hashtable sit behind Authelia
// forwardAuth. For a visitor without a session, Traefik answers the iframe's
// request with a 302 to auth.zachd.duckdns.org — and Authelia sends
//
//     Content-Security-Policy: ... frame-ancestors 'none'
//     X-Frame-Options: DENY
//
// so the browser refuses to render the login page inside the frame. The result
// is a blank rectangle and no explanation: the page looks broken rather than
// locked. Measured against the live Authelia, not assumed.
//
// So: probe first, and only attach the iframe once we know it will render.
// The probe is a same-origin fetch with redirect: "manual", which turns
// Traefik's cross-origin redirect into an opaqueredirect response we can detect
// without ever following it. ttyd's /token endpoint is the cheapest thing to
// ask for and needs no session of its own.

(function () {
  "use strict";

  var mount = document.getElementById("terminal");
  if (!mount) return;

  var base = mount.getAttribute("data-base");   // "/cloysta", "/hashtable", "/console"
  var label = mount.getAttribute("data-label") || "terminal";
  // /console only: which program to open. ttyd --url-arg hands ?arg= to
  // console-session.sh as $1, which checks it against its own fixed list.
  var arg = mount.getAttribute("data-arg");
  var src;
  function setArg(a) {
    arg = a;
    src = base + "/" + (arg ? "?arg=" + encodeURIComponent(arg) : "");
  }
  setArg(arg);

  function attach() {
    var f = document.createElement("iframe");
    f.className = "frame frame-tall";
    f.title = label;
    f.src = src;
    mount.innerHTML = "";
    mount.appendChild(f);
  }

  function signInPrompt(detail) {
    mount.innerHTML =
      '<div class="gate">' +
      '<p><strong>This one needs a sign-in.</strong></p>' +
      '<p>Authelia sits in front of ' + label + ', because it is a live process ' +
      'on the cluster rather than a recording. Open it once in its own tab to ' +
      'sign in, then come back here and reload.</p>' +
      '<p><a class="btn" href="' + src + '" target="_blank" rel="noopener">' +
      'Sign in and open ' + label + '</a> ' +
      '<button class="btn btn-quiet" type="button" id="term-retry">Reload here</button></p>' +
      (detail ? '<p class="status">' + detail + '</p>' : '') +
      '</div>';
    var r = document.getElementById("term-retry");
    if (r) r.addEventListener("click", function () { probe(); });
  }

  function probe() {
    mount.innerHTML = '<p class="status">Checking…</p>';
    fetch(base + "/token", { redirect: "manual", credentials: "same-origin" })
      .then(function (res) {
        // opaqueredirect === Traefik bounced us to Authelia, which will not
        // render in a frame. A real 200 means the session is good.
        if (res.type === "opaqueredirect" || res.status === 0 || !res.ok) {
          signInPrompt("");
        } else {
          attach();
        }
      })
      .catch(function (e) {
        // A network-level failure is not the same as "not signed in", so say so
        // rather than blaming the session.
        signInPrompt("The check itself failed: " + e + ".");
      });
  }

  // Buttons elsewhere on the page that switch the terminal to another program.
  // Each switch is a fresh ttyd session: the old iframe's websocket closes with
  // it, so this never holds two of the --max-clients slots at once.
  Array.prototype.forEach.call(document.querySelectorAll("[data-term-arg]"), function (b) {
    b.addEventListener("click", function () {
      setArg(b.getAttribute("data-term-arg"));
      Array.prototype.forEach.call(document.querySelectorAll("[data-term-arg]"), function (o) {
        o.setAttribute("aria-pressed", o === b ? "true" : "false");
      });
      probe();
      mount.scrollIntoView({ block: "nearest" });
    });
  });

  probe();
})();

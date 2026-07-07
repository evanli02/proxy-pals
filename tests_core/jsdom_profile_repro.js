// Reproduce the blank-profile bug: load the real index.html + app.js in jsdom,
// authenticate as Alice, navigate to Bob's profile, report what rendered.
const fs = require("fs");
const { JSDOM } = require("jsdom");

const seed = JSON.parse(fs.readFileSync("/tmp/seed.json", "utf8"));
const html = fs.readFileSync("webapp/static/index.html", "utf8");
const js = fs.readFileSync("webapp/static/app.js", "utf8");

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:8901/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

// wire fetch to the real server; capture every async failure
window.fetch = (input, init) => fetch(new URL(input, "http://127.0.0.1:8901").href, init);
window.confirm = () => true;
const errors = [];
window.addEventListener("error", e => errors.push("window.error: " + e.message));
process.on("unhandledRejection", r => errors.push("unhandledRejection: " + (r && r.stack || r)));

window.localStorage.setItem("standin_token", seed.a.token);
window.localStorage.setItem("standin_uid", seed.a.user_id);

async function main() {
  try {
    window.eval(js);           // boot the SPA (runs render() for default route)
  } catch (e) {
    errors.push("eval: " + e.stack);
  }
  await new Promise(r => setTimeout(r, 600));
  console.log("BROWSE length:", window.document.querySelector("#view").innerHTML.length);

  // click-through: navigate to Bob's profile like the explore card does
  window.location.hash = `#/profile/${seed.b.user_id}`;
  window.dispatchEvent(new window.Event("hashchange"));
  await new Promise(r => setTimeout(r, 800));

  const view = window.document.querySelector("#view").innerHTML;
  console.log("PROFILE view length:", view.length);
  console.log("PROFILE excerpt:", view.slice(0, 300).replace(/\s+/g, " "));
  console.log("ERRORS:", errors.length ? errors.join("\n---\n") : "(none)");
  process.exit(0);
}
main();

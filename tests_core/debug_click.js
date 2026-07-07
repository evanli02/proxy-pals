const fs = require("fs");
const { JSDOM } = require("jsdom");
const seed = JSON.parse(fs.readFileSync("/tmp/seed.json", "utf8"));
const dom = new JSDOM(fs.readFileSync("webapp/static/index.html", "utf8"),
  { url: "http://127.0.0.1:8901/", runScripts: "outside-only" });
const { window } = dom;
window.fetch = (i, init) => fetch(new URL(i, "http://127.0.0.1:8901").href, init);
const errors = [];
process.on("unhandledRejection", r => errors.push(String(r && r.stack || r)));
window.localStorage.setItem("standin_token", seed.a.token);
window.localStorage.setItem("standin_uid", seed.a.user_id);
window.eval(fs.readFileSync("webapp/static/app.js", "utf8"));
setTimeout(() => {
  const card = window.document.querySelector(".profile-card");
  console.log("browse cards:", window.document.querySelectorAll(".profile-card").length);
  card.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));  // real click
  setTimeout(() => {
    const v = window.document.querySelector("#view");
    console.log("view length:", v.innerHTML.length);
    console.log("has p-chat:", !!v.querySelector("#p-chat"), "| hash:", window.location.hash);
    console.log("ERRORS:", errors.length ? errors.join("\n") : "(none)");
    process.exit(0);
  }, 900);
}, 700);

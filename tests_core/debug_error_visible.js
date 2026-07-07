const fs = require("fs");
const { JSDOM } = require("jsdom");
const dom = new JSDOM(fs.readFileSync("webapp/static/index.html", "utf8"),
  { url: "http://x.local/", runScripts: "outside-only" });
const { window } = dom;
window.fetch = () => Promise.reject(new Error("network down"));  // worst case
process.on("unhandledRejection", r => { console.log("UNHANDLED (bad):", String(r)); process.exit(1); });
window.localStorage.setItem("standin_token", "t");
window.localStorage.setItem("standin_uid", "u1");
window.location.hash = "#/profile/u2";
window.eval(fs.readFileSync("webapp/static/app.js", "utf8"));
setTimeout(() => {
  const v = window.document.querySelector("#view").innerHTML;
  console.log("blank?", v.trim().length === 0, "| shows error?", v.includes("Something went wrong"));
  process.exit(0);
}, 500);

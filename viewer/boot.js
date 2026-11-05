/* 首屏引导：错误收集 + 桌面模式标记。从 index.html 的内联脚本抽出——
   CSP script-src 'self' 生效后，内联脚本会被浏览器拒绝，这里必须外部化。 */
window.__errs = [];
window.addEventListener("error", (e) => {
  window.__errs.push((e.message || "") + "@" + e.lineno);
});
window.addEventListener("unhandledrejection", (e) => {
  window.__errs.push("rej:" + (e.reason && e.reason.message || e.reason));
});
if (new URLSearchParams(location.search).get("desktop") === "1") {
  document.documentElement.classList.add("desktop-app");
}

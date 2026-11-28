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
/* 夜间模式引导：CSP script-src 'self' 拒绝内联脚本，主题必须在首帧前由
   外部脚本落 html[data-theme]，否则亮色闪烁一次才被 app.js 纠正。 */
try {
  if (localStorage.getItem("fcode-theme") === "dark") {
    document.documentElement.dataset.theme = "dark";
  }
} catch (e) { /* 私密模式忽略 */ }

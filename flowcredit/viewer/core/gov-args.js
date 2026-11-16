/* 治理台 `/挖洞` 参数解析。
 *
 * 目标路径可能含空格，所以不能简单 split；只摘掉两个可选 flag，
 * 剩余文本原样作为 target：
 *   /挖洞 /Applications/EvoX.app --target-md fixtures/evox/target.md
 *   /挖洞 /Applications/EvoX.app --target-md=fixtures/evox/target.md
 *   /挖洞 /Applications/My App.app --osv-snapshot work/audit/osv.json
 */

const FLAGS = {
  "--target-md": "targetMd",
  "--osv-snapshot": "osvSnapshot",
};

export function parseGovAuditArgs(raw) {
  let text = String(raw || "").trim();
  const result = { target: "", targetMd: "", osvSnapshot: "" };
  for (const [flag, key] of Object.entries(FLAGS)) {
    const pattern = new RegExp("(?:^|\\s)" + flag + "(?:=|\\s+)(\\S+)");
    const match = text.match(pattern);
    if (!match) continue;
    result[key] = match[1];
    text = (text.slice(0, match.index) + text.slice(match.index + match[0].length)).trim();
  }
  result.target = text.trim();
  return result;
}

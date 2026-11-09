"use strict";

/* 创建运行时的模型字段（纯逻辑）。
   规则：mock 不发模型字段；effort 只随目录里明确 supportsEffort 的模型发送。
   服务端对"不支持思考强度"的模型组合是硬拒绝（400），而本地选择会保留
   上一次支持 effort 的档位——换到 deepseek 这类模型后沿用会直接提交失败。
   目录未加载时不发 effort：宁可让服务端落 provider 默认，也不冒 400。 */

export const EFFORT_FALLBACK = "medium";

export function runModelFields(choice, catalog) {
  const key = choice && typeof choice.key === "string" ? choice.key : "";
  if (!key || key === "mock") return {};
  const entry = (catalog || []).find(model => model && model.key === key);
  const fields = { model: key };
  if (entry && entry.supportsEffort) {
    fields.effort = choice.effort || EFFORT_FALLBACK;
  }
  return fields;
}
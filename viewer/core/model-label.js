"use strict";

/* 模型展示名的唯一解析入口。事实优先级由调用方决定，这里只负责把
   provider/model 两个字段合成一个不重复、不过长的标签。 */

const clean = value => (typeof value === "string" ? value.trim() : "");

export function modelLabel(model) {
  const provider = clean(model && model.provider);
  const name = clean(model && model.model);
  if (!provider) return name;
  if (!name || name.toLowerCase() === provider.toLowerCase()) return provider;
  const lower = name.toLowerCase();
  const providerLower = provider.toLowerCase();
  // kimi / kimi-k2.6 这类 provider 已包含在模型名里的组合只显示模型名；
  // openai / gpt-4o 这类不重复的组合保留 provider，避免同名模型混淆。
  if (lower.startsWith(providerLower + "-")
    || lower.startsWith(providerLower + "/")
    || lower.startsWith(providerLower + ":")) {
    return name;
  }
  return `${provider}/${name}`;
}

export function traceModelLabel(trace) {
  return modelLabel(trace && trace.model);
}

/* trace 是运行事实的第一来源；其次才是本次运行已确认的选择/服务端运行信息。
   用户之后改选模型不能回写历史运行，所以 selectedModel 只在没有运行事实时使用。 */
export function resolveModelLabel({ trace, runModel, selectedModel } = {}) {
  return traceModelLabel(trace) || clean(runModel) || clean(selectedModel);
}
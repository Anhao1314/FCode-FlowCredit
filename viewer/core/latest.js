"use strict";

/* “最后一次选择获胜”的最小原语。
   - createLatestGate：页面/任务选择的世代号。旧世代即使后返回也不得写 DOM。
   - settleLatest：把一次异步加载绑定到某个世代，返回 stale/error/value，
     由调用方决定是否展示错误；陈旧请求的错误不会被当成当前页面的错误。
   - createRunsPoller：侧栏轮询的串行化 + 乱序丢弃。 */

export function createLatestGate() {
  let sequence = 0;
  return {
    begin() {
      const id = ++sequence;
      return {
        id,
        isCurrent: () => id === sequence,
      };
    },
    invalidate() {
      sequence += 1;
    },
    get sequence() {
      return sequence;
    },
  };
}

export async function settleLatest(ticket, load) {
  try {
    const value = await load();
    return { stale: !ticket.isCurrent(), value };
  } catch (error) {
    return { stale: !ticket.isCurrent(), error };
  }
}

export function createRunsPoller({ fetchRuns, apply, canFetch = () => true, onError = () => {} }) {
  let sequence = 0;
  let inFlight = null;

  return async function refresh({ force = false } = {}) {
    if (!canFetch()) return { skipped: true };
    // 常规轮询绝不叠加：调用方共享同一次请求，await 它的调用方仍能拿到
    // 最新结果。显式刷新（finishSession 等）可以抢在旧请求前面，旧响应
    // 会因为世代号落后而被丢弃。
    if (inFlight && !force) return inFlight;
    const id = ++sequence;
    const request = (async () => {
      try {
        const value = await fetchRuns();
        if (id !== sequence) return { stale: true };
        apply(value);
        return { applied: true, value };
      } catch (error) {
        if (id !== sequence) return { stale: true, error };
        onError(error);
        return { applied: false, error };
      }
    })();
    inFlight = request;
    try {
      return await request;
    } finally {
      if (inFlight === request) inFlight = null;
    }
  };
}
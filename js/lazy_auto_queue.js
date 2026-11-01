/**
 * ComfyUI_ZXin —— 惰性分批加载的「自动续跑」
 *
 * 后端加载器跑完一批但还剩图片时，通过 WebSocket 发 zxin.lazy_continue
 * 事件；本扩展收到后自动再排一次队，直到全部批次跑完。
 * 用户只需点一次「运行」。
 *
 * 安全边界：
 * - remaining <= 0 不排队（后端跑完最后一批时本就不发事件，这里再兜一层）
 * - 连续自动排队有上限，防止后端游标异常时无限循环
 * - 两次排队之间留最小间隔，避免与当前 prompt 的收尾广播打架
 * - 距上次排队超过 IDLE_RESET_MS 视为新一轮手动运行，计数归零
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EVENT = "zxin.lazy_continue";
const MAX_AUTO_ROUNDS = 5000;   // 连续自动排队上限
const MIN_INTERVAL_MS = 80;     // 两次排队的最小间隔
const IDLE_RESET_MS = 10000;    // 超过这个间隔没排队，视为新一轮

let autoRounds = 0;
let lastQueuedAt = 0;
let pendingTimer = null;

function queueNext() {
    if (pendingTimer) return;
    const wait = Math.max(0, MIN_INTERVAL_MS - (Date.now() - lastQueuedAt));

    pendingTimer = setTimeout(() => {
        pendingTimer = null;
        lastQueuedAt = Date.now();
        try {
            const p = app.queuePrompt?.();
            if (p && typeof p.then === "function") {
                p.catch(() => { /* 排队失败静默，由用户手动兜底 */ });
            }
        } catch (e) {
            console.warn("[ZXin] 自动续跑失败，请手动再次运行：", e);
        }
    }, wait);
}

api.addEventListener(EVENT, (event) => {
    const d = (event && event.detail) || {};
    const remaining = Number(d.remaining);
    if (!Number.isFinite(remaining) || remaining <= 0) return;

    // 距上次排队过久 → 认为是用户重新发起的一轮，重置上限计数
    if (Date.now() - lastQueuedAt > IDLE_RESET_MS) {
        autoRounds = 0;
    }
    if (autoRounds >= MAX_AUTO_ROUNDS) {
        console.warn("[ZXin] 自动续跑已达上限，停止排队（剩余 %d 张）", remaining);
        return;
    }
    autoRounds += 1;
    queueNext();
});

app.registerExtension({
    name: "ComfyUI_ZXin.LazyAutoQueue",
});

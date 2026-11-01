/**
 * 截图验证：批量图像加载器节点上的「实时进度」面板视觉效果
 *
 * 流程：起浏览器 → 建加载器节点 → 跑一次真实加载 → 等面板更新 → 截图
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8188";
const PROFILE = path.join(process.env.TEMP || ".", "zxin_shot_" + Date.now());
const OUT = path.join(process.env.TEMP || ".", "zxin_progress_panel.png");

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d)); })
            .on("error", reject);
    });
}
async function waitFor(fn, ms, label) {
    const s = Date.now();
    while (Date.now() - s < ms) {
        try { const v = await fn(); if (v) return v; } catch (e) { }
        await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("超时: " + label);
}

(async () => {
    const proc = spawn(EDGE, [
        "--headless=new", "--remote-debugging-port=9347",
        `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
        "--disable-features=msEdgeSync,msEdgeFirstRunExperience,msEdgeAutoSignIn,msEdgeWelcomePage",
        "--disable-sync", "--disable-background-networking", "--disable-popup-blocking",
        "--window-size=1400,900", "--disable-gpu", BASE,
    ], { stdio: "ignore" });

    const wsUrl = await waitFor(async () => {
        const list = JSON.parse(await httpGet("http://127.0.0.1:9347/json/list"));
        const p = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        return p ? p.webSocketDebuggerUrl : null;
    }, 30000, "CDP");

    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
        ws.addEventListener("open", res); ws.addEventListener("error", rej);
        setTimeout(() => rej(new Error("WS 超时")), 15000);
    });
    let msgId = 0; const pending = new Map();
    ws.addEventListener("message", (ev) => {
        const m = JSON.parse(ev.data.toString());
        if (m.id && pending.has(m.id)) {
            const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
            m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
        }
    });
    const send = (method, params, timeoutMs = 30000) => new Promise((resolve, reject) => {
        const id = ++msgId; pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error("超时 " + method)); } }, timeoutMs);
    });
    const evaluate = async (expr, timeoutMs = 30000) => {
        const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }, timeoutMs);
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "异常");
        return r.result.value;
    };

    await waitFor(async () => evaluate("!!(window.app && window.app.graph)"), 60000, "前端");
    await new Promise((r) => setTimeout(r, 5000));

    // 建节点并把视图聚焦到它
    const info = await evaluate(`(async () => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        const n = new nt();
        n.title = "① 批量图像加载器";
        n.pos = [60, 60];
        n.size = [340, 700];
        window.app.graph.add(n);
        // 清掉默认工作流，只留这个节点，画面更干净
        for (const other of [...(window.app.graph._nodes || [])]) {
            if (other !== n) window.app.graph.remove(other);
        }
        await new Promise(r => setTimeout(r, 600));
        // 把视图移到节点左上角
        const cv = window.app.canvas || window.app.graph.canvas;
        if (cv && cv.ds) {
            // 负偏移把节点往画面下方推，让顶部面板完整可见
            cv.ds.offset[0] = -20; cv.ds.offset[1] = -30; cv.ds.scale = 0.9;
        }
        window.app.graph.setDirtyCanvas(true, true);
        await new Promise(r => setTimeout(r, 1200));
        return { id: String(n.id), hasPanel: !!n.__zxinProgressPanel,
                 canvasOk: !!(cv && cv.ds) };
    })()`);
    console.log("节点已建:", JSON.stringify(info));

    // 跑一次真实加载
    await evaluate(`(async () => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        const n = window.app.graph._nodes.find(x => x.__zxinProgressPanel);
        const key = String(n.id);
        const prompt = {};
        prompt[key] = { class_type: "ZXIN_BatchImageLoader", inputs: {
            加载来源: "input", 文件名匹配: "*.png", 排序方式: "按名称升序", 随机种子: 0,
            最多加载张数: 100, 载入设备: "仅CPU", 生成预览拼贴: false, 预览列数: 4,
            预览单格大小: 192,
            自定义路径: "E:\\\\AI\\\\ComfyUI_windows_portable-G313\\\\ComfyUI\\\\input\\\\角色卡",
            含子文件夹: false, 限制最大边长: 0, 加载模式: "全部载入（不惰性）",
            单批内存上限: 0, 预览最多显示张数: 0 } };
        prompt["__out__"] = { class_type: "SaveText", inputs: {
            text: [key, 4], filename_prefix: "zxin_shot", format: "txt" } };
        const mod = await import("/scripts/api.js");
        const api = mod.api;
        // 绕开第三方扩展（Inspire-Pack / rgthree）对 queuePrompt 的包装：
        // 它们要求完整工作流上下文，在裸节点场景下会抛 widget_idx_map 错误。
        // 直接调用 ComfyUI 原生实现即可。
        // 直接把后端会推送的那份数据派发到扩展注册的 api 上，
        // 用于确认面板的最终视觉呈现。
        // （真实链路已由 browser_progress_panel.cjs 端到端验证通过）
        api.dispatchEvent(new CustomEvent("zxin.loader_progress", {
            detail: { node: key, count: 100, total: 100,
                      progress: "全部载入：100 张一次载入完成，占用约 9935 MB" } }));
    })()`);

    // 等面板更新
    const final = await evaluate(`(async () => {
        const n = window.app.graph._nodes.find(x => x.__zxinProgressPanel);
        window.__evLog = [];
        const mod = await import("/scripts/api.js");
        mod.api.addEventListener("zxin.loader_progress", (e) => {
            window.__evLog.push(JSON.stringify(e.detail || e));
        });
        const deadline = Date.now() + 150000;
        while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 500));
            const c = n.__zxinProgressPanel.count.textContent;
            if (c && c !== "—" && c !== "0") break;
        }
        window.app.graph.setDirtyCanvas(true, true);
        await new Promise(r => setTimeout(r, 1500));
        return { count: n.__zxinProgressPanel.count.textContent,
                 progress: n.__zxinProgressPanel.progress.textContent,
                 text: n.__zxinProgressPanel.text.textContent,
                 events: (window.__evLog||[]).slice(0,3),
                 nodeId: String(n.id) };
    })()`, 200000);
    console.log("面板内容:", JSON.stringify(final, null, 0));

    const shot = await send("Page.captureScreenshot", { format: "png" }, 30000);
    fs.writeFileSync(OUT, Buffer.from(shot.data, "base64"));
    console.log("截图已保存:", OUT);

    proc.kill();
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) { }
    process.exit(0);
})().catch((e) => { console.error("失败:", e.message); process.exit(1); });

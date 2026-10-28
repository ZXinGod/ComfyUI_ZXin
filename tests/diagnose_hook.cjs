/**
 * 诊断：为什么 onNodeCreated 钩子没有生效。
 *
 * 关键检查点：
 * 1. 扩展是否真的被加载（脚本是否执行）
 * 2. beforeRegisterNodeDef 是否被调用、nodeData.name 是什么
 * 3. onNodeCreated 是否被调用
 * 4. LiteGraph 中节点的输出是怎么建的（何时建的、在 onNodeCreated 之前还是之后）
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 8191;
const BASE = `http://127.0.0.1:${PORT}`;
const PROFILE = path.join(process.env.TEMP || ".", "zxin_edge_profile2");

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => {
            let d = "";
            res.on("data", (c) => (d += c));
            res.on("end", () => resolve(d));
        }).on("error", reject);
    });
}

async function waitFor(fn, timeoutMs, label) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try { const v = await fn(); if (v) return v; } catch (e) {}
        await new Promise((r) => setTimeout(r, 1500));
    }
    throw new Error("超时: " + label);
}

(async () => {
    await waitFor(async () => (await httpGet(BASE + "/object_info")).includes("ZXIN_"), 20000, "服务");

    const proc = spawn(EDGE, [
        "--headless=new", "--remote-debugging-port=9334",
        `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", BASE,
    ], { stdio: "ignore" });

    const wsUrl = await waitFor(async () => {
        const list = JSON.parse(await httpGet("http://127.0.0.1:9334/json/list"));
        const p = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        return p ? p.webSocketDebuggerUrl : null;
    }, 30000, "CDP");

    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
        ws.addEventListener("open", res);
        ws.addEventListener("error", rej);
        setTimeout(() => rej(new Error("WS 超时")), 15000);
    });

    let msgId = 0;
    const pending = new Map();
    ws.addEventListener("message", (ev) => {
        const m = JSON.parse(ev.data.toString());
        if (m.id && pending.has(m.id)) {
            const { resolve, reject } = pending.get(m.id);
            pending.delete(m.id);
            m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
        }
    });
    const send = (method, params) => new Promise((resolve, reject) => {
        const id = ++msgId;
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error("超时 " + method)); } }, 30000);
    });
    const evaluate = async (expr) => {
        const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "异常");
        return r.result.value;
    };

    // 先注入探针：在页面加载前挂钩，记录 beforeRegisterNodeDef 的调用
    await send("Page.enable");
    await send("Page.addScriptToEvaluateOnNewDocument", {
        source: `
        window.__zxinProbe = { beforeRegisterCalls: [], createdCalls: [], extensions: [] };
        // 记录所有扩展注册
        const _origRegister = null;
        // 延迟挂钩 app.registerExtension
        const setup = () => {
            if (!window.app) return setTimeout(setup, 50);
            const orig = window.app.registerExtension?.bind(window.app);
            if (orig) {
                window.app.registerExtension = function (ext) {
                    if (ext && ext.name) window.__zxinProbe.extensions.push(ext.name);
                    return orig(ext);
                };
            }
        };
        setup();
        `,
    });

    await send("Page.reload", { ignoreCache: true });
    await new Promise((r) => setTimeout(r, 15000));

    console.log("=== 页面加载的扩展列表（我的扩展是否在其中）===");
    const exts = await evaluate(`JSON.stringify(window.__zxinProbe?.extensions || [])`);
    const arr = JSON.parse(exts);
    console.log("  捕获到的扩展数:", arr.length);
    console.log("  含 ZXin:", arr.filter((n) => /ZXin/i.test(n)));
    console.log("  前 8 个:", arr.slice(0, 8));

    console.log("\n=== 检查前端真实结构 ===");
    const struct = await evaluate(`(() => {
        const app = window.app;
        const out = {};
        out.hasApp = !!app;
        out.appKeys = app ? Object.keys(app).filter(k => /ext|node|graph|canvas/i.test(k)).slice(0, 20) : [];
        out.hasExtensionManager = !!app?.extensionManager;
        out.hasGraph = !!app?.graph;
        out.hasCanvas = !!app?.canvas;
        out.litegraphTypes = window.LiteGraph ? Object.keys(window.LiteGraph.registered_node_types || {}).filter(k => /ZXIN/i.test(k)) : "no LiteGraph";
        out.nodeDefsKeys = app?.extensionManager?.nodeDefs ? Object.keys(app.extensionManager.nodeDefs).filter(k=>/ZXIN/i.test(k)) : "no nodeDefs";
        return out;
    })()`);
    console.log("  ", JSON.stringify(struct, null, 2));

    console.log("\n=== 直接测试节点类型与 hook ===");
    const hookTest = await evaluate(`(() => {
        try {
            const nt = window.LiteGraph?.registered_node_types?.["ZXIN_BatchImageSplitter"];
            if (!nt) return { error: "未注册" };
            const proto = nt.prototype;
            const info = {
                hasOnNodeCreated: typeof proto.onNodeCreated === "function",
                onNodeCreatedSrc: String(proto.onNodeCreated).slice(0, 300),
                hasComfyClass: !!proto.ComfyClass,
                comfyClass: proto.ComfyClass,
                nodeDataName: proto.nodeData?.name,
            };
            // 实际创建
            const n = new nt();
            info.afterCreateOutputs = n.outputs?.length;
            info.widgetNames = n.widgets?.map(w => w.name);
            return info;
        } catch (e) { return { error: String(e) }; }
    })()`);
    console.log("  ", JSON.stringify(hookTest, null, 2));

    ws.close();
    proc.kill();
})().catch((e) => { console.error("失败:", e); process.exit(1); });

/**
 * 真实浏览器端到端验证：
 * 1. 扩展加载后节点端口是否按「分支数」裁剪
 * 2. 修改「分支数」控件值，端口是否实时增减
 * 3. 非目标节点是否不受影响
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 8191;
const BASE = `http://127.0.0.1:${PORT}`;
const PROFILE = path.join(process.env.TEMP || ".", "zxin_edge_final_" + Date.now());

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => {
            let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d));
        }).on("error", reject);
    });
}
async function waitFor(fn, ms, label) {
    const s = Date.now();
    while (Date.now() - s < ms) { try { const v = await fn(); if (v) return v; } catch (e) {} await new Promise((r) => setTimeout(r, 1500)); }
    throw new Error("超时: " + label);
}

(async () => {
    await waitFor(async () => (await httpGet(BASE + "/object_info")).includes("ZXIN_"), 20000, "服务");

    const proc = spawn(EDGE, [
        "--headless=new", "--remote-debugging-port=9340",
        `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", BASE,
    ], { stdio: "ignore" });

    const wsUrl = await waitFor(async () => {
        const list = JSON.parse(await httpGet("http://127.0.0.1:9340/json/list"));
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
    const send = (method, params) => new Promise((resolve, reject) => {
        const id = ++msgId; pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error("超时 " + method)); } }, 30000);
    });
    const evaluate = async (expr) => {
        const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "异常");
        return r.result.value;
    };

    await waitFor(async () => evaluate("!!(window.app && window.app.graph)"), 60000, "前端");
    await new Promise((r) => setTimeout(r, 10000));

    let pass = 0, fail = 0;
    const check = (name, cond, extra = "") => {
        if (cond) { pass++; console.log(`  [PASS] ${name}`); }
        else { fail++; console.log(`  [FAIL] ${name} ${extra}`); }
    };

    console.log("=== 真实浏览器端到端验证 ===\n");

    // 1. 初始状态：分支数=4 → 4+1=5 个输出
    const r1 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageSplitter"];
        const n = new nt();
        return { outputs: n.outputs.length, names: n.outputs.map(o=>o.name), widget: n.widgets?.map(w=>w.name) };
    })()`);
    check("初始：分支数=4 → 输出 5 个", r1.outputs === 5, `实际 ${r1.outputs}: ${r1.names}`);
    check("初始：端口名正确", r1.names.join(",") === "分支1,分支2,分支3,分支4,分流清单", r1.names.join(","));

    // 2. 修改控件值 → 端口实时增减
    const r2 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageSplitter"];
        const n = new nt();
        const w = n.widgets.find(x => x.name === "分支数");
        w.value = 7;
        w.callback(7);
        return { outputs: n.outputs.length, names: n.outputs.map(o=>o.name) };
    })()`);
    check("改为 7 → 输出 8 个", r2.outputs === 8, `实际 ${r2.outputs}`);
    check("改为 7 后端口名正确", r2.names[7] === "分流清单" && r2.names[6] === "分支7", r2.names.join(","));

    const r3 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageSplitter"];
        const n = new nt();
        const w = n.widgets.find(x => x.name === "分支数");
        w.value = 2;
        w.callback(2);
        return { outputs: n.outputs.length, names: n.outputs.map(o=>o.name) };
    })()`);
    check("改为 2 → 输出 3 个", r3.outputs === 3, `实际 ${r3.outputs}`);
    check("改为 2 后端口名正确", r3.names.join(",") === "分支1,分支2,分流清单", r3.names.join(","));

    const r4 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageSplitter"];
        const n = new nt();
        const w = n.widgets.find(x => x.name === "分支数");
        w.value = 12;
        w.callback(12);
        return { outputs: n.outputs.length, last: n.outputs[n.outputs.length-1].name };
    })()`);
    check("改为 12 → 输出 13 个", r4.outputs === 13, `实际 ${r4.outputs}`);
    check("改为 12 后清单在最后", r4.last === "分流清单", r4.last);

    // 3. 非目标节点不受影响
    const r5 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["KSampler"];
        if (!nt) return { skip: true };
        const n = new nt();
        return { outputs: n.outputs.length };
    })()`);
    if (r5.skip) {
        console.log("  [SKIP] 非目标节点检查（KSampler 未注册）");
    } else {
        console.log(`  [INFO] KSampler 输出数 ${r5.outputs}（未被裁剪即正确）`);
    }

    // 4. 其他 ZXin 节点不受影响
    const r6 = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        const n = new nt();
        return { outputs: n.outputs.length, names: n.outputs.map(o=>o.name) };
    })()`);
    check("加载器未被误裁剪", r6.outputs === 4, `实际 ${r6.outputs}: ${r6.names}`);

    console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);

    ws.close(); proc.kill();
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
    process.exit(fail ? 1 : 0);
})().catch((e) => { console.error("失败:", e); process.exit(1); });

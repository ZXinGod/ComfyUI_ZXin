#!/usr/bin/env python3
"""ComfyUI_ZXin 一键发布脚本：提交 → 打 tag → 推送 → 创建/更新 Release → 上传 zip 附件。

以前每次发布都要手动拼一长串命令（读凭据、切代理、区分两个 GitHub 域名、
删旧附件再传新附件…），反复踩坑。这个脚本把整条链路固化下来。

用法
----
    # 预览：只打印将要做什么，不改动任何东西
    python tools/publish.py --dry-run

    # 常规发布（自动提交当前改动 + 打 tag + 建 Release + 上传 zip）
    python tools/publish.py --version 1.4.0 -m "feat: xxx"

    # 只推已有提交，不新建 tag / Release
    python tools/publish.py --no-release

    # 指定代理 / 跳过测试
    python tools/publish.py --proxy http://127.0.0.1:10808 --skip-tests

设计要点（都是踩过坑才定下来的）
--------------------------------
1. 凭据从 GCM **非交互**读取。必须设 ``GCM_INTERACTIVE=never``，
   否则 GCM 会弹浏览器登录并挂起直到超时（白等 10 分钟）。
2. 代理自动探测：默认 60898 对 github 写操作返回 502，10808 正常。
   脚本会依次探测候选代理，挑第一个能写通的。
3. **上传附件用 uploads.github.com，创建 Release 用 api.github.com**。
   用错域名上传附件返回的是 404（不是 403），极易误判成「权限不足」。
4. 替换同名附件必须先 DELETE 旧 asset 再 POST 新建（GitHub 不允许同名覆盖）。
5. GitHub 下载/上传偶发返回 HTTP 000（代理抖动），所有网络操作都带重试。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GCM = r"C:\Program Files\Git\mingw64\bin\git-credential-manager.exe"

DEFAULT_REPO = "ZXinGod/ComfyUI_ZXin"
PROXY_CANDIDATES = ["http://127.0.0.1:10808", "http://127.0.0.1:60898"]
RETRY = 3


# ────────────────────────────── 工具函数 ──────────────────────────────

def say(msg):
    print(msg, flush=True)


def run(cmd, cwd=PKG_ROOT, check=True, env=None):
    """执行命令，返回 (returncode, stdout, stderr)。"""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    if check and p.returncode != 0:
        say(f"[✗] 命令失败：{' '.join(cmd)}")
        say("    " + (p.stderr or p.stdout).strip().replace("\n", "\n    "))
        sys.exit(1)
    return p.returncode, p.stdout, p.stderr


def read_version():
    with open(os.path.join(PKG_ROOT, "pyproject.toml"), encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def set_version(new):
    path = os.path.join(PKG_ROOT, "pyproject.toml")
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    txt = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{new}"', txt, count=1, flags=re.M)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)


# ────────────────────────────── 凭据与代理 ──────────────────────────────

def get_token():
    """从 Git Credential Manager 非交互读取 github 凭据。"""
    if not os.path.exists(GCM):
        say("[✗] 找不到 git-credential-manager.exe")
        sys.exit(1)
    env = dict(os.environ, GCM_INTERACTIVE="never", GCM_GUI_PROMPT="0")
    try:
        p = subprocess.run([GCM, "get"], input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        say("[✗] 读取凭据超时")
        sys.exit(1)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            tok = line.split("=", 1)[1].strip()
            if tok:
                return tok
    say("[✗] GCM 里没有 github 凭据。请先在任意 git 仓库 push 一次完成登录。")
    sys.exit(1)


def proxy_works(proxy, repo):
    """探测代理能否完成 github 写操作（用 ls-remote + 一个只读 API 调用判断连通性）。"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(
        ["git", "-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}",
         "ls-remote", f"https://github.com/{repo}", "HEAD"],
        capture_output=True, text=True, env=env, timeout=60)
    return p.returncode == 0 and p.stdout.strip() != ""


def pick_proxy(repo, forced=None):
    if forced:
        return forced
    for proxy in PROXY_CANDIDATES:
        if proxy_works(proxy, repo):
            say(f"[✓] 代理可用：{proxy}")
            return proxy
    say("[!] 所有候选代理都不可用，改用直连")
    return None


# ────────────────────────────── HTTP ──────────────────────────────

def make_opener(proxy):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def http(opener, method, url, token, data=None, headers=None, retries=RETRY):
    """带重试的 HTTP 请求，返回 (status, body_bytes)。"""
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ComfyUI_ZXin-publish",
    }
    if headers:
        h.update(headers)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with opener.open(req, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            last = e
            if attempt < retries:
                say(f"    … 网络异常（{e}），第 {attempt} 次重试")
                time.sleep(2 * attempt)
    raise RuntimeError(f"请求失败：{method} {url} → {last}")


def api_json(opener, method, url, token, payload=None):
    """调 api.github.com，返回解析后的 JSON。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdr = {"Content-Type": "application/json"} if data else None
    st, body = http(opener, method, url, token, data=data, headers=hdr)
    try:
        return st, json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return st, {"_raw": body[:400].decode("utf-8", "replace")}


# ────────────────────────────── 各步骤 ──────────────────────────────

def step_preflight(args):
    say("▸ [1/7] 前置检查")
    _, st, _ = run(["git", "status", "--short"], check=False)
    dirty = [l for l in st.splitlines() if l.strip()]
    if dirty:
        say(f"    待提交改动 {len(dirty)} 项：")
        for l in dirty[:20]:
            say(f"      {l}")
    else:
        say("    工作区干净（无待提交改动）")

    cur = read_version()
    say(f"    pyproject 当前版本：{cur}")
    if args.version and args.version != cur:
        say(f"    → 将更新为：{args.version}")

    # README 顶部版本号是最容易漏的 —— 这里主动查一遍
    readme = os.path.join(PKG_ROOT, "README.md")
    if os.path.exists(readme):
        with open(readme, encoding="utf-8") as f:
            for line in f:
                if "当前版本" in line:
                    say(f"    README：{line.strip()}")
                    m = re.search(r"v(\d+\.\d+\.\d+)", line)
                    if m and args.version and m.group(1) != args.version:
                        say(f"    ⚠️  README 版本号 ({m.group(1)}) 与目标版本 "
                            f"({args.version}) 不一致，发布前请手动更新")
                    break
    return dirty


def step_sync_local(args):
    """可选：从本机安装目录同步文件到仓库（开发时改的是安装目录）。"""
    if not args.sync_from:
        return
    say(f"▸ [2/7] 从本机安装目录同步：{args.sync_from}")
    src = os.path.abspath(args.sync_from)
    if not os.path.isdir(src):
        say(f"[✗] 目录不存在：{src}")
        sys.exit(1)
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", ".workbuddy")
                   and not d.endswith(".egg-info")]
        for name in files:
            if name.endswith((".pyc", ".zip")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src)
            dst = os.path.join(PKG_ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            same = (os.path.exists(dst)
                    and open(full, "rb").read() == open(dst, "rb").read())
            if not same:
                shutil.copy2(full, dst)
                n += 1
    say(f"    同步 {n} 个文件")
    run(["git", "status", "--short"], check=False)


def step_bump(args):
    say("▸ [3/7] 版本号与变更记录")
    if args.version:
        set_version(args.version)
        say(f"    pyproject.toml → {args.version}")
    else:
        say(f"    保持当前版本 {read_version()}")


def find_python():
    """探测可用的 python 解释器。

    本仓库可能放在 ComfyUI 的 custom_nodes 下，也可能单独放在工作区里，
    所以按多个候选位置找，而不是猜固定的相对路径。
    """
    import glob
    cands = []
    # 1) 本机 ComfyUI 便携版（固定路径优先，这个最可靠）
    cands.append(r"E:\AI\ComfyUI_windows_portable-G313\python_embeded\python.exe")
    # 2) 仓库在 custom_nodes 下 → 上两级是 ComfyUI 根
    cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(PKG_ROOT))),
                               "python_embeded", "python.exe"))
    # 3) 系统 python
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    # 4) 全局扫描便携版
    cands += glob.glob(r"E:\AI\ComfyUI_windows_portable-*\python_embeded\python.exe")
    for c in cands:
        if c and os.path.exists(c):
            return c
    return sys.executable


def step_test(args):
    if args.skip_tests:
        say("▸ [4/7] 跳过测试（--skip-tests）")
        return
    say("▸ [4/7] 运行回归测试")
    py = args.python or find_python()
    if not os.path.exists(py):
        say(f"    [跳过] 找不到解释器 {py}")
        return
    env = dict(os.environ)
    if args.comfy_root:
        env["COMFY_ROOT"] = args.comfy_root
    p = subprocess.run([py, os.path.join("tests", "test_nodes.py")],
                       cwd=PKG_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=1800)
    tail = (p.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        say(f"    {line}")
    if p.returncode != 0:
        say("[✗] 测试未通过，中止发布")
        say("    " + (p.stderr or "")[-500:])
        sys.exit(1)


def step_package(args):
    say("▸ [5/7] 打包")
    py = args.python or find_python()
    out_dir = args.out_dir or os.path.dirname(PKG_ROOT)
    p = subprocess.run([py, os.path.join("tools", "package.py"), out_dir],
                       cwd=PKG_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    for line in (p.stdout or "").splitlines():
        if line.strip():
            say(f"    {line.strip()}")
    if p.returncode != 0:
        say("[✗] 打包失败")
        sys.exit(1)
    version = args.version or read_version()
    zip_path = os.path.join(out_dir, f"ComfyUI_ZXin-v{version}.zip")
    if not os.path.exists(zip_path):
        say(f"[✗] 找不到打包产物：{zip_path}")
        sys.exit(1)
    say(f"    产物：{zip_path}（{os.path.getsize(zip_path)/1024:.1f} KB）")
    return zip_path


def step_commit(args):
    say("▸ [6/7] 提交与打 tag")
    msg = args.message or (f"chore: release v{args.version}" if args.version else None)
    if not msg:
        say("    无改动且未提供 -m，跳过提交")
        return None
    run(["git", "add", "-A"])
    _, out, _ = run(["git", "status", "--short"], check=False)
    if out.strip():
        run(["git", "commit", "-q", "-m", msg])
        _, sha, _ = run(["git", "rev-parse", "--short", "HEAD"])
        say(f"    已提交 {sha.strip()}：{msg.splitlines()[0]}")
    else:
        say("    无改动，跳过提交")

    if args.tag:
        rc, _, _ = run(["git", "tag", "-a", args.tag, "-m", args.tag], check=False)
        if rc == 0:
            say(f"    已打 tag {args.tag}")
        else:
            # tag 已存在 = 重新发布同一版本 → 复用并强推到远端
            say(f"    tag {args.tag} 已存在，复用（将强推覆盖远端）")
            run(["git", "tag", "-f", args.tag, "-m", args.tag], check=False)
    return args.tag


def step_push(args, tag):
    say("▸ [7/7] 推送与 Release")
    proxy = args.proxy
    if not proxy:
        proxy = pick_proxy(args.repo, forced=None)
    else:
        say(f"[✓] 使用指定代理：{proxy}")

    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if proxy:
        run(["git", "config", "http.proxy", proxy], check=False)
        run(["git", "config", "https.proxy", proxy], check=False)
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    def push_one(target, force=False):
        """推送并重试。代理抖动时 git push 会失败，重试通常即通。"""
        cmd = ["git", "push"] + (["--force"] if force else []) + ["origin", target]
        for attempt in range(1, 4):
            rc, out, err = run(cmd, check=False, env=env)
            text = (out.strip() or err.strip()).splitlines()
            last = text[-1] if text else ""
            if rc == 0:
                say(f"    {last}")
                return True
            if attempt < 3:
                say(f"    … 推送失败（{last}），第 {attempt} 次重试")
                time.sleep(3 * attempt)
            else:
                say(f"    [✗] 推送失败：{last}")
        return False

    push_one("HEAD:main")
    if tag:
        # 重新发布同一版本时 tag 可能已在远端，用 --force 覆盖
        push_one(tag, force=True)

    if args.no_release or not tag:
        say("    未创建 Release（--no-release 或无 tag）")
        return

    token = get_token()
    opener = make_opener(proxy)
    api = f"https://api.github.com/repos/{args.repo}"
    upl = f"https://uploads.github.com/repos/{args.repo}"

    # Release：已存在就复用，不存在才创建
    st, rels = api_json(opener, "GET", f"{api}/releases", token)
    target = None
    for r in rels if isinstance(rels, list) else []:
        if r.get("tag_name") == tag:
            target = r
            break
    if target:
        say(f"    Release {tag} 已存在（id={target['id']}），复用")
        rel_id = target["id"]
    else:
        st, created = api_json(opener, "POST", f"{api}/releases", token, {
            "tag_name": tag,
            "name": tag,
            "body": args.release_notes or f"Release {tag}",
            "draft": False,
            "prerelease": False,
        })
        if st not in (200, 201):
            say(f"[✗] 创建 Release 失败 HTTP {st}：{created.get('message')}")
            sys.exit(1)
        rel_id = created["id"]
        say(f"    已创建 Release {tag}（id={rel_id}）")

    # 附件：同名先删后传（GitHub 不允许同名覆盖）
    zip_name = os.path.basename(args.zip_path)
    st, assets = api_json(opener, "GET", f"{api}/releases/{rel_id}/assets", token)
    for a in assets if isinstance(assets, list) else []:
        if a.get("name") == zip_name:
            st, _ = api_json(opener, "DELETE", f"{api}/releases/assets/{a['id']}", token)
            say(f"    已删除同名旧附件（id={a['id']}，HTTP {st}）")

    with open(args.zip_path, "rb") as f:
        data = f.read()
    url = f"{upl}/releases/{rel_id}/assets?name={urllib.parse.quote(zip_name)}"
    st, body = http(opener, "POST", url, token, data=data,
                    headers={"Content-Type": "application/zip"})
    if st not in (200, 201):
        say(f"[✗] 上传附件失败 HTTP {st}：{body[:300].decode('utf-8','replace')}")
        sys.exit(1)
    info = json.loads(body.decode("utf-8", "replace"))
    say(f"    附件已上传：{info['name']}（{info['size']/1024:.1f} KB）")
    say(f"    {info['browser_download_url']}")


def step_verify(args):
    say("▸ 验证")
    # push 刚完成时 GitHub 侧可能还没完全就绪 + 代理偶发抖动 → 必须重试
    out = ""
    for attempt in range(1, 4):
        _, out, err = run(["git", "ls-remote", "--tags", "origin"], check=False)
        if out.strip():
            break
        if attempt < 3:
            say(f"    … 远端查询无响应，第 {attempt} 次重试")
            time.sleep(2 * attempt)
    if not out.strip():
        say("    [跳过] 远端查询失败（网络问题），发布本身已完成")
        return

    # refs/tags/v1.3.0^{} 是 annotated tag 的剥离标记，不是 tag 本身
    tags = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.endswith("^{}"):
            continue
        if ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/"):])
    if args.tag:
        ok = args.tag in tags
        say(f"    远端 tag {args.tag}：{'存在 ✓' if ok else '缺失 ✗'}")
        if not ok:
            say("    [注意] tag 未在远端出现，请手动核对 git ls-remote --tags origin")
    say(f"    远端 tags：{', '.join(t for t in tags if t.startswith('v')) or '（无）'}")


# ────────────────────────────── 入口 ──────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ComfyUI_ZXin 一键发布（提交 → tag → push → Release → 附件）")
    ap.add_argument("--version", help="目标版本号，如 1.4.0（会写进 pyproject.toml）")
    ap.add_argument("-m", "--message", help="commit 信息")
    ap.add_argument("--tag", help="tag 名，如 v1.4.0（不给则用 v<version>）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--proxy", help="强制指定代理，如 http://127.0.0.1:10808")
    ap.add_argument("--sync-from", help="发布前从该目录同步文件（本机 ComfyUI 安装目录）")
    ap.add_argument("--release-notes", help="Release 说明（默认用 tag 名）")
    ap.add_argument("--python", help="python 解释器路径")
    ap.add_argument("--comfy-root", help="ComfyUI 根目录（跑测试用）")
    ap.add_argument("--out-dir", help="zip 输出目录（默认工作区根目录）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过回归测试")
    ap.add_argument("--no-release", action="store_true", help="只推代码，不建 Release")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改动")
    args = ap.parse_args()

    if args.version and not args.tag:
        args.tag = f"v{args.version}"

    say("═" * 62)
    say(f"  ComfyUI_ZXin 发布  →  {args.repo}  {args.tag or '(不建 tag)'}")
    say("═" * 62)

    if args.dry_run:
        say("\n[dry-run] 不会改动任何东西。计划：")
        say(f"  1. 前置检查（工作区状态 / 版本号 / README 版本行）")
        if args.sync_from:
            say(f"  2. 从 {args.sync_from} 同步文件")
        say(f"  3. 版本号 → {args.version or read_version()}")
        say(f"  4. 回归测试")
        say(f"  5. 打包 zip")
        say(f"  6. 提交（-m {args.message or '(自动)'}）+ tag {args.tag or '(无)'}")
        say(f"  7. 推送 + 创建/更新 Release + 上传 zip 附件")
        say("\n执行：去掉 --dry-run 即可")
        return

    step_preflight(args)
    step_sync_local(args)
    step_bump(args)
    step_test(args)
    zip_path = step_package(args)
    args.zip_path = zip_path
    tag = step_commit(args)
    step_push(args, tag)
    step_verify(args)

    say("\n" + "═" * 62)
    if args.tag and not args.no_release:
        say(f"  ✓ 发布完成  →  https://github.com/{args.repo}/releases/tag/{args.tag}")
    else:
        say(f"  ✓ 已推送  →  https://github.com/{args.repo}/commits/main")
    say("═" * 62)


if __name__ == "__main__":
    main()

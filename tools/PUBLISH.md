# 发布流程（维护者用）

> 这份文档给**仓库维护者**看。终端用户不需要关心本文件。

## 一句话

```bash
python tools/publish.py --version 1.4.0 -m "feat: xxx"
```

脚本会把整条链路一次跑完：前置检查 → 同步本机改动 → 写版本号 →
回归测试 → 打包 zip → 提交 → 打 tag → 推送 → 创建/更新 Release → 上传附件 → 验证。

## 仓库位置

| 用途 | 路径 |
| --- | --- |
| 工作区克隆（**改代码在这里**） | `C:\Users\60237\WorkBuddy\<会话>\ComfyUI_ZXin` |
| 本机 ComfyUI 安装目录（**运行验证在这里**） | `E:\AI\ComfyUI_windows_portable-G313\ComfyUI\custom_nodes\ComfyUI_ZXin` |

本机安装目录**不是 git 仓库**。日常开发改安装目录（能直接重启 ComfyUI 验证），
发布时用 `--sync-from` 把改动同步进克隆仓库。

## 常用命令

```bash
# 先看看会做什么（不改任何东西）
python tools/publish.py --dry-run --version 1.4.0 -m "feat: xxx"

# 常规发布
python tools/publish.py --version 1.4.0 -m "feat: xxx"

# 从本机安装目录同步后再发布
python tools/publish.py --version 1.4.0 -m "feat: xxx" \
  --sync-from "E:/AI/ComfyUI_windows_portable-G313/ComfyUI/custom_nodes/ComfyUI_ZXin"

# 只推代码，不建 Release（纯文档/小修）
python tools/publish.py --no-release -m "docs: xxx"

# 重新发布同一版本（覆盖 tag + 替换附件）
python tools/publish.py --tag v1.3.0 -m "chore: 修正 v1.3.0 的 README"

# 跳过测试（不建议）
python tools/publish.py --version 1.4.0 -m "xxx" --skip-tests
```

## 参数速查

| 参数 | 说明 |
| --- | --- |
| `--version X.Y.Z` | 目标版本，会写进 `pyproject.toml`；不给 tag 时自动用 `vX.Y.Z` |
| `-m "msg"` | commit 信息。给了才会提交（不给且无改动则跳过） |
| `--tag vX.Y.Z` | 单独指定 tag 名；已存在会自动复用并强推 |
| `--sync-from PATH` | 发布前从该目录同步文件到仓库 |
| `--release-notes "..."` | Release 说明，默认用 tag 名 |
| `--repo owner/repo` | 换仓库，默认 `ZXinGod/ComfyUI_ZXin` |
| `--proxy URL` | 强制代理，默认自动探测 |
| `--python PATH` | 指定 python 解释器，默认自动探测 |
| `--comfy-root PATH` | ComfyUI 根目录（跑测试用） |
| `--skip-tests` | 跳过回归测试 |
| `--no-release` | 只推代码，不建 Release / 不传附件 |
| `--dry-run` | 只打印计划 |

## 发布前必查清单

脚本会自动检查一部分，但**这两项必须人工确认**：

1. **README 顶部版本号** —— 脚本会打印当前值，若与 `--version` 不一致会给出警告。
   上次就是漏了这行，导致 GitHub 首页显示 v1.1.0。
2. **CHANGELOG.md 顶部条目** —— 要有当前版本的段落，说明改了什么。
3. **README 的「升级说明」节** —— 新增控件要追加到末尾并列出。

## 脚本内建的关键规则（别再手动绕）

| 规则 | 原因 |
| --- | --- |
| 凭据从 GCM **非交互**读取，必须设 `GCM_INTERACTIVE=never` | 否则弹浏览器登录并挂起 10 分钟 |
| 代理自动探测 `10808` → `60898` | 60898 对 github **写操作**返回 502，10808 正常 |
| 上传附件用 `uploads.github.com`，创建 Release 用 `api.github.com` | 用错域名返回 **404**（不是 403），看着像权限问题 |
| 替换同名附件：**先 DELETE 旧 asset 再 POST** | GitHub 不允许同名覆盖 |
| 所有网络操作带 3 次重试 | GitHub 下载偶发 HTTP 000（代理抖动） |
| tag 推送用 `--force` | 重新发布同一版本时 tag 已存在 |
| `tools/publish.py` 本身**不进发布包** | 用户安装不需要发布脚本 |

## 验证

发布完必看这两条：

```bash
# 1. 线上 README 版本号
curl -s --noproxy "*" -x http://127.0.0.1:10808 \
  -H "Accept: application/vnd.github.raw" \
  "https://api.github.com/repos/ZXinGod/ComfyUI_ZXin/contents/README.md?ref=main" \
  | grep "当前版本"

# 2. 下载线上 zip 并读包内 README
curl -s -o /tmp/v.zip --noproxy "*" -x http://127.0.0.1:10808 -L \
  "https://github.com/ZXinGod/ComfyUI_ZXin/releases/download/v1.3.0/ComfyUI_ZXin-v1.3.0.zip"
```

端到端验证（最严格）：从 GitHub 全新 clone → 跑测试 → 打包。

```bash
git clone --branch v1.3.0 --depth 1 https://github.com/ZXinGod/ComfyUI_ZXin.git _verify
cd _verify && python_embeded/python.exe tests/test_nodes.py
```

## 手动应急（脚本挂了时）

```bash
export GCM_INTERACTIVE=never GCM_GUI_PROMPT=0
git config http.proxy http://127.0.0.1:10808
git config https.proxy http://127.0.0.1:10808

git push origin HEAD:main
git push --force origin v1.3.0

TOK=$(printf 'protocol=https\nhost=github.com\n\n' \
  | "C:/Program Files/Git/mingw64/bin/git-credential-manager.exe" get | sed -n 's/^password=//p')

# 创建 Release
curl -s --noproxy "*" -x http://127.0.0.1:10808 -X POST \
  -H "Authorization: Bearer $TOK" -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d '{"tag_name":"v1.3.0","name":"v1.3.0","body":"...","draft":false}' \
  https://api.github.com/repos/ZXinGod/ComfyUI_ZXin/releases

# 上传附件（注意是 uploads.github.com）
curl -s --noproxy "*" -x http://127.0.0.1:10808 -X POST \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/zip" \
  --data-binary @ComfyUI_ZXin-v1.3.0.zip \
  "https://uploads.github.com/repos/ZXinGod/ComfyUI_ZXin/releases/<RELEASE_ID>/assets?name=ComfyUI_ZXin-v1.3.0.zip"
```

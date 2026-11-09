# 流光勾选 · 直接提交部署与读回

本文件给部署的人。它不包含口令、令牌、隧道地址或答案。

## 现在算不算线上能交

不算。`liuguang/gateway.json` 的 `gateway` 是空字符串。空的时候同一页只会说明「网关还没配置」，草稿留在浏览器里，不会发到任何主机。

现网 `https://r42hgghpkz-lang.github.io/review/liuguang/` 在你用下面的静态发布步骤更新之前，仍是只有本机草稿和导出的那一版。本变更不推 `main`，也不改 Pages 设置。

GitHub Pages 只有静态文件，没有提交后端。旧的 `127.0.0.1:8787` 章节批注服务没有在跑，也没有 systemd 单元。不要复用那一端口，也不要改章节流水线或其口令文件。

## 私密包放哪

默认路径（作者机器上的流光私密包，和文枢分开）：

```text
/workspace/tmp/liuguang-design-20261109/answers
```

用 `LIUGUANG_ANSWERS_ROOT` 可以改。进程启动时会把该目录收成 `0700`、里面的文件收成 `0600`，包括原先是 `0755` 的目录。

拒绝启动的情况：

- 路径落在任一 git 工作区里面（避免被 Pages 或仓库带走）
- 路径任一段叫 `wenshu`
- 路径落在本网关源码目录里面

在这台云端代理虚拟机上，`/workspace` 是审阅仓库的符号链接，所以上面的默认路径会被拒绝。作者自己的机器若 `/workspace/tmp/...` 不在 git 仓库里，默认路径就是对的，不用改代码。

## 运行时秘密

只放在服务器文件里，不进仓库、不进静态页、不进 `gateway.json`。

```sh
umask 077
install -m 0600 /dev/null /path/outside-repo/liuguang-submit-secret
# 用本地编辑器写入口令。不要写进 shell 历史再贴到日志里。
```

- 文件必须是普通文件，不能是符号链接，模式必须是 `0600`
- 长度 8 到 256
- `LIUGUANG_SUBMIT_PASSWORD_FILE` 指向它
- 不设置 `LIUGUANG_DATA_KEY_FILE` 时，同一份秘密既校验作者口令，也做落盘加密
- 想把落盘密钥和作者口令分开时，再设 `LIUGUANG_DATA_KEY_FILE`（同样 `0600`）
- 不要去打开或修改章节审阅口令。本程序不会读它

作者在网页里现填口令。不要让作者复制浏览器里的会话令牌。

## 起进程

在私密机器上，源码用本目录（不要放进 Pages 发布树）：

```sh
export LIUGUANG_ANSWERS_ROOT=/workspace/tmp/liuguang-design-20261109/answers
export LIUGUANG_SUBMIT_PASSWORD_FILE=/path/outside-repo/liuguang-submit-secret
export LIUGUANG_ALLOWED_ORIGINS=https://r42hgghpkz-lang.github.io
export LIUGUANG_BIND=127.0.0.1
export LIUGUANG_PORT=8791
./run.sh
```

看到 `LIUGUANG_READY bind=127.0.0.1 port=8791` 再去配路由。生产不要设置 `LIUGUANG_KDF_ITERS`。

监听默认只在本机。公开的 HTTPS 要由你前面的路由终止，再转到这个端口。日志只记方法、路径和状态码。

接口：

- `POST /v1/session`：口令换随机 bearer。响应里没有 `Set-Cookie`
- `POST /v1/submissions`：`Authorization: Bearer` 加 `Idempotency-Key`。fsync 完成才回 `persisted: true`
- `GET /v1/health`：只说明服务在，没有答案
- 其它路径，包括任何读回路径，都是 404

来源必须精确等于 `LIUGUANG_ALLOWED_ORIGINS` 里的某一项（默认只有 `https://r42hgghpkz-lang.github.io`）。没有 `Access-Control-Allow-Credentials`。这是故意的：手机 Safari 对跨站 Cookie 默认不可用，所以认证不依赖 Cookie，也不改成不登录的公开收集。

## 公网 API 基址（两段，不要混）

页面只认 `liuguang/gateway.json` 的 `gateway`，必须是 `https://主机`，没有路径、没有用户名、没有口令。试验时，从本机打开的页面才允许用查询参数 `?gw=` 指向 `http://127.0.0.1` 或 `https://*.trycloudflare.com`。github.io 上的页面忽略 `?gw=`。

### 试验：官方 Quick Tunnel（临时）

没有稳定入站时，可以用本机已有的 `cloudflared` 做一次真网络往返。URL 每次都会变，停掉进程就失效，不能写进仓库当长期地址。

```sh
cloudflared tunnel --url http://127.0.0.1:8791 --no-autoupdate
```

把终端里出现的 `https://<random>.trycloudflare.com` 填进**即将发布的那份** `gateway.json`（或只在试验页的 `?gw=` 上使用）。试验完停掉 `cloudflared`。历史 quick tunnel 地址不要复用。

### 长期：现在还没有

需要一条你自己控制的稳定 HTTPS 路由，指到 `127.0.0.1:8791`。named tunnel、现有反向代理都可以。本环境没有受控域名，也没有这条路由。长期地址确定之后，再把那个源写进发布用的 `gateway.json`，然后只发布静态树。

## 静态页怎么发布才不会把网关源码公开

这个仓库的 Pages 目前是「分支根目录即网站」。若把含 `liuguang-gateway/` 的 `main` 直接推上去，Python 源码会被公开。源码里没有口令，但私密服务的实现不该出现在 Pages 上。

发布命令（输出目录必须是空的或不存在）：

```sh
python3 publish_pages_static.py --repo /path/to/review --out /tmp/review-pages
```

输出里只有原来的公开页、`c/`、以及 `liuguang/index.html`、`questions.json`、`gateway.json`、`README.md`。没有网关、没有收据、没有口令文件。

在 Pages 仍指向整个分支根之前，不要合并本分支。把 `/tmp/review-pages` 当作发布内容；发布前再把其中的 `liuguang/gateway.json` 改成你的 HTTPS 源。仓库里的那份保持为空。

同一作者地址不变：`https://r42hgghpkz-lang.github.io/review/liuguang/`。

## 莉莉丝怎么读收据

在私密包那台机器上，不经过网页：

```sh
python3 readback.py \
  --root "$LIUGUANG_ANSWERS_ROOT" \
  --password-file "$LIUGUANG_SUBMIT_PASSWORD_FILE" \
  --check-perms \
  --latest
```

落盘密钥若是单独文件，`--password-file` 指向 `LIUGUANG_DATA_KEY_FILE`。`--list` 只打收据编号。`--id lg-...` 读指定一张。`--json` 给程序读。权限不是 `0700/0600` 时 `--check-perms` 会失败。

网页和 HTTP 都没有读回。

## 作者侧失败时会发生什么

- 网络失败、超时、HTTP 错误、回执缺少 `persisted: true` / 收据号 / 摘要：状态是失败，草稿键 `liuguang-checklist-20261109-v1` 保持原样
- 超时之后再点提交：沿用 `liuguang-checklist-20261109-v1-pending-submit` 里的同一个幂等键
- 成功：状态里有收据编号，草稿仍在，不会被清掉。同键重试不会再写一份
- 若回执是更早一次的内容、和当前草稿摘要不一致：不会假装当前草稿已保存，草稿仍在

加密包装与章节密文相同（PBKDF2-HMAC-SHA256，60 万次，然后 AES-GCM，`base64(16 字节盐 || 12 字节 IV || 密文)`），密钥是上面的运行时秘密，不是章节口令。

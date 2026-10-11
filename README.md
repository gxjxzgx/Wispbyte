# Wispbyte 多账号自动重启工具

> 基于 **GitHub Actions + SeleniumBase** 的全自动服务器重启方案，支持自动登录、广告观看、Cloudflare Turnstile 验证，通过 **Telegram** 推送运行汇总（含出口 IP 与截图）。

⚠️ **旧版 Cloudflare Workers 方案已失效**（Workers 平台无法处理 Turnstile 验证与浏览器指纹检测），本文档合并保留其 API 调用说明，**主体内容为 GitHub Actions 新方案**。  
已部署 Workers 的用户请迁移至 Actions 版本。

---

## 📌 方案对比

| 特性 | GitHub Actions（推荐） | Cloudflare Workers（已失效） |
|------|------------------------|----------------------------|
| 自动登录 | ✅ 完整模拟浏览器 | ❌ 需手动维护 Cookie |
| Turnstile 验证 | ✅ 自动识别点击 | ❌ 不支持 |
| 广告观看 | ✅ 自动处理 | ❌ 不支持 |
| 每日自动执行 | ✅ Cron 定时 / API / 手动 | ✅ Cron 触发 |
| Telegram 通知 | ✅ 每次运行一条汇总（含截图、出口 IP） | ✅ 汇总报告 |
| 部署难度 | 低（Fork + Secrets） | 中（Worker + KV） |
| 当前状态 | ✅ 有效 | ❌ 已失效 |

---

## 🚀 快速开始

### 1. 准备文件

将以下两个文件放入你的 GitHub 仓库根目录：

- `scripts/main.py` —— 主脚本（见本仓库源码）
- `.github/workflows/main.yml` —— Actions 工作流定义

> 内容较长，完整源码请从本仓库复制，或直接 Fork 本仓库。
>
> 保活：workflow 每次运行结束会调用 GitHub API 重新启用自身，避免仓库 60 天无提交导致定时任务被自动停用（不产生空提交）。

### 2. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 说明 | 示例 |
|------------|------|------|
| `WISPBYTE_1` | 第一个账号 | `alice@example.com-----mypassword` |
| `WISPBYTE_2` ... `WISPBYTE_5` | 更多账号（可选） | 同上格式 |
| `TG_BOT_TOKEN` | Telegram Bot Token（可选） | `123456:ABC-DEF1234...` |
| `TG_CHAT_ID` | Telegram Chat ID（可选） | `123456789` |

> 账号格式：`邮箱-----密码`，分隔符为 **五个连字符** `-----`。

### 3. 运行

- **手动运行**：Actions 页面 → `Wispbyte 自动重启` → Run workflow。
- **API 触发**（见下方 API 调用方式）。
- **定时运行**：workflow 已默认开启每天 UTC 3:20（北京时间 11:20）运行，修改 `schedule` 里的 cron 表达式即可调整。

---

## 🔧 详细配置说明

### 账号 Secrets 格式

每一个 `WISPBYTE_N` 变量对应一个账号，格式固定：

```
your_email@example.com-----your_password
```

示例：

```
WISPBYTE_1 = bob@test.com-----SuperSecret123
WISPBYTE_2 = alice@domain.com-----AnotherPass!
```

默认支持 1~5 个账号。若需更多，请修改脚本中 `range(1, 6)` 并添加对应 Secrets。

### Telegram 通知（可选）

1. 向 [BotFather](https://t.me/botfather) 申请 Bot Token。
2. 获取你的 Chat ID（向 bot 发消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`）。
3. 填入 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`。
4. 运行结束后，Bot 会推送一条汇总通知（含截图和出口 IP）。

### 定时任务（Cron）

编辑 `.github/workflows/main.yml` 中的 `schedule`（默认已启用）：

```yaml
on:
  schedule:
    - cron: '20 3 * * *'   # 每天 UTC 3:20（北京时间 11:20）
  workflow_dispatch:
    inputs:
      accounts:
        ...
```

---

## 🌐 触发方式与 API 调用

### 1. 手动触发（GitHub 网页）

进入 Actions 页面 → 选择工作流 → **Run workflow**。  
可选填写 `accounts` 输入框（多个邮箱用英文逗号分隔）以指定运行账号；留空则运行全部已配置账号。

### 2. API 触发（workflow_dispatch）

通过 GitHub REST API 触发工作流，适合外部定时服务或自动化集成。

#### 运行全部账号

```bash
curl -X POST \
  https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/main.yml/dispatches \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token ghp_xxxxxxxxxxxx" \
  -d '{"ref":"main","inputs":{"accounts":""}}'
```

#### 运行单个指定邮箱账号

```bash
curl -X POST \
  https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/main.yml/dispatches \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token ghp_xxxxxxxxxxxx" \
  -d '{"ref":"main","inputs":{"accounts":"alice@example.com"}}'
```

> 替换 `<用户名>`、`<仓库名>`、`ghp_xxxxxxxxxxxx`（GitHub Personal Access Token，需具有 `workflow` 权限）以及邮箱地址。  
> 多个邮箱用逗号分隔，如 `"alice@example.com,bob@test.com"`。

### 3. 定时触发

通过 Actions 内置的 `schedule` 事件，编辑 workflow 文件的 `cron` 表达式即可。

---

## 🧠 脚本工作流程

单次运行时，脚本依次处理每个账号：

1. 打开 Wispbyte 登录页，填写邮箱密码  
2. 自动点击 Cloudflare Turnstile 验证  
3. 登录成功后通过 API 获取所有服务器 ID  
4. 进入每个服务器控制台：状态为 `running` 则跳过；离线/异常时先尝试直接调用 Start API，失败再点击 **Start**  
5. 智能处理广告流程：  
   - 自动观看奖励视频（Watch ad to continue）  
   - 处理 “No ad available” 弹窗  
   - 绕过 AdBlocker 检测页  
6. 处理重启时弹出的 **CF Turnstile 二次验证弹窗**  
7. 轮询服务器状态直至 `running`  
8. 截图并记录每个账号的出口 IP，全部完成后通过 Telegram 推送一条汇总  
9. 多账号间自动切换 WARP IP（避免风控）

---

## 💬 Telegram 通知示例

配置 Telegram 后，**每次运行结束只推送一条消息**：所有截图合并为一个相册（失败截图优先，最多 10 张），汇总文字作为说明；文字超过 1024 字时改为纯文字消息。

```
✅ Wispbyte 运行汇总

🕐 时间：2026-10-12 11:20（北京时间）
⏱ 耗时：5分38秒
👤 账号：2 个，成功 2，失败 0
🖥 服务器：3 台，开机 1，跳过 2，失败 0

────────────
✅ 账号 1：a***e@gmail.com
🌐 出口 IP：104.28.1.2

🟢 服务器 a1b2c3d4
      结果：跳过（状态正常）
      状态：运行中
      运行时长：2小时13分

🟢 服务器 deadbeef
      结果：开机成功
      状态：运行中
      运行时长：35秒

────────────
✅ 账号 2：b***b@test.com
🌐 出口 IP：104.28.9.8

🟢 服务器 1234abcd
      结果：跳过（状态正常）
      状态：运行中
      运行时长：1天3小时5分
```

Actions 日志中也会打印同样的汇总，但服务器 ID 和 IP 会打码。

---

## ❌ 旧版 Cloudflare Workers 方案（已失效）

<details>
<summary>点击展开旧版说明（仅供参考）</summary>

# ⭐ Star 一下支持项目 ⭐

> 动动发财手点点 Star ⭐

基于 **Cloudflare Workers** 部署的 **Wispbyte 多账号自动重启** 脚本（Cookie 被动续期版）

---

## 📌 功能说明

* ✅ 多账号自动重启所有服务器，保持服务在线  
* ✅ Cookie **被动续期**：无需重复登录，请求时自动刷新有效期  
* ✅ 定时 Cron 触发，全量扫描并重启  
* ✅ 网页管理面板，可查看/添加/删除账号，手动更新 Cookie  
* ✅ 单账号/批量重启，状态实时反馈  
* ✅ Telegram **汇总报告**：所有账号执行完毕后一次性发送，无垃圾通知  
* ✅ 自动发现账号下的服务器（从页面智能提取 8 位 ID）  
* ✅ Cookie 过期提醒（报告中标注，面板显示红标）

---

## ⚠️ 注意事项

> ❗ 本工具依赖 Wispbyte 面板的 Cookie 维持会话，**未集成自动登录**（因 CF 验证限制）  
> ❗ Cookie 被动续期依赖网站返回的 `Set-Cookie` 响应头，若网站不返回新 Cookie，则无法续期  
> ❗ 当 Cookie 真正过期（无效）时，需**手动打开面板更新 Cookie**，否则无法重启对应账号的服务器  

---

## 📝 注册地址

👉 https://wispbyte.com

---

## 🚀 部署方式

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)，进入 **Workers & Pages**  
2. 创建一个新的 Worker，将 [`legacy/worker.js`](./legacy/worker.js) 代码粘贴进去  
3. 创建 **KV 命名空间**（名称随意），在 Worker 设置中绑定，变量名为 `WISPBYTE_KV`  
4. 配置环境变量（见下表）  
5. 部署 Worker  

---

## 🔧 环境变量配置

| 变量名               | 说明                         |
| -------------------- | ---------------------------- |
| `AUTH_KEY`           | 管理面板与 API 的密钥        |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（可选）   |
| `TELEGRAM_CHAT_ID`   | Telegram Chat ID（可选）     |

账号数据存储在 **KV** 的 `wispbyte_accounts` 键中。部署后通过管理面板添加账号。

---

## ⏰ 定时任务（Cron）

在 Cloudflare Workers 的 **Triggers** 中添加 Cron Trigger，建议每天执行一次，例如：

```
0 0 * * *
```

👉 每天自动重启所有账号下的全部服务器，执行完毕后发送一条 Telegram 汇总报告。

> ⚠️ **免费计划限制**：每个 Cloudflare 账户最多可设置 **5 个 Cron 触发器**。如有需要，可使用外部 Cron 服务（如 [cron-job.org](https://cron-job.org/)）调用 API 触发。

---

## 🌐 使用方式

### 1️⃣ 浏览器管理面板（推荐）

```
https://你的域名?key=设置的AUTH_KEY
```

面板功能：

* **🔐 密钥认证**：页面顶部输入 `AUTH_KEY` 解锁全部功能  
* **➕ 添加账号**：邮箱 + Cookie，支持批量粘贴  
* **👥 账号列表**：显示邮箱、Cookie 长度、上次更新时间，Cookie 新鲜度（绿色/黄色/红色标记）  
* **🔄 单账号重启**：点击账号旁的“重启”按钮，立即执行该账号下所有服务器重启，页面显示成功/失败数量  
* **🔄 全部重启**：点击顶部按钮，批量执行所有账号，页面显示汇总结果  
* **🍪 更新 Cookie**：弹出窗口手动粘贴新 Cookie，刷新令牌状态  
* **🗑️ 删除账号**：点击删除按钮，确认后移除  
* **📊 统计卡片**：显示账号总数、服务器总数、当前状态

---

### 2️⃣ API 触发全部账号重启

```bash
curl "https://你的域名/restart-all?key=你的AUTH_KEY"
```

---

### 3️⃣ API 触发指定账号重启

```bash
curl "https://你的域名/restart?account=admin@@gmail.com&key=你的AUTH_KEY"
```

可选参数 `server` 指定单个服务器 ID（8 位十六进制）。

---

## 💬 Telegram 通知说明

配置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 后：

* ✅ 定时任务（Cron）或面板批量重启完成后，**只发送一条汇总报告**  
* ❌ 单个账号手动重启 **不会** 发送通知（页面直接显示结果）  
* ✅ 报告格式示例：

```
📊 重启报告

账号: admin@@gmail.com
⚠️ Cookie 过期

账号: admin1@gmail.com
服务器: fbi0773
✅ 重启成功

账号: admin2@gmail.com
服务器: 31fbi73
❌ 重启失败

Wispbyte Auto Restart
```

---

## ❤️ 支持项目

如果这个项目对你有帮助：

👉 点个 **Star ⭐** 支持一下吧！

---

## ⚠️ 免责声明

本项目仅供学习研究使用。使用本脚本产生的任何后果由使用者自行承担。请遵守 Wispbyte 的服务条款。

</details>

---

## ❤️ 支持项目

如果这个项目对你有帮助，点个 **Star ⭐** 支持一下吧！

---

## ⚠️ 免责声明

本项目仅供学习研究使用。使用本脚本产生的任何后果由使用者自行承担。请遵守 Wispbyte 的服务条款。


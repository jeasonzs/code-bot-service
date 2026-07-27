# code-bot-service

Host-side Python daemon for the Code Bot USB info display (WCH CH32X033F8P6, 1.47" 320×172 IPS).

Cross-platform: **Linux / macOS / Windows** — sim 模式（浏览器预览）三平台常驻；真 USB 硬件
三平台都支持（需按平台一次性安装驱动）。

## 要求

- **Python 3.10 - 3.13**（pyproject.toml `requires-python=">=3.10,<3.14"`）
- pip 23+
- 操作系统：Linux / macOS / Windows（任选其一）

## 三平台 Python 安装

### Windows

从 [python.org/downloads](https://www.python.org/downloads/) 下载 3.11 或 3.12 安装包，
**勾选 "Add python.exe to PATH"**。**不要用 Microsoft Store 的 stub Python**
（首次启动会触发 Store 二次下载，体验差且不兼容 ctypes）。

装好后：
```powershell
py -3.11 -m pip install --upgrade pip
py -3.11 -m pip install codebot
```

### macOS

**不要用 macOS 自带的 `/usr/bin/python3`**（System Integrity Protection 会拦掉
`DYLD_LIBRARY_PATH`，vendor libusb 加载不到）。

推荐任一：
- 从 [python.org/downloads/macos](https://www.python.org/downloads/macos/) 下载安装包
- `brew install python@3.11`

```bash
python3 -m pip install --upgrade pip
python3 -m pip install codebot
```

### Linux

- Ubuntu 22.04+ / Debian 12+ / Fedora 38+ / Arch：系统 Python 3.10+ 已够用
  - **推荐**（避开 PEP 668）：`pipx install codebot`
    pipx 通常从发行版仓库装：`sudo apt install pipx`（Debian/Ubuntu）/
    `sudo dnf install pipx`（Fedora）
  - 备选：`pip install --user codebot`（旧发行版可用；新发行版 PEP 668 会拒）
  - 注：Debian 12+ / Fedora 38+ / Arch 启用 PEP 668，连 `pip install --user` 也拦，
    必须走 pipx 或 `pip install --break-system-packages`（不推荐）
  ```bash
  sudo apt install python3-pip python3-venv        # Debian/Ubuntu
  # 或
  sudo dnf install python3-pip                     # Fedora
  pipx install codebot                             # 或：pip install --user codebot（旧发行版）
  ```
- 旧发行版（Ubuntu 20.04 等 Python 3.8）：用 [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)
  或 [pyenv](https://github.com/pyenv/pyenv) 装 3.11

## 快速上手（任何平台）

```bash
# 1. 装包
pip install codebot                  # Linux PEP 668 发行版用：pipx install codebot

# 2. 一条命令搞定所有平台相关的安装（驱动 + 自启 + Claude 集成）
codebotd setup                       # 默认非交互；Linux 上 udev 那一步
                                     # 会内部用 sudo 写 /etc/udev/rules.d/，
                                     # Python 进程保持在用户 env 不动
                                     # （不被 sudo 切换到 root env，避免
                                     # `pip install --user` 下找不到 codebot）
                                     # Windows 用管理员 PowerShell 同理
#   或 CI / Docker:
codebotd setup --doctor-only         # 只验证环境，不动任何东西

# 3. 启动 daemon（sim 通道常驻；插上设备时 USB 通道同时跑）
codebotd start

# 浏览器打开 http://127.0.0.1:8080 看 LCD 渲染
# Ctrl+C 优雅退出

# 卸载：反向操作（清掉 setup 装的所有东西）
codebotd teardown                    # 同样默认非交互
pip uninstall codebot                # 单独卸载 Python 包
```

`codebotd setup` 自动识别平台：

- **Linux**：装 udev 规则 + 注册 systemd 用户级 unit（用户登录自启）
- **macOS**：TCC 提示 + 写 `~/Library/LaunchAgents/com.codebot.codebotd.plist` + `launchctl load -w`
- **Windows**：装 WinUSB INF（管理员） + 注册 Task Scheduler 任务（onlogon）

倒数第二步（phase 4）会问你要不要配 GitHub token（LCD 的 GitHub 页要用）。
**直接回车就跳过**，随时可以之后补。最后一步（phase 5）才装自启 + 拉起
daemon——这样 token / Claude 集成配置都已经落盘,daemon 起来时直接读到。
细节见下面"GitHub token（可选）"。

整个进程跑在 `sudo` 下时，user-scope 写入（systemd unit / plist / Claude
集成）会落到 **`/root`** 而不是你的 home，daemon（用户态运行）读不到
这些文件，setup 看起来成功了但其实没生效。所以 **`codebotd setup`
不要 `sudo` 跑**。udev 那一步如果需要 root，由 `os_helper.run_as_root`
内部只对那一条 shell 命令加 sudo，Python 进程保持在你的 env 不动。
见下面"Sudo 用法"段。

幂等。再跑一次不会出错。

`codebotd teardown` 同样幂等：再跑是 no-op。不会动 daemon 状态文件 / `~/.code-bot/`。

## 子命令

| 命令 | 说明 |
|---|---|
| `codebotd start` | 启动 daemon（sim + USB 双发） |
| `codebotd stop` | 停止运行中的 daemon（通过 loopback control port） |
| `codebotd status` | 查看 daemon 状态（PID / ports / USB device） |
| `codebotd doctor` | 环境诊断（Python / 依赖 / libusb / 设备枚举） |
| `codebotd setup` | 一条命令搞定平台驱动 + 自启 + Claude 集成 |
| `codebotd teardown` | `setup` 的反向：按 反向顺序 停 daemon → 清 Claude 集成 → 拆驱动。**不动** `~/.code_bot/config.yml`(GitHub token 保留) |
| `codebotd test-protocol` | USB 协议编解码自检 |

## Sudo 用法

`codebotd setup` 的 **udev 阶段**（写 `/etc/udev/rules.d/`）需要 root。
**不要** `sudo codebotd setup`——`codebotd` 在 root env 下找不到
`codebotd` 自己（root 的 `secure_path` 不含 `~/.local/bin/`），而且
user-scope 写入（systemd --user / plist / Claude settings / config.yml）
会落到 `/root/...`，daemon 读不到，等于没装。

正确做法：**用户态跑 `codebotd setup`**，udev 那一步由 `os_helper.run_as_root`
内部只对那一条 shell 命令加 sudo。Python 进程保持在你的 env（venv /
`~/.local/`）不动，imports 不断。

```text
$ codebotd setup
[setup] phase 2/5: driver (linux)
[setup.driver] Installing udev rule to /etc/udev/rules.d/99-codebot.rules
[sudo] password for you: ********
[setup.driver] udev rules reloaded
[setup] phase 3/5: Claude Code integration
[setup] phase 4/5: GitHub token (optional)
[setup] phase 5/5: service (linux)
[setup.service] Installed /home/you/.config/systemd/user/codebot.service
[setup.service] Daemon enabled and started for this user.
```

| 操作 | sudo? |
|---|---|
| `pip install codebot` / `pipx install codebot` | **否**（用户空间） |
| `apt install libusb-1.0-0` / `dnf install libusb` 等系统包 | **是**（一次性） |
| `codebotd start` / `stop` / `status` / `doctor` | **否**（用户态 daemon：systemd `--user` / LaunchAgent / Task Scheduler 用户任务） |
| `codebotd setup` / `teardown` | **内部 sudo（仅 udev 那一步 shell 命令）**；Windows 用管理员 PowerShell；macOS 不需要 |
| `apt install python3-pip` / `pipx` 等系统包 | **是**（一次性） |

**已经 root 的容器 / 远程 chroot**：`codebotd setup` 直接跑就行
（`os_helper.run_as_root` 检测到 `euid=0` 就跳过 `sudo` 前缀）。

**非交互环境**（CI / cron）：setup 跑到 udev 那一步会卡 sudo 密码框——
`codebotd setup --doctor-only` 可以只跑环境检查不写东西；或者先
`sudo -v` 把凭证缓存住再跑 setup。

**sudo 不可用**（容器没装 `sudo` 包）：`run_as_root` 会抛
`RuntimeError("sudo is required to install udev rules")`。这条在
最小化容器里常见，最干净是 `apt install sudo`。

## Claude Code 集成（可选）

如果你用 [Claude Code](https://claude.com/product/claude-code)，`codebotd setup` 会自动把 statusline + 8 hooks 合并进 `~/.claude/settings.json`。LCD 的 Claude 页就有数据了。

工作原理：

```
Claude Code 会话
  ├─ statusLine (每次渲染) ──> stdin JSON ─> codebot-claude-statusline ──> ~/.code-bot/claude-state.json
  └─ 8 lifecycle hooks  ───> stdin JSON ─> codebot-claude-status-hook  ──> ~/.code-bot/claude-status.json
                                                                       │
                                                                       ▼
                                                       codebotd 的 ClaudeCollector
                                                       (4Hz 轮询两个 JSON)
                                                                       │
                                                                       ▼
                                                                  LCD Claude 页
```

不装 Claude 集成也完全 OK —— LCD 其他页面（时钟 / 系统 / GitHub）正常工作；
只有 Claude 页会显示 idle / 没数据。

Claude 集成是幂等的：再跑会备份 `~/.claude/settings.json.<TS>.bak` 然后覆盖 statusLine + hooks 块，保留 `mcpServers` / `permissions` 等其他 key。

## GitHub token（可选）

LCD 的 GitHub 页要一个 personal access token（PAT）才有数据。
`codebotd setup` 的最后一步会问：

```text
[setup] phase 4/5: GitHub token (optional)
  Code Bot's GitHub page needs a personal access token
  (scopes: repo, read:user — read-only stats, nothing is written).
  Create one at: https://github.com/settings/tokens/new?scopes=repo,read:user...
  Press Enter on an empty prompt to skip; you can add it later.

  GitHub token (hidden, Enter to skip):
```

- 输入是隐藏的（`getpass`），token 不会留在终端 scrollback 里。
- 存进 `~/.code_bot/config.yml` 的 `github.token`，权限 600。
- 存之前会拿 `GET /user` 验一下，通过会打印认证到的用户名；验不过（401 /
  离线）可以选择"仍然保存"，或重试，或跳过。
- **随时可以跳过**：直接回车。之后补的两种办法：编辑
  `~/.code_bot/config.yml`，或 `export GITHUB_TOKEN=<pat>` 再起 daemon。
- 已经配过 token 时，默认保留不动（`--interactive` 下才问要不要换）。
- `$GITHUB_TOKEN` 已经在环境里时整个 phase 跳过——运行时 env 优先于配置文件。
- 非 TTY（CI / 管道 / `--doctor-only`）不会卡住：打印补配方式然后跳过。

不配 token 也完全 OK —— 其他页面正常，只有 GitHub 页显示一个 warning banner。

## 架构

- **transport**: pyusb + libusb（Linux 系统包 / macOS IOKit / Windows vendor dll fallback）
- **render**: Pillow，7-seg 字体 + 50×50 PNG 图标 + VSCode Dark+ 调色板
- **sim**: stdlib HTTP server（无第三方依赖）
- **collectors**: 后台线程 — psutil 系统指标 / GitHub API / Claude Code 状态文件

## 故障排查

跑 `codebotd doctor`，所有 FAIL 项都给出修复指引。常见：

- `libusb FAIL on Linux` → `sudo apt install libusb-1.0-0`
- `USB device scan INFO not found` → 检查 udev（Linux）/ WinUSB 绑定（Windows）/ TCC 权限（macOS）
- Python 版本太旧 → 按上面"三平台 Python 安装"升级
- `setup` 卡在 `sudo` 密码框 → 输入密码，或先 `sudo -v` 把凭证缓存住再跑
- 容器 / 最小镜像无 `sudo` → `apt install sudo`（udev 那一步靠它）；
  或者直接以 root 进入容器跑 setup，user-scope 文件会落到 `/root/...`
- `sudo is required to install udev rules` → 当前环境没装 `sudo` 二进制，
  且 euid 不是 0；装 sudo，或以 root 身份跑 setup

## 详细文档

- [docs/hardware-setup.md](docs/hardware-setup.md) — 三平台 USB 驱动安装详解
- [pyproject.toml](pyproject.toml) — 包元数据与依赖

## License

MIT

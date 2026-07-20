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
  ```bash
  sudo apt install python3-pip        # Debian/Ubuntu
  # 或
  sudo dnf install python3-pip        # Fedora
  pip3 install --user codebot
  ```
- 旧发行版（Ubuntu 20.04 等 Python 3.8）：用 [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)
  或 [pyenv](https://github.com/pyenv/pyenv) 装 3.11

## 快速上手（任何平台）

```bash
# 1. 装包
pip install codebot

# 2. 一条命令搞定所有平台相关的安装（驱动 + 自启 + Claude 集成）
codebotd setup                       # 默认非交互；sudo / 管理员按提示
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
| `codebotd teardown` | `setup` 的反向：清掉所有平台相关的东西（驱动 / 自启 / Claude 集成） |
| `codebotd test-protocol` | USB 协议编解码自检 |

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

## 详细文档

- [docs/hardware-setup.md](docs/hardware-setup.md) — 三平台 USB 驱动安装详解
- [pyproject.toml](pyproject.toml) — 包元数据与依赖

## License

MIT

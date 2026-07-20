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

# 2. 验证环境（Python / 依赖 / libusb / 设备）
codebotd doctor

# 3. 装平台 USB 驱动（一次性，详见 docs/hardware-setup.md）
sudo codebotd setup-driver           # Linux/macOS 可省 sudo；Windows 需管理员

# 4. 启动 daemon（sim 通道常驻；插上设备时 USB 通道同时跑）
codebotd start

# 浏览器打开 http://127.0.0.1:8080 看 LCD 渲染
# Ctrl+C 优雅退出
```

## 子命令

| 命令 | 说明 |
|---|---|
| `codebotd start` | 启动 daemon（sim + USB 双发） |
| `codebotd stop` | 停止运行中的 daemon（通过 loopback control port） |
| `codebotd status` | 查看 daemon 状态（PID / ports / USB device） |
| `codebotd doctor` | 环境诊断（Python / 依赖 / libusb / 设备枚举） |
| `codebotd setup-driver` | 一次性安装平台 USB 驱动（Linux udev / Windows INF / macOS TCC 提示） |
| `codebotd install-claude` | 把 Claude Code statusline + 8 hooks 合并进 `~/.claude/settings.json`（让 LCD Claude 页有数据） |
| `codebotd test-protocol` | USB 协议编解码自检 |

## Claude Code 集成（可选）

如果你用 [Claude Code](https://claude.com/product/claude-code)，装一下让 LCD 的 Claude 页显示实时状态：

```bash
pip install codebot           # 会顺带装两个 console_scripts:
                              #   - codebot-claude-statusline
                              #   - codebot-claude-status-hook
                              # 它们在 PATH 上, 跨平台 (Win/macOS/Linux)
codebotd install-claude       # 把 statusLine + 8 个 hooks 合并进 ~/.claude/settings.json
```

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

不跑 `install-claude` 完全 OK —— LCD 其他页面（时钟 / 系统 / GitHub）正常工作；
只有 Claude 页会显示 idle / 没数据。

`install-claude` 是幂等的：再跑会备份 `~/.claude/settings.json.<TS>.bak` 然后覆盖 statusLine + hooks 块，保留 `mcpServers` / `permissions` 等其他 key。

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

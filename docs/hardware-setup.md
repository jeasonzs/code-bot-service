# Hardware Setup — USB Driver Installation per Platform

This guide covers installing the OS-level driver / permissions required to
talk to the Code Bot USB device (VID=`0x1A86` PID=`0xCB0B`, WCH CH32X033F8P6).

## 一次性流程

```bash
# 1. 装包
pip install codebot                  # Linux PEP 668 发行版用：pipx install codebot

# 2. 一条命令搞定所有平台安装（驱动 + 自启 + Claude 集成）
sudo "$(which codebotd)" setup       # 绝对路径写法，跨所有装法通用
#   或 CI / Docker（只验证环境，不安装任何东西）：
codebotd setup --doctor-only         # 这一行不需要 sudo（不动 /etc/）

# 3. 启动 daemon（sim 通道常驻；插上设备时 USB 通道同时跑）
codebotd start
```

`codebotd setup` 自动：
1. 先跑 `codebotd doctor` 检查环境（FAIL 仅 warn，不阻塞）
2. 按平台分支：装 udev 规则（Linux）/ 提示即插即用（Windows, MS OS 2.0 免驱）/ 提示 macOS TCC
3. 注册 daemon 自启：systemd 用户级 unit（Linux）/ LaunchAgent（macOS）/ Task Scheduler 任务（Windows）
4. 把 Claude Code statusline + 8 lifecycle hooks 合并进 `~/.claude/settings.json`

幂等。再跑一次不会出错。

---

## Linux

### 系统要求

- `libusb-1.0-0`（绝大多数发行版预装）
- `udev`（systemd-based 发行版默认）
- 用户加入 `plugdev` 组（可选，避免 sudo）

### 安装 libusb

```bash
# Debian / Ubuntu
sudo apt install libusb-1.0-0

# Fedora / RHEL
sudo dnf install libusb

# Arch
sudo pacman -S libusb

# Alpine
sudo apk add libusb
```

### 安装 udev 规则

```bash
codebotd setup
```

这会：
1. 把 `udev/99-codebot.rules` 复制到 `/etc/udev/rules.d/`（udev 阶段
   内部走 `os_helper.run_as_root`，会提示一次 sudo 密码）
2. `udevadm control --reload-rules && udevadm trigger`
3. 注册 systemd 用户级 unit `~/.config/systemd/user/codebot.service`，
   `ExecStart=` 自动填入 `which codebotd` 解析到的绝对路径
4. 提示 `sudo usermod -aG plugdev $USER`（可选，登录后生效）

> **`codebotd setup` 不要 sudo 跑**。`codebotd` 在 root env 下找不到
> 自己（root 的 `secure_path` 不含 `~/.local/bin/`），而且 phase 5 的
> systemd unit、phase 3 的 Claude settings、phase 4 的
> `~/.code_bot/config.yml` 等 user-scope 写入会落到 `/root/...`，
> daemon 读不到，等于没装。`run_as_root` helper 已经把 udev 那一条
> shell 命令单独 sudo，Python 进程保持用户 env。

`codebotd setup`（无 sudo）下 udev 阶段如果走不到 `/etc/udev/rules.d/`
会**直接报错退出**——没有 fallback 到 `~/.config/udev/rules.d/`。那路径
在 systemd 系发行版基本不加载，写了也是装了个寂寞。需要 root 时让 setup
自己提示密码。

### 验证

```bash
codebotd doctor
# 期望: USB device scan: PASS found device bus=N addr=M
```

重新插拔设备让 udev 应用新规则。

---

## macOS

### 系统要求

- pyusb ≥ 1.x（项目依赖）
- 无需额外包（pyusb 自动用 Apple IOKit 框架，不依赖 libusb）

### 安装

macOS 不需要装任何驱动。第一次插设备时系统会弹权限窗：

```
"Allow accessory to connect?"
→ 点 Allow
```

如果之前误点 Deny，重置 TCC 条目：

```bash
sudo killall usbd
# 然后拔掉重新插
```

### 验证

```bash
codebotd doctor
# 期望: libusb: not required on macOS (pyusb uses IOKit backend) [PASS]
# 期望: USB device scan: PASS found device bus=N addr=M
```

### 故障排查

| 现象 | 修复 |
|---|---|
| `device not configured` | 拔掉重新插；检查系统设置 → 隐私与安全 → USB |
| `Access denied` (TCC) | `sudo killall usbd` 然后重插 |
| `pyusb` 报错 | `pip install --upgrade pyusb` |

---

## Windows

### 系统要求

- Python ≥ 3.10（python.org 安装，**不要** Microsoft Store stub）
- Windows 10 1809+ / Windows 11（任意版本均支持 MS OS 2.0 Descriptor）
- pyusb ≥ 1.x
- **不需要**管理员权限（这是 v0.18 起的关键改进）

### 安装：免驱

Code Bot 用 [Microsoft OS 2.0 Descriptors](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/microsoft-os-2-0-descriptors-specification)
在固件里声明 Interface 0 (Vendor Bulk) 为 WinUSB 兼容。Windows 8.1+
在设备首次插入时会主动拉这个 descriptor，并自动把 Interface 0 绑到
inbox `winusb.sys`——**无需 INF、无需管理员 shell、无需代码签名**。

Interface 1 (HID Keyboard) 走标准 HID 类驱动，跟普通 USB 键盘没区别。

```powershell
# 任意 PowerShell / cmd, 不需要管理员
pip install codebot
codebotd setup
```

`codebotd setup` 的 Windows 分支不再调 `pnputil`——它就是一次 pass-through，
告诉你「插上设备就能用」，然后注册 Task Scheduler 任务
`CodeBot`（onlogon trigger，运行 `codebotd start`）。

### 验证

```powershell
codebotd doctor
# 期望: USB backend: winusb1 backend available [PASS]
# 期望: USB device scan: PASS found device bus=N addr=M
```

打开 **设备管理器** → Universal Serial Bus devices → 找到 "Code Bot
USB Display (Interface 0 - Vendor)"。右键 → 属性 → 驱动程序 → 提供商
应显示 "Microsoft"，驱动日期是 inbox `winusb.sys` 的版本。

### 故障排查

| 现象 | 修复 |
|---|---|
| `USB backend FAIL` (winusb1) | 重装 pyusb: `pip install --upgrade pyusb` |
| `USB device scan INFO not found` | 拔掉重插一次，让 host 重新枚举 |
| `device not configured` | 重新插设备；WinUSB 绑定有时需要 replug 触发 |
| 设备管理器显示黄色警告 | 卸载设备 → 拔掉 → 重插；MS OS 2.0 重新触发绑定 |
| 误用 Zadig 绑过整个设备 | 设备管理器卸载 → 拔掉 → 重插（WinUSB 绑定是 inbox 的，Zadig 装的是 libusb-win32，会覆盖） |

---

## 故障排查总览

任何平台先跑：

```bash
codebotd doctor
```

看哪一项 FAIL，按它的提示修。

| FAIL | 平台 | 修法 |
|---|---|---|
| `Python version FAIL` | 全平台 | 装 Python 3.10-3.13 |
| `pyusb FAIL` | 全平台 | `pip install codebot[usb]` |
| `libusb FAIL on Linux` | Linux | `sudo apt install libusb-1.0-0` |
| `USB backend FAIL on Windows` | Windows | `pip install --upgrade pyusb` (winusb1 模块) |
| `USB device scan INFO not found` | 全平台 | 插设备；首次插时 Windows 自动绑 WinUSB |

## 卸载

`codebotd teardown` 把 `setup` 装下的所有东西清掉，**按反向顺序**：

1. systemd user unit / launchd LaunchAgent / Task Scheduler 任务
   (停 daemon,避免 collector 与后续清理 race)
2. `~/.claude/settings.json` 里的 `statusLine` + `hooks` 块(备份后删除)
3. udev 规则(系统级 `/etc/udev/rules.d/` 和用户级 `~/.config/udev/rules.d/` 两处)

**保留**:`~/.code_bot/config.yml`(GitHub token 不删),`~/.local/share/codebot/`
状态文件,`~/.code-bot/` 运行时目录——重跑 `codebotd setup` 时这些不需要重建。

```bash
codebotd teardown                    # 默认非交互
# 然后单独卸载 Python 包：
pip uninstall codebot
```

幂等：再跑一次是 no-op。

## libusb 加载策略（v0.18 重构）

| 平台 | pyusb backend | libusb 二进制来源 |
|---|---|---|
| Linux | libusb1 | 系统包 `libusb-1.0-0` |
| macOS | IOKit | 不需要（pyusb 用 ctypes 调系统 framework） |
| Windows | winusb1 | 不需要（inbox `winusb.sys`，MS OS 2.0 自动绑） |

**Windows 不再 vendor libusb.dll**：v0.18 之前 project vendored 了
`libusb-1.0.dll` 作为 WinUSB 失败时的 fallback，但 MS OS 2.0 Descriptor
让 inbox `winusb.sys` 在首次插入时自动生效，WinUSB 绑定不会再失败——
vendor dll 反而引入了「万一装上 libusb-win32 而不是 inbox winusb」
这种 silent override 问题。所以 v0.18 起彻底删掉。

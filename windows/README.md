# Windows Service (NSSM)

## 前置

- **Python 3.10+** from [python.org](https://www.python.org/downloads/) (NOT Microsoft Store stub)
- **NSSM** (the Non-Sucking Service Manager): `choco install nssm` 或从 [nssm.cc](https://nssm.cc/download) 下
- **管理员权限** — install.bat 自己会提示

## 安装

```powershell
# 以管理员身份运行 PowerShell
cd path\to\code-bot-service
.\windows\install.bat
```

install.bat 做三件事：
1. **装 INF**（如果存在）：`pnputil /add-driver codebot-inface0.inf /install`
2. **装服务**：`nssm install codebotd python.exe -m codebot start`
3. **起服务**：`nssm start codebotd`

## 验证

```powershell
# 服务控制台
services.msc    # 找 "codebotd"

# 命令行
codebotd status
# 期望: PID: XXXX, Control port: YYYY, Sim port: 8080

# 日志
cat $PROGRAMDATA\codebot\codebotd.out.log
cat $PROGRAMDATA\codebot\codebotd.err.log

# 实时跟踪
Get-Content $PROGRAMDATA\codebot\codebotd.out.log -Wait
```

## 卸载

```powershell
# 停 + 删
nssm stop codebotd
nssm remove codebotd confirm

# （可选）卸 INF
pnputil /delete-driver codebot-inface0.inf /uninstall /force

# （可选）清日志
del $PROGRAMDATA\codebot\codebotd.*.log
```

## 故障排查

| 现象 | 修复 |
|---|---|
| `nssm not found` | `choco install nssm` 或手动放 `C:\Tools\nssm\win64\` |
| `python.exe not found` | 装 python.org Python；勾 Add to PATH |
| 服务起不来 | 看 `$PROGRAMDATA\codebot\codebotd.err.log` |
| 设备找不到 | 跑 `codebotd setup-driver` 检查 INF；检查设备管理器 |
| PID file 在哪里 | `%LOCALAPPDATA%\codebot\codebotd.pid` |

## 工作原理

`nssm install codebotd python.exe -m codebot start` 注册一个服务：
- **App**: `python.exe -m codebot start`
- **Display**: "Code Bot USB info display daemon (CH32X033F8P6)"
- **Startup**: 自动（auto）
- **Logs**: `%PROGRAMDATA%\codebot\`（log rotation 1MB）

daemon 自己前台运行、自己写 PID 文件、自己监听 loopback control port；
service 控制只负责"启动 / 停止 / 重启" 这个进程，**不**充当 PID 提供者
（用 NSSM 的 AppExit 默认行为就够）。

停止流程：
1. `nssm stop codebotd` → NSSM 给 python.exe 发 Ctrl+C
2. daemon 捕获 KeyboardInterrupt，优雅退出
3. nssm 检测到 exit code 0 → 服务停止
4. daemon 删 PID 文件

如果想用我们的 `codebotd stop` 流程：
```powershell
codebotd stop    # 走 PID file + loopback control port
```

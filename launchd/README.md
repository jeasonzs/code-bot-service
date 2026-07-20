# launchd LaunchAgent (macOS)

## 安装

```bash
# 装 entry point
pip3 install --user codebot

# 确认 codebotd 在 PATH
which codebotd    # 期望: /usr/local/bin/codebotd 或 ~/.local/bin/codebotd

# plist 里 ProgramArguments 路径与实际路径一致
# （默认 /usr/local/bin/codebotd; 如果 pip --user 装在 ~/.local/bin/，改成那个）

# 复制到用户级 LaunchAgents
cp launchd/com.codebot.codebotd.plist ~/Library/LaunchAgents/

# 加载 + 立即启动
launchctl load -w ~/Library/LaunchAgents/com.codebot.codebotd.plist

# 验证
launchctl list | grep codebot
codebotd status
```

## 开机自启

`RunAtLoad=true` + `KeepAlive.Crashed=true`：用户登录后自动启动；
崩溃自动重启（5s 节流，最多 10 次/60s 窗口）。

> 注意：launchd user-level LaunchAgent **只在 Aqua session 启动**。
> SSH-only session 不会自动启动，需要手动 `launchctl start com.codebot.codebotd`。

## 卸载

```bash
launchctl unload -w ~/Library/LaunchAgents/com.codebot.codebotd.plist
rm ~/Library/LaunchAgents/com.codebot.codebotd.plist
```

## 日志

```bash
# 标准输出/错误
tail -f /tmp/com.codebot.codebotd.out.log
tail -f /tmp/com.codebot.codebotd.err.log

# 或统一日志系统（Console.app / log stream）
log stream --predicate 'process == "codebotd"' --style compact
```

## 修改 ProgramArguments 路径

如果 `pip install --user` 把 codebotd 装到 `~/Library/Python/<ver>/bin/` 或
`~/.local/bin/` 而非 `/usr/local/bin/`，需要修改 plist 第一项：

```xml
<key>ProgramArguments</key>
<array>
    <string>/Users/YOUR_USERNAME/.local/bin/codebotd</string>
    <string>start</string>
</array>
```

## 注意

- 首次启动需要 USB 设备权限（TCC 弹窗），跑 `codebotd setup-driver` 看指引
- macOS Big Sur+ 对未签名二进制要求更严；如有问题用 python.org Python
- 如果 daemon 跑在 venv 里，把 venv 的 python -m codebot.cli start 作为 ProgramArguments

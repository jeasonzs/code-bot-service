# systemd user unit (Linux)

## 安装

```bash
# 装 entry point (如果用 pip install --user, codebotd 已自动装到 ~/.local/bin/)
pip install --user codebot

# link service 到 systemd user 目录
mkdir -p ~/.config/systemd/user
ln -sf $(pwd)/systemd/codebot.service ~/.config/systemd/user/codebot.service

# 重载 + 启用 + 启动
systemctl --user daemon-reload
systemctl --user enable --now codebot.service

# 验证
systemctl --user status codebot.service
codebotd status
```

## 开机自启

user unit 默认在用户登录时启动。要让 daemon 在用户没登录时也跑：

```bash
sudo loginctl enable-linger $USER
```

## 卸载

```bash
systemctl --user disable --now codebot.service
rm ~/.config/systemd/user/codebot.service
systemctl --user daemon-reload
```

## 日志

```bash
journalctl --user -u codebot.service -f
```

## 说明

- `ExecStart=%h/.local/bin/codebotd start`：用 `%h` 展开用户主目录
- `Type=simple`：daemon 自己前台跑，自己管 PID 文件（不再用 systemd 的 `PIDFile=`）
- `Restart=on-failure`：崩溃时 5s 后重启
- `PrivateTmp=true` + `ProtectSystem=full`：最小权限（daemon 不写 /etc /usr）
- `ProtectHome=read-only`：可读 home（找 config），不能写 home（写 ~/.local/share/codebot）
  如需写 ~/.local/share 请改成 `ProtectHome=true` (WritesAllowed=...)

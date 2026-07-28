"""Command-line interface for codebotd."""

from __future__ import annotations

import os
import sys
import click
from .daemon import run_daemon
from .doctor import run_doctor
from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="codebotd")
def cli():
    """Code Bot - USB info display daemon for CH32X033F8P6 screen."""
    pass


@cli.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)")
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--sim-port", type=int, default=8080,
              help="HTTP port for the browser sim (default 8080). "
                   "Sim channel is always on; USB channel is started when a device is found.")
def start(foreground: bool, config: str | None, verbose: bool, sim_port: int):
    """Start the codebotd daemon.

    The daemon always serves the browser sim (debug channel). When a Code Bot
    device (VID=0x1A86 PID=0xCB0B) is detected on USB, the same frames are
    also pushed to the device. If no device is found, the daemon continues
    running in sim-only mode (warning logged, no exit).
    """
    run_daemon(foreground=foreground, config_path=config, verbose=verbose,
               sim_port=sim_port)


@cli.command()
def doctor():
    """Run environment diagnostics (Python / deps / libusb / USB device)."""
    sys.exit(run_doctor())


@cli.command()
@click.option("--yes", "-y", is_flag=True,
              help="非交互：所有提示都取默认值，不问任何问题（CI / 脚本用）。")
@click.option("--doctor-only", is_flag=True,
              help="只跑 doctor 检查环境，不安装任何东西（CI 友好）。")
def setup(yes: bool, doctor_only: bool):
    """一条命令完成平台感知安装：USB 驱动 + 守护进程自启 + Claude 集成。

    自动识别当前平台（Linux / macOS / Windows）。在真正的 TTY 里默认
    走交互向导（每步问你怎么处理）；传 ``--yes`` 关掉所有提示。
    幂等：再跑一次不会出错。
    """
    from . import _ui
    from .setup import run_setup

    # Interactive iff the user didn't force --yes AND both sides of the
    # terminal are real TTYs. The TTY check is what makes
    # `codebotd setup | tee log` and `codebotd setup < /dev/null` behave
    # like --yes even without the flag — the output stays plain text
    # either way.
    auto = not yes and sys.stdin.isatty() and sys.stdout.isatty()
    _ui.bind(interactive=auto)

    sys.exit(run_setup(doctor_only=doctor_only))


@cli.command()
@click.option("--timeout", type=float, default=5.0,
              help="Timeout (seconds) for STOP ack from daemon (default 5)")
def stop(timeout: float):
    """Stop the running codebotd daemon (cross-platform).

    Reads the PID file written by `codebotd start`, verifies the daemon
    process is alive, and sends `STOP` over the loopback control port.
    Falls back to SIGTERM on POSIX if the control port is unreachable.
    """
    from . import ipc
    running, info = ipc.is_daemon_running()
    if not running:
        click.echo("codebotd is not running.", err=True)
        sys.exit(1)

    click.echo(f"codebotd pid={info['pid']} control_port={info['control_port']} "
               f"sim_port={info['sim_port']}")

    ok, msg = ipc.send_stop(timeout=timeout)
    if ok:
        click.echo(f"  ✓ {msg}")
        return
    click.echo(f"  ! {msg}", err=True)
    # Fallback: SIGTERM on POSIX
    if sys.platform != "win32":
        click.echo("  Falling back to SIGTERM…")
        try:
            os.kill(info["pid"], 15)  # SIGTERM
            click.echo("  ✓ SIGTERM sent")
            return
        except (ProcessLookupError, PermissionError, OSError) as e:
            click.echo(f"  ! SIGTERM failed: {e}", err=True)
    sys.exit(1)


@cli.command()
def status():
    """Show codebotd status (PID, control port, sim port, USB device)."""
    from . import ipc
    running, info = ipc.is_daemon_running()
    if not running:
        click.echo("codebotd is not running.")
        sys.exit(1)

    click.echo(f"  PID:          {info['pid']}")
    click.echo(f"  Control port: {info['control_port']}")
    click.echo(f"  Sim port:     {info['sim_port']}")
    # USB device scan: live check
    try:
        from .transport.usb import UsbTransport
        dev = UsbTransport().find()
        if dev is None:
            click.echo("  USB device:   not found (sim-only mode)")
        else:
            click.echo(f"  USB device:   bus={dev.bus} addr={dev.address} "
                       f"serial={dev.serial or 'n/a'}")
    except Exception as e:
        click.echo(f"  USB device:   probe failed: {e}", err=True)


@cli.command()
def screenshot():
    """Capture and save a screenshot from the device."""
    click.echo("screenshot: TODO - render current page to PNG")


@cli.command()
@click.argument("text")
def send(text: str):
    """Send a keystroke string via HID (for testing)."""
    click.echo(f"send: TODO - convert {text!r} to HID and send")


@cli.command()
@click.option("--yes", "-y", is_flag=True,
              help="非交互：所有提示都取默认值，不问任何问题（CI / 脚本用）。")
def teardown(yes: bool):
    """codebotd setup 的反向操作：清掉所有 setup 装下的东西。

    卸载范围（全部默认执行）：
      * driver — udev 规则（系统 + 用户两级）/ WinUSB INF 绑定（需管理员）
      * service — systemd user unit / launchd LaunchAgent / Task Scheduler 任务
      * Claude Code — settings.json 里的 statusLine + hooks 块（备份后删除）

    不动：codebot 包本身（pip uninstall codebot 单独跑）、daemon 状态文件、
    ~/.code-bot/。

    幂等：再跑一次不会出错（已经清掉的就 no-op）。默认在 TTY 下走
    交互向导；传 ``--yes`` 跳过确认。
    """
    from . import _ui
    from .teardown import run_teardown

    auto = not yes and sys.stdin.isatty() and sys.stdout.isatty()
    _ui.bind(interactive=auto)

    sys.exit(run_teardown())


@cli.command()
def test_protocol():
    """Run protocol codec self-tests (v3: 1B cmd + struct, no magic/CRC)."""
    from .protocol import Frame, build_ping, build_set_brightness, build_clear, build_draw_rect_begin

    # Round-trip 1: PING (empty payload, 1B)
    f1 = build_ping()
    assert len(f1.encode()) == 1
    parsed = Frame.decode(f1.encode())
    assert parsed.cmd == f1.cmd, f"cmd mismatch: {parsed.cmd} != {f1.cmd}"
    assert parsed.payload == f1.payload

    # Round-trip 2: SET_BRIGHTNESS (1B payload, 2B total)
    f2 = build_set_brightness(80)
    assert len(f2.encode()) == 2
    parsed = Frame.decode(f2.encode())
    assert parsed.cmd == f2.cmd
    assert parsed.payload == f2.payload

    # Round-trip 3: CLEAR (2B payload, 3B total)
    f3 = build_clear(0xF800)
    assert len(f3.encode()) == 3
    parsed = Frame.decode(f3.encode())
    assert parsed.cmd == f3.cmd
    assert parsed.payload == f3.payload

    # Round-trip 4: DRAW_RECT_BEGIN (8B payload, 9B total)
    f4 = build_draw_rect_begin(0, 0, 320, 172)
    assert len(f4.encode()) == 9
    parsed = Frame.decode(f4.encode())
    assert parsed.cmd == f4.cmd
    assert parsed.payload == f4.payload

    # All frames fit in single USB packet (≤ 64B)
    for f in (f1, f2, f3, f4):
        assert len(f.encode()) <= 64, f"frame too large: {len(f.encode())}"

    click.echo("Protocol codec self-test (v3): PASS")


def main():
    """Entry point for setuptools console_scripts."""
    cli()


if __name__ == "__main__":
    main()

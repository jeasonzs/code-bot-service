"""Command-line interface for codebotd."""

import sys
import click
from .daemon import run_daemon
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
def start(foreground: bool, config: str | None, verbose: bool):
    """Start the codebotd daemon."""
    run_daemon(foreground=foreground, config_path=config, verbose=verbose)


@cli.command()
def stop():
    """Stop the running codebotd daemon."""
    click.echo("stop: TODO - send SIGTERM to PID file")


@cli.command()
def status():
    """Show codebotd status."""
    click.echo("status: TODO - check PID file and USB device")


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

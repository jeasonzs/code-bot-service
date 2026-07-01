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
    """Run protocol codec self-tests."""
    from .protocol import Frame, FrameStream, build_ping, build_set_brightness

    # Round-trip 1: PING (empty payload)
    f1 = build_ping()
    parsed = next(FrameStream().feed(f1.encode()))
    assert parsed.cmd == f1.cmd, f"cmd mismatch: {parsed.cmd} != {f1.cmd}"
    assert parsed.payload == f1.payload

    # Round-trip 2: SET_BRIGHTNESS (1-byte payload)
    f2 = build_set_brightness(80)
    parsed = next(FrameStream().feed(f2.encode()))
    assert parsed.cmd == f2.cmd
    assert parsed.payload == f2.payload

    # Round-trip 3: feed with leading garbage (verifies magic-byte resync)
    stream = FrameStream()
    parsed = list(stream.feed(b"\xff\x00\x42" + f1.encode() + b"\xaa"))
    assert len(parsed) == 1, f"expected 1 frame, got {len(parsed)}"
    assert parsed[0].cmd == f1.cmd

    click.echo("Protocol codec self-test: PASS")


def main():
    """Entry point for setuptools console_scripts."""
    cli()


if __name__ == "__main__":
    main()

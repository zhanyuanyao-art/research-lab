"""`rl launchd …`: optional login autostart via a user LaunchAgent. Never runs launchctl itself;
it prints the commands to run."""
from __future__ import annotations

import os
import plistlib
import shutil
import socket
from pathlib import Path

import typer

from ..config import HOST, PORT, get_paths

app = typer.Typer(no_args_is_help=True, help="Autostart `rl serve` at login (macOS LaunchAgent)")

LABEL = "ai.research-lab"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def uv_path() -> str | None:
    found = shutil.which("uv")
    if found:
        return str(Path(found).resolve())
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


def build_plist(uv: str) -> dict:
    paths = get_paths()
    logs = paths.logs
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "rl", "serve", "--no-open"],
        "WorkingDirectory": str(paths.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "serve.out.log"),
        "StandardErrorPath": str(logs / "serve.err.log"),
        # launchd's PATH lacks ~/.local/bin and Homebrew; git and uv are needed at runtime.
        "EnvironmentVariables": {"PATH": os.pathsep.join([str(Path(uv).parent), "/opt/homebrew/bin",
                                                          "/usr/local/bin", "/usr/bin", "/bin"])},
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


@app.command()
def install() -> None:
    """Write the LaunchAgent plist and print the launchctl command that loads it."""
    uv = uv_path()
    if not uv:
        typer.echo("uv not found on PATH or at ~/.local/bin/uv", err=True)
        raise typer.Exit(1)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    get_paths().logs.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump(build_plist(uv), fh)
    typer.echo(f"wrote {target}")
    typer.echo("load it now (and at every login) with:")
    typer.echo(f"  launchctl bootstrap {_domain()} {target}")
    typer.echo("if a previous version is loaded, first run:")
    typer.echo(f"  launchctl bootout {_domain()}/{LABEL}")


@app.command()
def uninstall() -> None:
    """Remove the plist and print the launchctl command that unloads it."""
    target = plist_path()
    typer.echo("unload the running agent with:")
    typer.echo(f"  launchctl bootout {_domain()}/{LABEL}")
    if target.exists():
        target.unlink()
        typer.echo(f"removed {target}")
    else:
        typer.echo(f"{target} not present")


@app.command()
def status() -> None:
    """Show whether the plist exists, what it runs, and whether the port answers."""
    target = plist_path()
    if target.exists():
        with target.open("rb") as fh:
            data = plistlib.load(fh)
        typer.echo(f"plist {target}")
        typer.echo(f"  runs    {' '.join(data.get('ProgramArguments', []))}")
        typer.echo(f"  in      {data.get('WorkingDirectory')}")
        typer.echo(f"  logs    {data.get('StandardErrorPath')}")
    else:
        typer.echo(f"no plist at {target} (run `uv run rl launchd install`)")
    with socket.socket() as s:
        s.settimeout(0.5)
        up = s.connect_ex((HOST, PORT)) == 0
    typer.echo(f"http://{HOST}:{PORT}/ {'answering' if up else 'not answering'}")
    typer.echo(f"launchd's view: launchctl print {_domain()}/{LABEL}")

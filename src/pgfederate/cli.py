"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__, config as config_module
from .engine import Engine
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pgfederate", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("serve", "run the server"), ("check", "load every source once and list the tables")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("-c", "--config", default="pgfederate.yaml")
        command.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = config_module.load(args.config)
    except (OSError, config_module.ConfigError) as error:
        print(f"pgfederate: {error}", file=sys.stderr)
        return 2
    if args.command == "check":
        return check(config)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    return 0


def check(config: config_module.Config) -> int:
    engine = Engine(config)
    failed = False
    try:
        engine.start()
        for status in engine.status.values():
            line = f"{status.name} ({status.type}): {status.status}, {status.tables} tables"
            if status.error:
                failed = True
                line += f", error: {status.error}"
            print(line)
            for table in sorted(engine.exposed[status.name]):
                print(f"  {status.name}.{table}")
    except Exception as error:
        print(f"pgfederate: {error}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

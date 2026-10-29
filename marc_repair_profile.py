#!/usr/bin/env python3
"""Create run profiles for marc_repair.py.

A profile is a JSON file in the profile directory (profiles/ next to
marc_repair.py, or --profile-dir) holding a set of marc_repair.py options, so
they don't have to be retyped on every run. marc_repair.py loads it
automatically (see its --profile / --no-profile / --list-profiles).

  marc_repair_profile.py init NAME
      interactive wizard: walks through every option the profile can hold,
      showing its help text; Enter keeps the default, q finishes early

  marc_repair_profile.py save NAME [marc_repair.py switches...]
      builds a profile from the switches given, e.g.
      marc_repair_profile.py save sierra --source-system sierra --remap-999-to-945

Only options that differ from marc_repair.py's built-in defaults are stored,
so a profile stays short and picks up any default that changes later. Input
and output paths are per-run and never part of a profile.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Callable

import marc_repair

_HELP_CHARS = 400


class _Finish(Exception):
    """The user typed q: stop the wizard and keep the answers so far."""


def profile_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not stem:
        raise ValueError(f"{name!r} cannot be used as a profile name")
    return stem + ".json"


def options_from_switches(parser: argparse.ArgumentParser, switches: list[str]) -> dict:
    """The profile-eligible options that `switches` changes from the defaults."""
    args = vars(parser.parse_args(switches))
    defaults = vars(parser.parse_args([]))
    return {
        dest: args[dest]
        for dest in marc_repair.profile_option_actions(parser)
        if args[dest] != defaults[dest]
    }


def write_profile(
    directory: str, name: str, description: str, options: dict,
    force: bool = False, ask: Callable[[str], str] | None = None,
) -> str | None:
    """Write NAME.json under directory; returns its path, or None if the user declined to
    overwrite an existing file."""
    ask = ask or input
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, profile_filename(name))
    if os.path.exists(path) and not force:
        try:
            answer = ask(f"{path} exists -- overwrite? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            return None
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "description": description, "options": options}, f, indent=2)
        f.write("\n")
    return path


def _short_help(action: argparse.Action) -> str:
    text = " ".join((action.help or "").split())
    return text if len(text) <= _HELP_CHARS else text[:_HELP_CHARS].rstrip() + " ..."


def _ask(prompt: str, ask: Callable[[str], str]) -> str:
    answer = ask(prompt).strip()
    if answer.lower() == "q":
        raise _Finish
    return answer


def _parse_bool(text: str) -> bool | None:
    lowered = text.lower()
    if lowered in ("y", "yes", "true", "t", "1", "on"):
        return True
    if lowered in ("n", "no", "false", "f", "0", "off"):
        return False
    return None


def _ask_option(action, default, ask, say):
    """The profile value for one option, or marc_repair.py's default if the user keeps it."""
    kind = marc_repair.profile_value_kind(action)
    if kind == "bool" and not isinstance(action, argparse.BooleanOptionalAction):
        # store_true / store_false: the only question is whether to pass the flag
        answer = _ask(f"  Pass {action.option_strings[0]}? [y/N]: ", ask)
        return action.const if _parse_bool(answer) else default
    if kind == "bool":
        while True:
            answer = _ask(f"  true/false [default {default}]: ", ask)
            if not answer:
                return default
            value = _parse_bool(answer)
            if value is not None:
                return value
            say("  please answer true or false")
    if kind == "list":
        items: list[str] = []
        while True:
            answer = _ask("  value (blank line to finish): ", ask)
            if not answer:
                return items or default
            items.append(answer)
    while True:
        shown = "none" if default is None else default
        choices = f" {{{', '.join(map(str, action.choices))}}}" if action.choices else ""
        answer = _ask(f"  value{choices} [default {shown}]: ", ask)
        if not answer:
            return default
        if kind == "int":
            try:
                return int(answer)
            except ValueError:
                say("  please enter a whole number")
                continue
        if action.choices and answer not in action.choices:
            say(f"  must be one of {', '.join(map(str, action.choices))}")
            continue
        return answer


def run_wizard(
    parser: argparse.ArgumentParser,
    ask: Callable[[str], str] | None = None,
    say: Callable[[str], None] = print,
) -> dict:
    """Walk through every profile option; returns those the user changed from the defaults."""
    ask = ask or input
    defaults = vars(parser.parse_args([]))
    options: dict = {}
    say("For each option press Enter to keep the default, or q to finish now.")
    try:
        for dest, action in marc_repair.profile_option_actions(parser).items():
            say(f"\n{' / '.join(action.option_strings)}\n  {_short_help(action)}")
            value = _ask_option(action, defaults[dest], ask, say)
            if value != defaults[dest]:
                options[dest] = value
    except _Finish:
        pass
    return options


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("name", help="profile name; the file is NAME.json")
    common.add_argument(
        "--description", default=None, help="one-line description shown in the menu"
    )
    common.add_argument(
        "--profile-dir", metavar="DIR",
        help=f"directory to write to (default: {marc_repair.DEFAULT_PROFILE_DIR})",
    )
    common.add_argument(
        "--force", action="store_true", help="overwrite an existing profile without asking"
    )
    tool = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = tool.add_subparsers(dest="command", required=True)
    sub.add_parser("init", parents=[common], help="build a profile interactively")
    sub.add_parser(
        "save", parents=[common],
        help="build a profile from marc_repair.py switches given after the name",
    )
    args, rest = tool.parse_known_args(argv)
    parser = marc_repair.build_parser()

    if args.command == "init":
        if rest:
            tool.error(f"unrecognized arguments: {' '.join(rest)}")
        try:
            options = run_wizard(parser)
            description = args.description
            if description is None:
                description = input("\nOne-line description for the profile menu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled -- nothing written", file=sys.stderr)
            return 1
    else:
        description = args.description or ""
        try:
            options = options_from_switches(parser, rest)
        except SystemExit:
            return 2

    # an explicitly named directory is created if needed (marc_repair.py's own fallback to
    # profiles/ is for reading, where a missing directory means "no profiles")
    directory = args.profile_dir or marc_repair.DEFAULT_PROFILE_DIR
    try:
        marc_repair.validate_profile_options(parser, options, args.name)
        path = write_profile(directory, args.name, description, options, force=args.force)
    except (marc_repair.ProfileError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if path is None:
        print("not overwritten -- nothing written", file=sys.stderr)
        return 1
    print(f"wrote profile {args.name!r} ({len(options)} option(s)) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

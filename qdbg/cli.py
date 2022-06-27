#!/usr/bin/python3

import logging
import subprocess
import sys
import webbrowser
from urllib.parse import quote_plus
from typing import List


class QdbgError(Exception):
    logging.error("A qdbg error occurred")


def main(args: List[str]) -> None:
    """Run a command"""

    if len(args) < 1:
        logging.error("Qdbg requires a command")
        raise QdbgError

    try:
        child_proc = subprocess.run(args=args, check=False, capture_output=True)

        # On non-zero return code, raise subprocess.CalledProcessError
        child_proc.check_returncode()

        # On success, flush output to stdout
        print(child_proc.stdout.decode().rstrip(), flush=True)

    except subprocess.CalledProcessError:
        search_url = get_search_url(cmd=args[0], stderr=child_proc.stderr.decode())
        webbrowser.open_new_tab(url=search_url)
        exit(1)

    except FileNotFoundError as e:
        logging.error(f"Command not found: {e}")
        sys.exit(127)

    except Exception as e:
        raise QdbgError(e)


def parse_traceback(stderr: str, from_bottom: bool = True) -> str:
    """Return first non-empty line of the string stderr"""
    spl = stderr.split("\n")

    for line in reversed(spl) if from_bottom else iter(spl):
        if not line:
            continue
        return line

    return ""


def get_search_url(cmd: str, stderr: str) -> str:
    """Get safe search url"""
    err = parse_traceback(stderr=stderr)
    search_query = quote_plus(f"{cmd} {err}")
    return f"https://you.com/search?q={search_query}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARN)

    if len(sys.argv) < 2:
        logging.error("Qdbg requires a command")
        raise QdbgError

    main(sys.argv[1:])

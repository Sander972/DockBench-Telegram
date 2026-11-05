"""Telegram forum topics -> `claude -p` in DockBench dev containers. Stdlib only, see README.md. Entry point: the code is in package tgbot/."""
import sys

from tgbot import app

if __name__ == "__main__":
    if "--check" in sys.argv:
        from tgbot import check
        check.self_check()
    else:
        app.main()

import sys

if sys.version_info < (3, 11):
    sys.exit(f"timak needs Python 3.11 or later (this is {sys.version.split()[0]}); try /opt/homebrew/bin/python3")

from .cli import main  # noqa: E402

sys.exit(main())

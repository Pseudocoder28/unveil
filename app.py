"""Vercel entry point.

Vercel looks for a Flask object named `app` in a file like this one. It runs unveil's
web app in hosted mode: each scan is handled in a single request and every uploaded
file is deleted before the response is sent. To run unveil on your own computer, use
`unveil web` instead (see README).
"""

import os
import sys
from pathlib import Path

# Import the package from src/ whether or not the host installed it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

os.environ.setdefault("UNVEIL_HOSTED", "1")
os.environ.setdefault("UNVEIL_WORKERS", "1")         # serverless functions get a single vCPU
os.environ.setdefault("UNVEIL_HOME", "/tmp/unveil")  # never write to $HOME on a server

from unveil.web import create_app  # noqa: E402

app = create_app(hosted=True)

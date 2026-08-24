import os
import tempfile

# Must happen before `app.config` is imported anywhere (it reads env vars at
# module import time) -- conftest.py is collected first by pytest.
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["UAS_INTERNAL_TOKEN"] = "test-secret"

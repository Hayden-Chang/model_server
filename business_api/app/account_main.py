import logging

from .account_api import create_account_api
from .account_backend import AccountAPISettings

# uvicorn configures only its own loggers, so the app's INFO-level diagnostics
# never reached the container log: a billing_verify comparison line added to
# diagnose a failed purchase was invisible in production. The worker already
# sets the same level.
logging.basicConfig(level=logging.INFO)

app = create_account_api(AccountAPISettings())

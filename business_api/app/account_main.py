from .account_api import create_account_api
from .account_backend import AccountAPISettings

app = create_account_api(AccountAPISettings())

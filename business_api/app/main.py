from .factory import create_app
from .settings import get_settings


app = create_app(get_settings())


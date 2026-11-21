import atexit
from .app import Bridge, create_app

bridge = Bridge()
atexit.register(bridge.shutdown)
app = create_app(bridge)

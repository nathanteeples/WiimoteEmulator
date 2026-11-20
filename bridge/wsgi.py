import atexit
from .app import Bridge, create_app

bridge = Bridge()
atexit.register(bridge.stop)
app = create_app(bridge)

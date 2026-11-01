import sys

from .config import Error
from .server import supervise

try:
    sys.exit(supervise())
except Error as error:
    print(error, file=sys.stderr)
    sys.exit(1)

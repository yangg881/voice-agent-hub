import os
import sys
import traceback
from pathlib import Path

# Add project root to sys.path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

try:
    from app.main import app
except BaseException as e:
    err_msg = traceback.format_exc()

    async def app(scope, receive, send):
        if scope['type'] == 'http':
            await send({
                'type': 'http.response.start',
                'status': 200,
                'headers': [[b'content-type', b'text/plain; charset=utf-8']],
            })
            await send({
                'type': 'http.response.body',
                'body': f"DIAGNOSTIC STARTUP ERROR:\n{err_msg}".encode('utf-8'),
            })

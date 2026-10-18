import logging
import os
import uvicorn
from .main import create_app

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
# One worker: session refresh rotation and login nonces are process-local.
uvicorn.run(create_app(), host='0.0.0.0', port=int(os.getenv('PORT', '8080')), access_log=False, workers=1, proxy_headers=False)

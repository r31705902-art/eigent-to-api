"""Initialize a root-owned PaaS volume, then permanently drop privileges."""
import os
from pathlib import Path
import runpy

path = Path(os.getenv('AUTH_FILE', '/data/session/auth.json'))
if path.is_symlink() or path.parent.is_symlink():
    raise RuntimeError('Session directory and file must not be symlinks')
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
if os.geteuid() == 0:
    os.chown(path.parent, 10001, 10001)
    os.chmod(path.parent, 0o700)
    if path.exists():
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o600)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
runpy.run_module('app', run_name='__main__')

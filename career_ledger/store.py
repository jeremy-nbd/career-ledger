"""Atomic JSON persistence with thread and process exclusion."""
import json
import os
import threading
from pathlib import Path
from .domain import empty_state


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.file = self.directory / 'ledger.json'
        self.lock = threading.RLock()
        self.process_lock = None

    def acquire_process_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process_lock = open(self.directory / '.server.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.process_lock.write(b'0')
                self.process_lock.flush()
                self.process_lock.seek(0)
                msvcrt.locking(self.process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.close()
            raise RuntimeError('该数据目录已有 Python 服务运行，请先停止旧服务') from None

    def close(self):
        if self.process_lock:
            self.process_lock.close()
            self.process_lock = None

    def read(self):
        with self.lock:
            try:
                state = json.loads(self.file.read_text(encoding='utf-8'))
            except FileNotFoundError:
                return empty_state()
            if not isinstance(state, dict) or state.get('version') != 1:
                raise ValueError('Unsupported ledger')
            state.setdefault('weeklyReports', [])
            if any(not isinstance(state.get(key), list) for key in ('sources', 'cards', 'runs', 'weeklyReports')):
                raise ValueError('Damaged ledger')
            return state

    def update(self, operation):
        with self.lock:
            state = self.read()
            result = operation(state)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.directory / 'ledger.json.tmp'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.file)
            return result

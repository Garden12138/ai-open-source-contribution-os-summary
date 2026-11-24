"""Read bounded UTF-8 workspace files without following symlinks."""

import os
import stat
import sys


def read_workspace_file(root: str, path: str) -> tuple[int, str]:
    parts = path.split('/')
    if any(part.casefold() in {'', '.', '..', '.git'} for part in parts):
        return 1, ''
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        directory = os.open(root, flags | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = child
            with os.fdopen(os.open(parts[-1], flags, dir_fd=directory), 'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    return 1, ''
                data = file.read(1024 * 1024 + 1)
        finally:
            os.close(directory)
    except OSError:
        return 1, ''
    if len(data) > 1024 * 1024 or b'\x00' in data:
        return 2, ''
    try:
        return 0, data.decode('utf-8')
    except UnicodeError:
        return 2, ''


if __name__ == '__main__':
    status, text = read_workspace_file('.', sys.argv[1])
    sys.stdout.buffer.write(text.encode('utf-8'))
    raise SystemExit(status)

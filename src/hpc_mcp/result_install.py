"""Install complete result files atomically without following destination symlinks."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat
import uuid


@contextmanager
def directory(path, allow_existing=True):
    """Traverse/create directories using pinned fds and reject symlink components."""
    path = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    try:
        for index, name in enumerate(path.parts[1:], 1):
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                if index == len(path.parts) - 1 and not allow_existing:
                    raise ValueError('destination exists; refusing to overwrite concurrent results')
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def install(staging, target, files, overwrite, inplace=False):
    """Per-file atomic installation; a failure may leave earlier files installed."""
    with directory(target, allow_existing=inplace or overwrite == 'merge') as root:
        for item in files:
            parts = Path(item['path']).parts
            parent = os.dup(root)
            temporary = '.hpc-mcp-install-' + uuid.uuid4().hex
            try:
                for name in parts[:-1]:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                    os.close(parent)
                    parent = child
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600,
                                     dir_fd=parent)
                with os.fdopen(descriptor, 'w+b') as out:
                    with (Path(staging) / item['path']).open('rb') as source:
                        shutil.copyfileobj(source, out)
                        metadata = os.fstat(source.fileno())
                    out.flush()
                    out.seek(0)
                    if hashlib.file_digest(out, 'sha256').hexdigest() != item['sha256']:
                        raise ValueError('staged result changed during installation')
                    os.fchmod(out.fileno(), stat.S_IMODE(metadata.st_mode))
                    os.utime(out.fileno(), ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                    os.fsync(out.fileno())
                if overwrite == 'merge':
                    # Replacing a symlink replaces the link itself, never its referent.
                    os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    try:
                        os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent,
                                follow_symlinks=False)
                    except FileExistsError as exc:
                        raise ValueError(f'output exists: {item["path"]}; refusing to overwrite') from exc
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)

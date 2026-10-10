"""Machine-readable application definitions, pinned to reviewed profile versions."""

import json
import os
import re
import tempfile

from .onboarding_store import digest


def configuration_file(history, profile):
    profile_id = profile['profile_id']
    if not re.fullmatch(r'p_[a-f0-9]{32}', profile_id):
        raise ValueError('invalid stored profile ID')
    root = history.root / 'profiles'
    if root.is_symlink():
        raise ValueError('profile configuration directory cannot be a symlink')
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / (profile_id + '.json')
    if path.is_symlink():
        raise ValueError('profile configuration cannot be a symlink')
    if not path.exists():
        # Also exports pre-existing database profiles without relocating their history.
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix='.profile-')
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(profile['definition'], stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            # An exclusive link publishes the completed definition without replacing another writer.
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    if not path.is_file() or path.is_symlink():
        raise ValueError('profile configuration must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('profile configuration exceeds 1 MiB')
    try:
        definition = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('profile configuration is not valid JSON') from exc
    if digest(definition) != digest(profile['definition']):
        raise ValueError('profile configuration changed; restore the historical definition and migrate using an application handler')
    return path, definition

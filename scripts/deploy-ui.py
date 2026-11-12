#!/usr/bin/env python3
"""Install a clean UI build while preserving chunks used by open tabs."""
import argparse
import os
from pathlib import Path
import re
import shutil
import tempfile


def deploy(build: Path, target: Path) -> int:
    build, target = build.resolve(), target.resolve()
    entry = build / 'index.html'
    if build == target or not entry.is_file():
        raise ValueError('Use a separate build folder with index.html.')
    assets = re.findall(r'(?:src|href)=["\'](/chat/assets/[^"\']+)["\']', entry.read_text())
    if not assets or any(not (build / url.removeprefix('/chat/')).is_file() for url in assets):
        raise ValueError('The entry page must reference files present in the build.')
    target.mkdir(parents=True, exist_ok=True)
    # Stage on the destination filesystem so each replacement is atomic.
    with tempfile.TemporaryDirectory(prefix='.ui-deploy-', dir=target.parent) as folder:
        stage = Path(folder)
        files = [path for path in build.rglob('*') if path.is_file()]
        for source in files:
            relative = source.relative_to(build)
            staged = stage / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
        # Retain prior chunks. Publish the entry page only after its assets exist.
        for source in sorted(files, key=lambda path: path == entry):
            relative = source.relative_to(build)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / relative, destination)
    (target / '.live-ui').touch(exist_ok=True)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    try:
        count = deploy(args.build, args.target)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Deployment failed: {error}\n')
    print(f'Installed {count} files; prior chunks retained.')


if __name__ == '__main__':
    main()

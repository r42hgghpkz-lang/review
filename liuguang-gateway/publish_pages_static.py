#!/usr/bin/env python3
"""Copy the public Pages tree and leave the gateway, secrets, and answers out.

GitHub Pages for this repo publishes the branch root. Do not merge the
gateway into that root. Publish the directory this script writes.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT_FILES = (
    'index.html',
    'log.html',
    'pipeline.html',
    'seat-kpi.html',
    'shenming.html',
    'robots.txt',
    '.nojekyll',
)
ROOT_DIRS = ('c',)
LIUGUANG_FILES = (
    'index.html',
    'questions.json',
    'gateway.json',
    'README.md',
)
FORBIDDEN_PARTS = (
    'liuguang-gateway',
    'answers',
    'idempotency',
    'receipts',
    'sessions',
)


def publish(repo: str, out: str) -> list[str]:
    repo = os.path.abspath(repo)
    out = os.path.abspath(out)
    if os.path.exists(out):
        raise SystemExit('output directory already exists')
    written: list[str] = []
    os.makedirs(out, 0o755)

    def copy_file(rel: str) -> None:
        src = os.path.join(repo, rel)
        dst = os.path.join(out, rel)
        if not os.path.isfile(src):
            raise SystemExit(f'missing public file: {rel}')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        written.append(rel)

    for name in ROOT_FILES:
        copy_file(name)
    for directory in ROOT_DIRS:
        src_dir = os.path.join(repo, directory)
        if not os.path.isdir(src_dir):
            raise SystemExit(f'missing public directory: {directory}')
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if d not in FORBIDDEN_PARTS and not d.startswith('.')]
            for filename in filenames:
                rel = os.path.relpath(os.path.join(dirpath, filename), repo)
                if filename.startswith('.'):
                    continue
                copy_file(rel)
    lug = os.path.join(out, 'liuguang')
    os.makedirs(lug, 0o755)
    for name in LIUGUANG_FILES:
        copy_file(os.path.join('liuguang', name))
    for dirpath, dirnames, filenames in os.walk(out):
        for part in dirnames + filenames:
            if part in FORBIDDEN_PARTS or part.endswith('.py'):
                raise SystemExit(f'publish output contains forbidden name: {part}')
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    files = publish(args.repo, args.out)
    sys.stdout.write(f'published {len(files)} files\n')


if __name__ == '__main__':
    main()

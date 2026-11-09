#!/usr/bin/env python3
"""Read Liuguang receipts from the private package. No network listener."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys

import store

HERE = os.path.dirname(os.path.abspath(__file__))


def _read_secret(path: str) -> str:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SystemExit('secret file must be a regular file')
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit('secret file mode must be 0600')
    with open(path, encoding='utf-8') as fh:
        value = fh.read().strip('\n')
    if value.endswith('\r'):
        value = value[:-1]
    return value


def _questions() -> dict[str, str]:
    path = os.path.join(HERE, 'questions.json')
    if not os.path.exists(path):
        return {}
    doc = json.loads(open(path, encoding='utf-8').read())
    out = {}
    for sec in doc:
        for item in sec.get('items') or []:
            out[item['id']] = item.get('text') or ''
    return out


def _check_perms(root: str) -> list[str]:
    problems = []
    if stat.S_IMODE(os.stat(root).st_mode) != 0o700:
        problems.append('root mode is not 0700')
    for dirpath, dirnames, filenames in os.walk(root):
        if stat.S_IMODE(os.stat(dirpath).st_mode) != 0o700:
            problems.append('directory mode is not 0700')
        for name in filenames:
            mode = stat.S_IMODE(os.stat(os.path.join(dirpath, name)).st_mode)
            if mode != 0o600:
                problems.append('file mode is not 0600')
    return problems


def _load(root: str, secret: str) -> store.Package:
    iters = store.kdf_iters_from_env()
    resolved = store.assert_safe_root(root, HERE)
    return store.Package(resolved, secret, iters, create_kdf=False)


def main() -> None:
    parser = argparse.ArgumentParser(description='Read Liuguang private receipts for Lilith')
    parser.add_argument('--root', required=True)
    parser.add_argument('--password-file', required=True, help='data key file (defaults to the submit secret when one file is used)')
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('--id', dest='receipt_id')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check-perms', action='store_true')
    args = parser.parse_args()
    if args.check_perms:
        problems = _check_perms(os.path.abspath(args.root))
        if problems:
            sys.stderr.write('perms failed\n')
            raise SystemExit(1)
        if not args.latest and not args.receipt_id and not args.list and not args.json:
            sys.stdout.write('perms ok\n')
            return
    secret = _read_secret(args.password_file)
    try:
        package = _load(args.root, secret)
    except store.StoreError as exc:
        sys.stderr.write(f'readback refused: {exc}\n')
        raise SystemExit(2)
    if args.list and not args.latest and not args.receipt_id:
        ids = package.list_ids()
        if args.json:
            sys.stdout.write(json.dumps({'ids': ids}, ensure_ascii=False) + '\n')
        else:
            for rid in ids:
                sys.stdout.write(rid + '\n')
        return
    try:
        if args.receipt_id:
            records = [package.read_receipt(args.receipt_id)]
        elif args.latest:
            rid = package.latest_id()
            if not rid:
                sys.stderr.write('no receipt\n')
                raise SystemExit(1)
            records = [package.read_receipt(rid)]
        else:
            records = [package.read_receipt(rid) for rid in package.list_ids()]
    except (OSError, ValueError, KeyError, store.StoreError):
        sys.stderr.write('decrypt failed or receipt missing\n')
        raise SystemExit(1)
    if args.json:
        sys.stdout.write(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
        return
    questions = _questions()
    if not records:
        sys.stdout.write('no receipt\n')
        return
    for rec in records:
        flag = ' synthetic' if rec.get('synthetic') else ''
        sys.stdout.write(f"receipt {rec.get('receiptId')}{flag}\n")
        sys.stdout.write(f"storedAt {rec.get('storedAt')}\n")
        sys.stdout.write(f"digest {rec.get('digest')}\n")
        sys.stdout.write(f"origin {rec.get('origin')}\n")
        sys.stdout.write(f"counts {json.dumps(rec.get('counts') or {}, ensure_ascii=False)}\n")
        answers = rec.get('answers') or {}
        for qid in sorted(answers, key=lambda s: [int(p) for p in s.split('.')]):
            ans = answers[qid]
            prompt = questions.get(qid, '')
            sys.stdout.write(f"\n{qid} [{ans.get('status')}] {prompt}\n")
            text = ans.get('text') or ''
            if text:
                sys.stdout.write(text + '\n')
            if ans.get('parkUntil'):
                sys.stdout.write(f"parkUntil {ans['parkUntil']}\n")
        sys.stdout.write('\n')


if __name__ == '__main__':
    main()

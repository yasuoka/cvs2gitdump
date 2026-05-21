#!/usr/bin/env python3

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HEAD_REF = 'refs/heads/master'
ATTIC_KEYWORD_PATH = re.compile(
    rb'\$(Header|Source): ([^$\n]*)/Attic/([^/$\n]+,v[^$\n]*)\$')


def run(cmd, cwd=None, stdout=subprocess.PIPE, check=True):
    proc = subprocess.run(cmd, cwd=cwd, stdout=stdout,
                          stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc)
    return proc


class CommandError(Exception):
    def __init__(self, cmd, proc):
        self.cmd = cmd
        self.proc = proc
        super().__init__(self.message())

    def message(self):
        stderr = self.proc.stderr.decode('utf-8', 'replace').strip()
        stdout = (self.proc.stdout or b'').decode('utf-8', 'replace').strip()
        msg = 'command failed (%d): %s' % (
            self.proc.returncode, ' '.join(self.cmd))
        if stderr:
            msg += '\n' + stderr
        if stdout:
            msg += '\n' + stdout[:2000]
        return msg


def parse_args():
    parser = argparse.ArgumentParser(
        description='Compare CVS checkout trees against Git refs.')
    parser.add_argument('cvsroot')
    parser.add_argument('module')
    parser.add_argument('git_dir')
    parser.add_argument('--symbol-glob', default='OPENBSD_*')
    parser.add_argument('--keep-work', action='store_true')
    parser.add_argument('--no-head', action='store_true')
    return parser.parse_args()


def list_refs(git_dir, symbol_glob):
    proc = run([
        'git', '--git-dir=' + git_dir, 'for-each-ref',
        '--format=%(refname)', 'refs/heads', 'refs/tags'])

    refs = []
    for ref in proc.stdout.decode().splitlines():
        short = ref.rsplit('/', 1)[-1]
        if fnmatch.fnmatchcase(short, symbol_glob):
            refs.append(ref)
    return sorted(refs)


def checkout_head(cvsroot, module, cvs_dir):
    shutil.rmtree(cvs_dir, ignore_errors=True)
    cvs_dir.parent.mkdir(parents=True, exist_ok=True)
    run([
        'cvs', '-Q', '-R', '-d', cvsroot, 'checkout', '-P',
        '-d', str(cvs_dir), module])
    return 'checkout-head'


def checkout_symbol(cvsroot, module, symbol, cvs_dir):
    shutil.rmtree(cvs_dir, ignore_errors=True)
    cvs_dir.parent.mkdir(parents=True, exist_ok=True)
    checkout_cmd = [
        'cvs', '-Q', '-R', '-d', cvsroot, 'checkout', '-P',
        '-r', symbol, '-d', str(cvs_dir), module]
    proc = run(checkout_cmd, check=False)
    if proc.returncode == 0:
        return 'checkout'

    # CVS can run out of memory during large tagged checkouts while leaving
    # behind a partial tree that a follow-up update can complete.
    if cvs_dir.exists():
        update_cmd = ['cvs', '-Q', '-R', 'update', '-dP', '-r', symbol]
        update_proc = run(update_cmd, cwd=cvs_dir, check=False)
        if update_proc.returncode == 0:
            return 'checkout-update-after-checkout-error'

    raise CommandError(checkout_cmd, proc)


def tree_files(path):
    files = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d != 'CVS']
        for filename in filenames:
            if filename.startswith('.#'):
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(path).as_posix()
            files.append(rel)
    return sorted(files)


def git_files(git_dir, ref):
    proc = run([
        'git', '--git-dir=' + git_dir, 'ls-tree', '-r', '-z',
        '--name-only', ref])
    files = [item.decode('utf-8', 'surrogateescape')
             for item in proc.stdout.split(b'\0') if item]
    return sorted(files)


def extract_git(git_dir, ref, git_dir_out):
    shutil.rmtree(git_dir_out, ignore_errors=True)
    git_dir_out.mkdir(parents=True)

    archive = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, 'archive', '--format=tar', ref],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.Popen(
        ['tar', '-xf', '-', '-C', str(git_dir_out)],
        stdin=archive.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    archive.stdout.close()
    _, tar_err = tar.communicate()
    archive_err = archive.stderr.read()
    archive_rc = archive.wait()
    if archive_rc != 0 or tar.returncode != 0:
        raise RuntimeError(
            'git archive/tar failed arch=%d tar=%d\n%s\n%s' % (
                archive_rc, tar.returncode,
                archive_err.decode('utf-8', 'replace'),
                tar_err.decode('utf-8', 'replace')))


def file_bytes(path):
    if os.path.islink(path):
        data = b'SYMLINK\0' + os.readlink(path).encode(
            'utf-8', 'surrogateescape')
    else:
        with open(path, 'rb') as file:
            data = file.read()
    return data


def normalize_attic_keyword_paths(data):
    # CVS expands path-bearing keywords from the physical RCS file path.
    # Files stored in Attic therefore get /Attic/ in $Header$/$Source$,
    # while Git keeps the same file at its repository path without Attic.
    return ATTIC_KEYWORD_PATH.sub(rb'$\1: \2/\3$', data)


def same_content(cvs_data, git_data):
    if cvs_data == git_data:
        return True

    return normalize_attic_keyword_paths(cvs_data) == \
        normalize_attic_keyword_paths(git_data)


def content_diffs(cvs_dir, git_dir_out, files):
    diffs = []
    for rel in files:
        try:
            cvs_data = file_bytes(cvs_dir / rel)
            git_data = file_bytes(git_dir_out / rel)
        except OSError as err:
            diffs.append('%s (%s)' % (rel, err))
        else:
            if not same_content(cvs_data, git_data):
                diffs.append(rel)
    return diffs


def print_mismatches(ref, kind, paths):
    for path in paths:
        print('%s %s %s' % (ref, kind, path), flush=True)


def compare_ref(args, ref, symbol, cvs_dir, git_dir_out):
    try:
        if symbol is None:
            checkout_head(args.cvsroot, args.module, cvs_dir)
        else:
            checkout_symbol(args.cvsroot, args.module, symbol, cvs_dir)

        cvs_paths = tree_files(cvs_dir)
        git_paths = git_files(args.git_dir, ref)
        if cvs_paths != git_paths:
            cvs_path_set = set(cvs_paths)
            git_path_set = set(git_paths)
            print_mismatches(ref, 'cvs-only',
                             sorted(cvs_path_set - git_path_set))
            print_mismatches(ref, 'git-only',
                             sorted(git_path_set - cvs_path_set))
            return False

        extract_git(args.git_dir, ref, git_dir_out)
        diffs = content_diffs(cvs_dir, git_dir_out, cvs_paths)
        if diffs:
            print_mismatches(ref, 'diff', diffs)
            return False

        return True
    except Exception as err:
        print('%s error %s' % (ref, err), flush=True)
        return False


def main():
    args = parse_args()
    refs = list_refs(args.git_dir, args.symbol_glob)

    work_dir = Path(tempfile.mkdtemp(prefix='verify-cvs-git-'))
    cvs_dir = work_dir / 'cvs'
    git_dir_out = work_dir / 'git'
    failures = 0

    try:
        if not args.no_head:
            if not compare_ref(args, HEAD_REF, None, cvs_dir, git_dir_out):
                failures += 1

        for ref in refs:
            symbol = ref.rsplit('/', 1)[-1]
            if not compare_ref(args, ref, symbol, cvs_dir, git_dir_out):
                failures += 1

        return 1 if failures else 0
    finally:
        if args.keep_work:
            print('kept work dir %s' % work_dir)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())

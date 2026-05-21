#!/usr/bin/env python3

import argparse
import hashlib
import subprocess
from collections import defaultdict
from pathlib import Path
import re


HEAD_REF = 'refs/heads/master'
REVISION = re.compile(r'^\d+(?:\.\d+)+$')


class Record:
    def __init__(self, module, ref, kind, path):
        self.module = module
        self.ref = ref
        self.kind = kind
        self.path = path
        self.symbol = symbol_from_ref(ref)


class ModuleInput:
    def __init__(self, module, output_file, git_dir):
        self.module = module
        self.output_file = output_file
        self.git_dir = git_dir


class SelectedInfo:
    def __init__(self, cls, revision=None, blob=None, error=None):
        self.cls = cls
        self.revision = revision
        self.blob = blob
        self.error = error


def parse_args():
    parser = argparse.ArgumentParser(
        description='Classify verify-cvs-git.py mismatch output.')
    parser.add_argument('--details')
    parser.add_argument('cvsroot')
    parser.add_argument(
        'inputs', nargs='+',
        help='module output-file git-dir triples')
    args = parser.parse_args()
    if len(args.inputs) % 3 != 0:
        parser.error('inputs must be module output-file git-dir triples')
    return args


def symbol_from_ref(ref):
    if ref == HEAD_REF:
        return None
    if ref.startswith('refs/heads/') or ref.startswith('refs/tags/'):
        return ref.rsplit('/', 1)[-1]
    return None


def read_records(module, filename):
    records = []
    with open(filename, 'r', encoding='utf-8', errors='surrogateescape') as file:
        for line in file:
            ref, kind, path = line.rstrip('\n').split(' ', 2)
            records.append(Record(module, ref, kind, path))
    return records


def rcs_path(cvsroot, module, path):
    base = Path(cvsroot) / module
    normal = base / (path + ',v')
    if normal.exists():
        return normal

    parts = path.split('/')
    attic = base.joinpath(*parts[:-1], 'Attic', parts[-1] + ',v')
    if attic.exists():
        return attic

    return None


def is_branch_symbol_revision(revision):
    parts = revision.split('.')
    return '.0.' in revision or len(parts) % 2 == 1


def normalize_branch_revision(revision):
    parts = revision.split('.')
    if '.0.' in revision:
        zero = parts.index('0')
        return '.'.join(parts[:zero] + parts[zero + 1:])
    return revision


def branchpoint_revision(branch):
    parts = branch.split('.')
    return '.'.join(parts[:-1])


def revision_key(revision):
    return tuple(int(part) for part in revision.split('.'))


def git_blob_hash(data):
    header = ('blob %d\0' % len(data)).encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def co_revision(path, revision):
    proc = subprocess.run(
        ['co', '-q', '-p', '-r' + revision, str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode('utf-8', 'replace').strip())
    return proc.stdout


class RcsInfo:
    def __init__(self):
        self.symbols = {}
        self.states = {}


def parse_rcs(path, needed):
    info = RcsInfo()
    in_symbols = False
    revision = None

    with open(path, 'rb') as file:
        for raw_line in file:
            line = raw_line.decode('latin-1').strip()
            if line == 'desc':
                break

            if not in_symbols:
                if line.startswith('symbols'):
                    in_symbols = True
                    line = line[len('symbols'):].strip()

            if in_symbols:
                if line.startswith('locks;'):
                    in_symbols = False
                else:
                    if line.endswith(';'):
                        line = line[:-1]
                    for item in line.split():
                        if ':' not in item:
                            continue
                        name, value = item.split(':', 1)
                        if name in needed:
                            info.symbols[name] = value
                    continue

            if REVISION.match(line):
                revision = line
                continue

            if revision is None:
                continue

            state = re.search(r'\bstate\s+([^;]+);', line)
            if state:
                info.states[revision] = state.group(1)

    return info


def selected_branch_revision(info, branch):
    parts = branch.split('.')
    candidates = []
    prefix = branch + '.'
    for revision in info.states:
        revision_parts = revision.split('.')
        if revision.startswith(prefix) and len(revision_parts) == len(parts) + 1:
            candidates.append(revision)

    if candidates:
        return max(candidates, key=revision_key)

    return branchpoint_revision(branch)


def selected_revision_info(rcs, info, symbol):
    revision = info.symbols.get(symbol)
    if revision is None:
        return SelectedInfo('no-cvs-symbol')

    if is_branch_symbol_revision(revision):
        branch = normalize_branch_revision(revision)
        selected = selected_branch_revision(info, branch)
        state = info.states.get(selected)
        if state == 'dead':
            return SelectedInfo('cvs-branch-selects-dead-revision', selected)
        if state is None:
            return SelectedInfo('cvs-branch-selects-unknown-revision', selected)
        cls = 'cvs-branch-selects-live-revision'
        try:
            blob = git_blob_hash(co_revision(rcs, selected))
            return SelectedInfo(cls, selected, blob)
        except RuntimeError as err:
            return SelectedInfo(cls, selected, error=str(err))

    state = info.states.get(revision)
    if state == 'dead':
        return SelectedInfo('cvs-tag-selects-dead-revision', revision)
    if state is None:
        return SelectedInfo('cvs-tag-selects-unknown-revision', revision)
    cls = 'cvs-tag-selects-live-revision'
    try:
        blob = git_blob_hash(co_revision(rcs, revision))
        return SelectedInfo(cls, revision, blob)
    except RuntimeError as err:
        return SelectedInfo(cls, revision, error=str(err))


def collect_symbol_info(cvsroot, records):
    needed = defaultdict(set)
    for record in records:
        if record.symbol is not None and record.kind in ('git-only',
                                                         'cvs-only', 'diff'):
            needed[(record.module, record.path)].add(record.symbol)

    info = {}
    for (module, path), symbols in needed.items():
        rcs = rcs_path(cvsroot, module, path)
        if rcs is None:
            for symbol in symbols:
                info[(module, path, symbol)] = SelectedInfo('no-rcs-file')
            continue

        rcs_info = parse_rcs(rcs, symbols)
        for symbol in symbols:
            info[(module, path, symbol)] = selected_revision_info(
                rcs, rcs_info, symbol)

    return info


def collect_wanted_blobs(records, symbol_info):
    wanted = defaultdict(set)
    for record in records:
        if record.symbol is None or record.kind == 'error':
            continue
        info = symbol_info[(record.module, record.path, record.symbol)]
        if info.blob is not None:
            wanted[record.module].add(info.blob)
    return wanted


def git_blob_locations(git_dir, wanted):
    locations = defaultdict(set)
    if not wanted:
        return locations

    proc = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, 'rev-list', '--objects', '--all'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', errors='surrogateescape')
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip('\n')
        if not line:
            continue
        parts = line.split(' ', 1)
        obj = parts[0]
        if obj not in wanted:
            continue
        path = parts[1] if len(parts) == 2 else ''
        locations[obj].add(path)

    stderr = proc.stderr.read() if proc.stderr is not None else ''
    if proc.wait() != 0:
        raise RuntimeError(stderr)

    return locations


def collect_git_locations(module_inputs, wanted):
    locations = {}
    by_module = {item.module: item for item in module_inputs}
    for module, blobs in wanted.items():
        locations[module] = git_blob_locations(by_module[module].git_dir, blobs)
    return locations


def git_blob_at_path(git_dir, ref, path):
    proc = subprocess.run(
        ['git', '--git-dir=' + git_dir, 'ls-tree', '-z', ref, '--', path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode('utf-8', 'replace').strip())
    if not proc.stdout:
        return None
    meta = proc.stdout.split(b'\t', 1)[0].decode('ascii')
    return meta.split()[2]


def collect_ref_blobs(records, module_inputs):
    blobs = {}
    by_module = {item.module: item for item in module_inputs}
    for record in records:
        if record.kind != 'diff':
            continue
        key = (record.module, record.ref, record.path)
        if key in blobs:
            continue
        blobs[key] = git_blob_at_path(
            by_module[record.module].git_dir, record.ref, record.path)
    return blobs


def git_cause(record, info, locations, ref_blobs):
    if info.error is not None:
        return 'cvs_selected_revision_checkout_failed'

    if info.cls == 'no-cvs-symbol':
        return 'git_ref_contains_path_but_rcs_has_no_requested_symbol'
    if info.cls == 'no-rcs-file':
        return 'git_ref_contains_path_but_no_rcs_file_was_found'

    if info.cls.endswith('dead-revision'):
        return 'git_ref_contains_path_but_cvs_selects_dead_revision'
    if info.cls.endswith('unknown-revision'):
        return 'cvs_symbol_points_to_revision_missing_from_rcs_admin'

    if info.blob is None:
        return 'cvs_selected_live_revision_blob_was_not_computed'

    paths = locations.get(record.module, {}).get(info.blob, set())
    same_path = record.path in paths
    if record.kind == 'cvs-only':
        if same_path:
            return 'cvs_live_blob_reachable_at_same_path_but_not_this_ref'
        if paths:
            return 'cvs_live_blob_reachable_only_at_other_path'
        return 'cvs_live_blob_not_reachable_from_any_git_ref'

    if record.kind == 'diff':
        git_blob = ref_blobs.get((record.module, record.ref, record.path))
        if git_blob == info.blob:
            return 'numeric_cvs_revision_blob_matches_git_symbolic_checkout_differs'
        if same_path:
            return 'git_ref_has_different_blob_cvs_blob_reachable_at_same_path'
        if paths:
            return 'git_ref_has_different_blob_cvs_blob_reachable_elsewhere'
        return 'git_ref_has_different_blob_cvs_blob_not_reachable'

    if record.kind == 'git-only':
        return 'git_ref_contains_path_but_cvs_does_not_select_live_file'

    return 'unclassified'


def classify(record, symbol_info, git_locations, ref_blobs):
    if record.kind == 'error':
        if record.symbol is None:
            return 'head-error', 'verifier_reported_error'
        return 'error', 'verifier_reported_error'

    if record.symbol is None:
        return 'head-' + record.kind, 'head_comparison_has_no_cvs_symbol'

    info = symbol_info[(record.module, record.path, record.symbol)]
    return record.kind + '/' + info.cls, git_cause(
        record, info, git_locations, ref_blobs)


def summarize(records, symbol_info, git_locations, ref_blobs):
    summary = {}
    for record in records:
        cls, cause = classify(record, symbol_info, git_locations, ref_blobs)
        key = (record.module, cls, cause)
        if key not in summary:
            summary[key] = {
                'lines': 0,
                'refs': set(),
                'paths': set(),
            }
        item = summary[key]
        item['lines'] += 1
        item['refs'].add(record.ref)
        item['paths'].add(record.path)
    return summary


def print_summary(summary):
    print('module\tclass\tcause\tlines\trefs\tpaths')
    for (module, cls, cause), item in sorted(
            summary.items(), key=lambda x: (x[0][0], -x[1]['lines'], x[0][1],
                                            x[0][2])):
        print('%s\t%s\t%s\t%d\t%d\t%d' % (
            module, cls, cause, item['lines'], len(item['refs']),
            len(item['paths'])))


def print_details(filename, records, symbol_info, git_locations, ref_blobs):
    with open(filename, 'w', encoding='utf-8', errors='surrogateescape') as file:
        print('module\tref\tkind\tpath\tclass\tcause\tcvs_revision\t'
              'cvs_blob\tgit_blob', file=file)
        for record in records:
            cls, cause = classify(record, symbol_info, git_locations, ref_blobs)
            info = None
            if record.symbol is not None and record.kind != 'error':
                info = symbol_info[(record.module, record.path, record.symbol)]
            git_blob = ref_blobs.get((record.module, record.ref, record.path),
                                     '')
            print('%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' % (
                record.module, record.ref, record.kind, record.path, cls,
                cause, info.revision if info is not None and
                info.revision is not None else '',
                info.blob if info is not None and info.blob is not None else '',
                git_blob if git_blob is not None else ''),
                file=file)


def main():
    args = parse_args()
    records = []

    module_inputs = [
        ModuleInput(*values)
        for values in zip(args.inputs[0::3], args.inputs[1::3],
                          args.inputs[2::3])
    ]

    for item in module_inputs:
        records.extend(read_records(item.module, item.output_file))

    symbol_info = collect_symbol_info(args.cvsroot, records)
    git_locations = collect_git_locations(
        module_inputs, collect_wanted_blobs(records, symbol_info))
    ref_blobs = collect_ref_blobs(records, module_inputs)
    print_summary(summarize(records, symbol_info, git_locations, ref_blobs))
    if args.details is not None:
        print_details(args.details, records, symbol_info, git_locations,
                      ref_blobs)


if __name__ == '__main__':
    main()

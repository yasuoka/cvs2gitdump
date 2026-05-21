#!/usr/local/bin/python

#
# Copyright (c) 2012 YASUOKA Masahiko <yasuoka@yasuoka.net>
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Usage
#
#   First import:
#   % git init --bare /git/openbsd.git
#   % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src \
#       > openbsd.dump
#   % git --git-dir /git/openbsd.git fast-import < openbsd.dump
#
#   Periodic import:
#   % sudo cvsync
#   % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src \
#       /git/openbsd.git > openbsd2.dump
#   % git --git-dir /git/openbsd.git fast-import < openbsd2.dump
#

import getopt
import os
import re
import subprocess
import sys
import time
import rcsparse

CHANGESET_FUZZ_SEC = 300


def usage():
    print('usage: cvs2gitdump [-aAh] [-z fuzz] [-e email_domain]\n'
          '\t[-E log_encodings]\n'
          '\t[-k rcs_keywords] [-b branch] [-m module] [-l last_revision]\n'
          '\tcvsroot [git_dir]', file=sys.stderr)


def main():
    email_domain = None
    do_incremental = False
    git_tip = None
    git_branch = 'master'
    dump_all = False
    log_encoding = 'utf-8,iso-8859-1'
    rcs = RcsKeywords()
    modules = []
    last_revision = None
    fuzzsec = CHANGESET_FUZZ_SEC
    convert_all = False
    existing_branches = set()
    existing_refs = set()
    existing_ref_commits = dict()
    branch_sources = dict()
    tag_sources = dict()
    branch_tips = dict()

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'aAb:hm:z:e:E:k:t:l:')
        for opt, v in opts:
            if opt == '-z':
                fuzzsec = int(v)
            elif opt == '-e':
                email_domain = v
            elif opt == '-a':
                dump_all = True
            elif opt == '-b':
                git_branch = v
            elif opt == '-E':
                log_encoding = v
            elif opt == '-k':
                rcs.add_id_keyword(v)
            elif opt == '-m':
                if v == '.git':
                    print('Cannot handle the path named \'.git\'',
                          file=sys.stderr)
                    sys.exit(1)
                modules.append(v)
            elif opt == '-l':
                last_revision = v
            elif opt == '-A':
                convert_all = True
            elif opt == '-h':
                usage()
                sys.exit(1)
    except getopt.GetoptError as msg:
        print(msg, file=sys.stderr)
        usage()
        sys.exit(1)

    if len(args) == 0 or len(args) > 2:
        usage()
        sys.exit(1)

    log_encodings = log_encoding.split(',')

    cvsroot = args[0]
    while cvsroot[-1] == '/':
        cvsroot = cvsroot[:-1]

    if len(args) == 2:
        do_incremental = True
        git = subprocess.Popen(
            ['git', '--git-dir=' + args[1], '-c',
             'i18n.logOutputEncoding=UTF-8', 'log', '--max-count', '1',
             '--date=raw', '--format=%ae%n%ad%n%H', git_branch],
            encoding='utf-8', stdout=subprocess.PIPE)
        outs = git.stdout.readlines()
        git.wait()
        if git.returncode != 0:
            print("Couldn't exec git", file=sys.stderr)
            sys.exit(git.returncode)
        git_tip = outs[2].strip()

        if last_revision is not None:
            git = subprocess.Popen(
                ['git', '--git-dir=' + args[1], '-c',
                 'i18n.logOutputEncoding=UTF-8', 'log', '--max-count', '1',
                 '--date=raw', '--format=%ae%n%ad%n%H', last_revision],
                encoding='utf-8', stdout=subprocess.PIPE)
            outs = git.stdout.readlines()
            git.wait()
            if git.returncode != 0:
                print("Coundn't exec git", file=sys.stderr)
                sys.exit(git.returncode)
        last_author = outs[0].strip()
        last_ctime = float(outs[1].split()[0])

        # strip off the domain part from the last author since cvs doesn't have
        # the domain part.
        if do_incremental and email_domain is not None and \
                last_author.lower().endswith(('@' + email_domain).lower()):
            last_author = last_author[:-1 * (1 + len(email_domain))]

        git = subprocess.Popen(
            ['git', '--git-dir=' + args[1], 'for-each-ref',
             '--format=%(refname)%00%(objectname)', 'refs/heads',
             'refs/tags'],
            encoding='utf-8', stdout=subprocess.PIPE)
        for line in git.stdout.readlines():
            ref, commit = line.rstrip('\n').split('\x00', 1)
            existing_refs.add(ref)
            existing_ref_commits[ref] = commit
        git.wait()
        if git.returncode != 0:
            print("Couldn't exec git", file=sys.stderr)
            sys.exit(git.returncode)
        existing_branches = set([
            r[len('refs/heads/'):] for r in existing_refs
            if r.startswith('refs/heads/')
        ])

    cvs = CvsConv(cvsroot, rcs, not do_incremental, fuzzsec, convert_all)
    print('** walk cvs tree', file=sys.stderr)
    if len(modules) == 0:
        cvs.walk()
    else:
        for module in modules:
            cvs.walk(module)

    changesets = sorted(cvs.changesets)
    nchangesets = len(changesets)
    if convert_all:
        cvs.select_branch_bases(changesets)
        cvs.prepare_tags()
    print('** cvs has %d changeset' % (nchangesets), file=sys.stderr)

    if nchangesets <= 0:
        sys.exit(0)

    if do_incremental and convert_all:
        commits = git_commit_map(args[1], git_branch, email_domain)
        branch_tips = git_branch_tips(
            args[1], existing_branches, changesets, email_domain)
        branch_sources = git_branch_sources(
            args[1], git_branch, cvs, existing_branches, commits,
            log_encodings)
        tag_sources = git_tag_sources(
            args[1], git_branch, cvs, existing_refs, existing_branches,
            existing_ref_commits, commits, log_encodings, last_ctime,
            branch_tips)
        cvs.filter_external_source_adjustments(
            args[1], branch_sources, tag_sources)
        cvs.add_missing_git_ref_adjustments(
            args[1], branch_sources, tag_sources)
    if convert_all:
        cvs.drop_ref_roots()

    if not dump_all:
        # don't use last 10 minutes for safety
        max_time_max = changesets[-1].max_time - 600
    else:
        max_time_max = changesets[-1].max_time

    found_last_revision = False
    markseq = cvs.markseq
    extags = set()
    reset_tags = set()
    commit_marks = dict()
    initialized_branches = set(existing_branches)
    found_branches = set()
    started_branches = set()
    adjusted_branches = set()
    branches_by_base = refs_by_base(cvs.branch_bases)
    tags_by_base = refs_by_base(cvs.tags)
    for k in changesets:
        if do_incremental and is_cvs_branch(k.branch):
            if k.branch in branch_tips and k.branch not in found_branches:
                if changeset_matches_git_tip(
                        k, branch_tips[k.branch], log_encodings):
                    found_branches.add(k.branch)
                continue
        elif do_incremental and not found_last_revision:
            if k.min_time == last_ctime and k.author == last_author:
                found_last_revision = True
            for tag in k.tags:
                extags.add(tag)
            continue
        if k.max_time > max_time_max:
            break

        branch_source = None
        if do_incremental and is_cvs_branch(k.branch):
            if k.branch in branch_tips:
                branch_source = branch_tips[k.branch]['commit']
            else:
                branch_source = cvs_branch_source(
                    k.branch, cvs, commit_marks, branch_sources)
            if branch_source is None:
                continue

        if is_cvs_branch(k.branch):
            if branch_source is None:
                branch_source = cvs_branch_source(
                    k.branch, cvs, commit_marks, branch_sources)
            if branch_source is None:
                continue
            reset_branch(k.branch, branch_source, initialized_branches)
            if k.branch in cvs.branch_adjustments and \
                    k.branch not in adjusted_branches:
                markseq = emit_partial_pick_commits(
                    'refs/heads/%s' % k.branch,
                    cvs.branch_adjustments[k.branch], do_incremental, rcs,
                    markseq, log_encodings, email_domain)
                adjusted_branches.add(k.branch)

        commit_revs = k.revs
        marks = {}
        for f in commit_revs:
            markseq, mark = mark_file_revision(
                f, do_incremental, rcs, markseq)
            if mark is not None:
                marks[f] = mark

        output('commit ' + git_ref(k.branch, git_branch))
        markseq = markseq + 1
        output('mark :%d' % (markseq))
        commit_marks[k] = markseq
        commit_data = changeset_commit_data(
            k, log_encodings, email_domain)
        output(b'author ' + commit_data['author'])
        output(b'committer ' + commit_data['committer'])

        output('data', len(commit_data['log']))
        output(commit_data['log'], end='')
        if do_incremental and k.branch in branch_tips and \
                k.branch not in started_branches:
            output('from', branch_tips[k.branch]['commit'])
            started_branches.add(k.branch)
        elif do_incremental and git_tip is not None and \
                not is_cvs_branch(k.branch):
            output('from', git_tip)
            git_tip = None

        for f in commit_revs:
            if f.state == 'dead':
                output('D', f.fn)
            else:
                output('M %o :%d %s' % (f.mode(), marks[f], f.fn))
        output('')
        for tag in k.tags:
            if tag in extags or tag in reset_tags or \
                    tag in cvs.collapsed_symbols or \
                    tag in cvs.tag_adjustments:
                continue
            source = ':%d' % commit_marks[k]
            reset_tag(tag, source)
        for tag in tags_by_base.get(k, []):
            if tag in extags or tag in reset_tags or \
                    tag in cvs.collapsed_symbols or \
                    tag not in cvs.tag_adjustments:
                continue
            source = ':%d' % commit_marks[k]
            reset_tag(tag, source)
            markseq = emit_partial_pick_commits(
                'refs/tags/%s' % tag, cvs.tag_adjustments[tag],
                do_incremental, rcs, markseq, log_encodings, email_domain)
            reset_tags.add(tag)
        for branch in branches_by_base.get(k, []):
            source = cvs_branch_source(
                branch, cvs, commit_marks, branch_sources)
            if source is not None:
                reset_branch(branch, source, initialized_branches)
                if branch in cvs.branch_adjustments and \
                        branch not in adjusted_branches:
                    markseq = emit_partial_pick_commits(
                        'refs/heads/%s' % branch,
                        cvs.branch_adjustments[branch], do_incremental, rcs,
                        markseq, log_encodings, email_domain)
                    adjusted_branches.add(branch)

    if do_incremental:
        for tag, source in list(tag_sources.items()):
            if tag in cvs.collapsed_symbols:
                continue
            reset_tag(tag, source)
            if tag in cvs.tag_adjustments:
                markseq = emit_partial_pick_commits(
                    'refs/tags/%s' % tag, cvs.tag_adjustments[tag],
                    do_incremental, rcs, markseq, log_encodings,
                    email_domain)
            reset_tags.add(tag)
        for branch, source in list(branch_sources.items()):
            reset_branch(branch, source, initialized_branches)
            if branch in cvs.branch_adjustments and \
                    branch not in adjusted_branches:
                markseq = emit_partial_pick_commits(
                    'refs/heads/%s' % branch, cvs.branch_adjustments[branch],
                    do_incremental, rcs, markseq, log_encodings,
                    email_domain)
                adjusted_branches.add(branch)

    if do_incremental and not found_last_revision:
        raise Exception('could not find the last revision')
    if do_incremental:
        missing = set(branch_tips) - found_branches
        if len(missing) > 0:
            raise Exception('could not find the last revision for %s' %
                            ', '.join(sorted(missing)))

    print('** dumped', file=sys.stderr)


#
# Encode by UTF-8 always for string objects since encoding for git-fast-import
# is UTF-8.  Also write without conversion for a bytes object (file bodies
# might be various encodings)
#
def output(*args, end='\n'):
    if len(args) == 0:
        pass
    elif len(args) > 1 or isinstance(args[0], str):
        lines = ' '.join(
            [arg if isinstance(arg, str) else str(arg) for arg in args])
        sys.stdout.buffer.write(lines.encode('utf-8'))
    else:
        sys.stdout.buffer.write(args[0])
    if len(end) > 0:
        sys.stdout.buffer.write(end.encode('utf-8'))


class FileRevision:
    __slots__ = (
        'path', 'fn', 'rev', 'state', 'markseq', 'git_mode',
        'deleted_later')

    def __init__(self, path, fn, rev, state, markseq, deleted_later=False):
        self.path = path
        self.fn = fn
        self.rev = rev
        self.state = state
        self.markseq = markseq
        self.git_mode = None
        self.deleted_later = deleted_later

    def mode(self):
        if self.git_mode is None:
            self.git_mode = 0o100755 if os.access(self.path, os.X_OK) \
                else 0o100644
        return self.git_mode


class ChangeSetKey:
    __slots__ = (
        'branch', 'author', 'min_time', 'max_time', 'commitid', 'fuzzsec',
        'revs', 'tags', 'log_hash')

    def __init__(self, branch, author, timestamp, log, commitid, fuzzsec):
        self.branch = branch
        self.author = author
        self.min_time = timestamp
        self.max_time = timestamp
        self.commitid = commitid
        self.fuzzsec = fuzzsec
        self.revs = []
        self.tags = []
        self.log_hash = 0
        h = 0
        for c in log:
            h = 31 * h + c
        self.log_hash = h

    def __lt__(self, other):
        return self._cmp(other) < 0

    def __gt__(self, other):
        return self._cmp(other) > 0

    def __eq__(self, other):
        return self._cmp(other) == 0

    def __le__(self, other):
        return self._cmp(other) <= 0

    def __ge__(self, other):
        return self._cmp(other) >= 0

    def __ne__(self, other):
        return self._cmp(other) != 0

    def _cmp(self, anon):
        # compare by the commitid
        cid = _cmp2(self.commitid, anon.commitid)
        if cid == 0 and self.commitid is not None:
            # both have commitid and they are same
            return 0

        # compare by the time
        ma = anon.min_time - self.max_time
        mi = self.min_time - anon.max_time
        ct = self.min_time - anon.min_time
        if ma > self.fuzzsec or mi > self.fuzzsec:
            return ct

        if cid != 0:
            # only one has the commitid, this means different commit
            return cid if ct == 0 else ct

        # compare by log, branch and author
        c = _cmp2(self.log_hash, anon.log_hash)
        if c == 0:
            c = _cmp2(self.branch, anon.branch)
        if c == 0:
            c = _cmp2(self.author, anon.author)
        if c == 0:
            return 0

        return ct if ct != 0 else c

    def merge(self, anot):
        self.max_time = max(self.max_time, anot.max_time)
        self.min_time = min(self.min_time, anot.min_time)
        self.revs.extend(anot.revs)

    def __hash__(self):
        return hash(self.branch + '/' + self.author) * 31 + self.log_hash

    def put_file(self, path, fn, rev, state, markseq, deleted_later=False):
        f = FileRevision(path, fn, rev, state, markseq, deleted_later)
        self.revs.append(f)
        return f


def _cmp2(a, b):
    _a = a is not None
    _b = b is not None
    return (a > b) - (a < b) if _a and _b else (_a > _b) - (_a < _b)


class CvsConv:
    def __init__(self, cvsroot, rcs, dumpfile, fuzzsec, convert_all=False):
        self.cvsroot = cvsroot
        self.rcs = rcs
        self.changesets = dict()
        self.dumpfile = dumpfile
        self.markseq = 0
        self.tags = dict()
        self.tag_roots = dict()
        self.branch_bases = dict()
        self.branch_roots = dict()
        self.missing_branch_roots = dict()
        self.branch_adjustments = dict()
        self.tag_adjustments = dict()
        self.missing_tag_roots = dict()
        self.collapsed_symbols = set()
        self.branch_symbols = set()
        self.tag_symbols = set()
        self.fuzzsec = fuzzsec
        self.convert_all = convert_all
        self.normal_rcs_paths = set()

    def walk(self, module=None):
        p = [self.cvsroot]
        if module is not None:
            p.append(module)
        path = os.path.join(*p)

        rcs_paths = []
        for root, dirs, files in os.walk(path):
            if '.git' in dirs:
                print('Ignore %s: cannot handle the path named \'.git\'' % (
                      root + os.sep + '.git'), file=sys.stderr)
                dirs.remove('.git')
            if '.git' in files:
                print('Ignore %s: cannot handle the path named \'.git\'' % (
                      root + os.sep + '.git'), file=sys.stderr)
                files.remove('.git')
            for f in files:
                if not f[-2:] == ',v':
                    continue
                rcs_path = root + os.sep + f
                rcs_paths.append(rcs_path)
                if not is_attic_path(rcs_path):
                    self.normal_rcs_paths.add(file_path(self.cvsroot, rcs_path))

        for path in rcs_paths:
            self.parse_file(path)

        self.collapsed_symbols = self.branch_symbols & self.tag_symbols

    def parse_file(self, path):
        rtags = dict()
        rbranches = dict()
        rcsfile = rcsparse.rcsfile(path)
        fn = file_path(self.cvsroot, path)
        shadowed_attic = is_attic_path(path) and fn in self.normal_rcs_paths
        branches = {'1': 'HEAD', '1.1.1': 'VENDOR'}
        symbols = [] if shadowed_attic else list(rcsfile.symbols.items())
        for k, v in symbols:
            if self.convert_all and k != 'HEAD':
                if is_cvs_branch_symbol(v):
                    self.branch_symbols.add(k)
                else:
                    self.tag_symbols.add(k)
            r = v.split('.')
            if len(r) == 3:
                branches[v] = 'VENDOR'
            elif is_cvs_branch_symbol(v):
                branch = '.'.join(r[:-2] + r[-1:])
                branches[branch] = k
                if self.convert_all:
                    b = '.'.join(r[:-2])
                    if b not in rbranches:
                        rbranches[b] = list()
                    rbranches[b].append(k)
        if self.convert_all:
            for k, v in symbols:
                if k == 'HEAD':
                    continue
                r = v.split('.')
                if is_cvs_branch_symbol(v):
                    continue
                branch = branches.get('.'.join(r[:-1]))
                if branch is not None:
                    if v not in rtags:
                        rtags[v] = []
                    rtags[v].append(k)

        revs = rcsfile.revs.items()
        # sort by revision descending to priorize 1.1.1.1 than 1.1
        revs = sorted(revs, key=lambda a: a[1][0], reverse=True)
        # sort by time
        revs = sorted(revs, key=lambda a: a[1][1])

        dead_after_revs = set()
        seen_dead = False
        for k, v in reversed(revs):
            if seen_dead:
                dead_after_revs.add(k)
            if v[3] == 'dead':
                seen_dead = True

        def put_skipped_roots(rev, info):
            frev = FileRevision(
                path, fn, rev, info[3], 0, rev in dead_after_revs)
            self.put_branch_roots(rbranches, rev, frev, None, info[1])
            if rev in rtags:
                for tag in rtags[rev]:
                    self.put_tag_root(tag, frev, None, info[1])

        novendor = False
        have_initial_revision = False
        last_vendor_status = None
        for k, v in revs:
            r = k.split('.')
            if len(r) == 4 and r[0] == '1' and r[1] == '1' and r[2] == '1' \
                    and r[3] == '1':
                if have_initial_revision:
                    put_skipped_roots(k, v)
                    continue
                if v[3] == 'dead':
                    put_skipped_roots(k, v)
                    continue
                last_vendor_status = v[3]
                have_initial_revision = True
            elif len(r) == 4 and r[0] == '1' and r[1] == '1' and r[2] == '1':
                if novendor:
                    put_skipped_roots(k, v)
                    continue
                last_vendor_status = v[3]
            elif len(r) == 2:
                if r[0] == '1' and r[1] == '1':
                    if have_initial_revision:
                        put_skipped_roots(k, v)
                        continue
                    if v[3] == 'dead':
                        put_skipped_roots(k, v)
                        continue
                    have_initial_revision = True
                elif r[0] == '1' and r[1] != '1':
                    novendor = True
                if last_vendor_status == 'dead' and v[3] == 'dead':
                    last_vendor_status = None
                    continue
                last_vendor_status = None
            else:
                b = '.'.join(r[:-1])
                if not self.convert_all or b not in branches:
                    put_skipped_roots(k, v)
                    continue
                last_vendor_status = None

            if self.dumpfile:
                self.markseq = self.markseq + 1
                git_dump_file(path, k, self.rcs, self.markseq)

            b = '.'.join(r[:-1])
            try:
                a = ChangeSetKey(
                    branches[b], v[2], v[1], rcsfile.getlog(v[0]), v[6],
                    self.fuzzsec)
            except Exception as e:
                print('Aborted at %s %s' % (path, v[0]), file=sys.stderr)
                raise e

            frev = a.put_file(
                path, fn, k, v[3], self.markseq, k in dead_after_revs)
            while a in self.changesets:
                c = self.changesets[a]
                del self.changesets[a]
                c.merge(a)
                a = c
            self.changesets[a] = a
            if k in rtags:
                for tag in rtags[k]:
                    if tag not in self.tags or \
                            self.tags[tag].max_time < a.max_time:
                        self.tags[tag] = a
                    self.put_tag_root(tag, frev, a, a.max_time)
            self.put_branch_roots(rbranches, k, frev, a, a.max_time)

    def prepare_tags(self):
        for tag, changeset in list(self.tags.items()):
            if tag in self.collapsed_symbols:
                continue
            changeset.tags.append(tag)

    def put_branch_roots(self, rbranches, rev, frev, changeset, timestamp):
        if rev not in rbranches:
            return

        for branch in rbranches[rev]:
            item = (changeset, frev, timestamp)
            if branch not in self.branch_roots:
                self.branch_roots[branch] = []
            self.branch_roots[branch].append(item)
            if frev.state != 'dead' and \
                    (is_attic_path(frev.path) or frev.deleted_later):
                if branch not in self.missing_branch_roots:
                    self.missing_branch_roots[branch] = dict()
                self.missing_branch_roots[branch][frev.fn] = item

    def put_tag_root(self, tag, frev, changeset, timestamp):
        item = (changeset, frev, timestamp)
        if tag not in self.tag_roots:
            self.tag_roots[tag] = dict()
        self.tag_roots[tag][frev.fn] = item
        if frev.state != 'dead' and \
                (is_attic_path(frev.path) or frev.deleted_later):
            if tag not in self.missing_tag_roots:
                self.missing_tag_roots[tag] = dict()
            self.missing_tag_roots[tag][frev.fn] = item

    def select_branch_bases(self, changesets):
        first_branch_changesets = dict()
        for changeset in changesets:
            if is_cvs_branch(changeset.branch) and \
                    changeset.branch not in first_branch_changesets:
                first_branch_changesets[changeset.branch] = changeset

        roots_by_branch = dict()
        candidates_by_branch = dict()
        for branch, roots in list(self.branch_roots.items()):
            root_revs = dict()
            for item in roots:
                root_revs[item[1].fn] = item

            first = first_branch_changesets.get(branch)
            candidates = [
                item for item in roots
                if item[0] is not None
            ]
            if first is not None:
                before_first = [
                    item for item in candidates
                    if item[2] < first.min_time
                ]
                if len(before_first) > 0:
                    candidates = before_first
            if len(candidates) == 0:
                continue

            roots_by_branch[branch] = root_revs
            candidates_by_branch[branch] = list(dict.fromkeys([
                item[0] for item in candidates
            ]))

        self.score_branch_bases(changesets, roots_by_branch,
                                candidates_by_branch)
        self.branch_adjustments = self.collect_ref_adjustments(
            changesets, roots_by_branch, self.branch_bases)
        self.tag_adjustments = self.collect_ref_adjustments(
            changesets, self.tag_roots, self.tags)
        self.branch_roots = dict()

    def score_branch_bases(self, changesets, roots_by_branch,
                           candidates_by_branch):
        score_data = dict()
        scores = dict()

        for branch, candidates in list(candidates_by_branch.items()):
            roots = roots_by_branch[branch]
            scores[branch] = []
            for changeset in candidates:
                state_branch = branch_state_name(changeset.branch)
                if state_branch not in score_data:
                    score_data[state_branch] = {
                        'candidates': dict(),
                        'mismatches': dict(),
                        'paths': dict(),
                        'state': dict(),
                    }
                data = score_data[state_branch]
                if changeset not in data['candidates']:
                    data['candidates'][changeset] = []
                data['candidates'][changeset].append(branch)
                if branch in data['mismatches']:
                    continue

                mismatches = 0
                for path, item in list(roots.items()):
                    frev = item[1]
                    if path not in data['paths']:
                        data['paths'][path] = []
                    data['paths'][path].append((branch, frev))
                    if frev.state != 'dead':
                        mismatches += 1
                data['mismatches'][branch] = mismatches

        for changeset in changesets:
            state_branch = branch_state_name(changeset.branch)
            if state_branch not in score_data:
                continue

            data = score_data[state_branch]
            for frev in changeset.revs:
                if frev.fn not in data['paths']:
                    continue
                old = data['state'].get(frev.fn)
                new = None if frev.state == 'dead' else frev
                for branch, root in data['paths'][frev.fn]:
                    old_match = same_branch_root_state(old, root)
                    new_match = same_branch_root_state(new, root)
                    if old_match and not new_match:
                        data['mismatches'][branch] += 1
                    elif not old_match and new_match:
                        data['mismatches'][branch] -= 1
                if new is None:
                    data['state'].pop(frev.fn, None)
                else:
                    data['state'][frev.fn] = new

            if changeset not in data['candidates']:
                continue
            for branch in data['candidates'][changeset]:
                scores[branch].append((
                    data['mismatches'][branch], changeset))

        for branch, branch_scores in list(scores.items()):
            if len(branch_scores) == 0:
                continue
            self.branch_bases[branch] = max(
                branch_scores, key=lambda item: (-item[0], item[1].max_time)
            )[1]

    def collect_ref_adjustments(self, changesets, roots_by_ref, bases_by_ref):
        refs_by_state_branch = dict()
        adjustments = dict()

        for ref, base in list(bases_by_ref.items()):
            if ref not in roots_by_ref:
                continue
            state_branch = branch_state_name(base.branch)
            if state_branch not in refs_by_state_branch:
                refs_by_state_branch[state_branch] = {
                    'bases': dict(),
                    'paths': dict(),
                    'state': dict(),
                }
            data = refs_by_state_branch[state_branch]
            if base not in data['bases']:
                data['bases'][base] = []
            data['bases'][base].append(ref)
            for path, item in list(roots_by_ref[ref].items()):
                if path not in data['paths']:
                    data['paths'][path] = []
                data['paths'][path].append((ref, item))

        for changeset in changesets:
            state_branch = branch_state_name(changeset.branch)
            if state_branch not in refs_by_state_branch:
                continue
            data = refs_by_state_branch[state_branch]

            for frev in changeset.revs:
                if frev.fn not in data['paths']:
                    continue
                if frev.state == 'dead':
                    data['state'].pop(frev.fn, None)
                else:
                    data['state'][frev.fn] = frev

            if changeset not in data['bases']:
                continue
            for ref in data['bases'][changeset]:
                roots = roots_by_ref[ref]
                for path, item in sorted(roots.items()):
                    root = item[1]
                    current = data['state'].get(path)
                    if not same_branch_root_state(current, root):
                        if ref not in adjustments:
                            adjustments[ref] = []
                        adjustments[ref].append(item)

        return adjustments

    def add_missing_git_ref_adjustments(
            self, git_dir, branch_sources, tag_sources):
        self.add_missing_git_adjustments(
            git_dir, self.missing_branch_roots, branch_sources,
            self.branch_adjustments)
        self.add_missing_git_adjustments(
            git_dir, self.missing_tag_roots, tag_sources,
            self.tag_adjustments)

    def filter_external_source_adjustments(
            self, git_dir, branch_sources, tag_sources):
        self.filter_external_adjustments(
            git_dir, branch_sources, self.branch_adjustments)
        self.filter_external_adjustments(
            git_dir, tag_sources, self.tag_adjustments)

    def filter_external_adjustments(self, git_dir, sources, adjustments):
        refs_by_source = dict()
        for ref, source in list(sources.items()):
            if ref not in adjustments:
                continue
            if source not in refs_by_source:
                refs_by_source[source] = []
            refs_by_source[source].append(ref)

        for source, refs in sorted(refs_by_source.items()):
            paths = dict()
            for ref in refs:
                for item in adjustments[ref]:
                    frev = item[1]
                    if frev.state == 'dead':
                        continue
                    if frev.fn not in paths:
                        paths[frev.fn] = []
                    paths[frev.fn].append((ref, item))

            kept = dict()
            missing = git_missing_paths(git_dir, source, sorted(paths))
            for path in missing:
                for ref, item in paths[path]:
                    if ref not in kept:
                        kept[ref] = []
                    kept[ref].append(item)

            for ref in refs:
                if ref in kept:
                    adjustments[ref] = kept[ref]
                else:
                    adjustments.pop(ref, None)

    def add_missing_git_adjustments(
            self, git_dir, roots_by_ref, sources, adjustments):
        refs_by_source = dict()
        for ref, source in list(sources.items()):
            if ref not in roots_by_ref:
                continue
            if source not in refs_by_source:
                refs_by_source[source] = []
            refs_by_source[source].append(ref)

        for source, refs in sorted(refs_by_source.items()):
            paths = dict()
            for ref in refs:
                for item in roots_by_ref[ref].values():
                    frev = item[1]
                    if frev.state == 'dead':
                        continue
                    if frev.fn not in paths:
                        paths[frev.fn] = []
                    paths[frev.fn].append((ref, item))

            missing = git_missing_paths(git_dir, source, sorted(paths))
            for path in missing:
                for ref, item in paths[path]:
                    if self.adjustment_exists(adjustments, ref, item):
                        continue
                    if ref not in adjustments:
                        adjustments[ref] = []
                    adjustments[ref].append(item)

    def adjustment_exists(self, adjustments, ref, item):
        if ref not in adjustments:
            return False
        frev = item[1]
        for _source, existing, _timestamp in adjustments[ref]:
            if existing.fn == frev.fn and existing.rev == frev.rev and \
                    existing.state == frev.state:
                return True
        return False

    def drop_ref_roots(self):
        self.branch_roots = dict()
        self.tag_roots = dict()
        self.missing_branch_roots = dict()
        self.missing_tag_roots = dict()

def file_path(r, p):
    if r.endswith('/'):
        r = r[:-1]
    path = p[:-2]               # drop ",v"
    p = path.split('/')
    if len(p) > 0 and p[-2] == 'Attic':
        path = '/'.join(p[:-2] + [p[-1]])
    if path.startswith(r):
        path = path[len(r) + 1:]
    return path


def is_attic_path(path):
    return 'Attic' in path.split('/')


def is_cvs_branch(branch):
    return branch not in ('HEAD', 'VENDOR')


def branch_state_name(branch):
    if is_cvs_branch(branch):
        return branch
    return 'HEAD'


def same_branch_root_state(current, root):
    if root.state == 'dead':
        return current is None
    if current is None:
        return False
    return current.rev == root.rev and current.state == root.state


def git_missing_paths(git_dir, source, paths):
    if len(paths) == 0:
        return []
    git = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, 'cat-file', '--batch-check'],
        encoding='utf-8', stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdin = ''.join(['%s:%s\n' % (source, path) for path in paths])
    outs = git.communicate(stdin)[0].splitlines()
    if git.returncode != 0:
        print("Couldn't exec git", file=sys.stderr)
        sys.exit(git.returncode)

    missing = []
    for path, line in zip(paths, outs):
        parts = line.split(' ', 2)
        if len(parts) < 2 or parts[1] != 'blob':
            missing.append(path)
    return missing


def is_cvs_branch_symbol(rev):
    r = rev.split('.')
    return len(r) >= 3 and r[-2] == '0'


def git_ref(branch, git_branch):
    if is_cvs_branch(branch):
        return 'refs/heads/' + branch
    return 'refs/heads/' + git_branch


def cvs_branch_source(branch, cvs, commit_marks, branch_sources):
    if branch in branch_sources:
        return branch_sources[branch]

    base = cvs.branch_bases.get(branch)
    if base in commit_marks:
        return ':%d' % commit_marks[base]

    return None


def refs_by_base(refs):
    by_base = dict()
    for ref, base in list(refs.items()):
        if base not in by_base:
            by_base[base] = []
        by_base[base].append(ref)
    return by_base


def reset_branch(branch, source, initialized_branches):
    if branch in initialized_branches:
        return

    output('reset refs/heads/%s' % branch)
    output('from', source)
    output('')
    initialized_branches.add(branch)


def reset_tag(tag, source):
    output('reset refs/tags/%s' % tag)
    output('from', source)
    output('')


def mark_file_revision(frev, do_incremental, rcs, markseq):
    if frev.state == 'dead':
        return markseq, None
    if not do_incremental and frev.markseq != 0:
        return markseq, frev.markseq
    markseq = markseq + 1
    git_dump_file(frev.path, frev.rev, rcs, markseq)
    return markseq, markseq


def emit_partial_pick_commits(ref, adjustments, do_incremental, rcs, markseq,
                              log_encodings, email_domain):
    for source, revs in partial_pick_groups(adjustments):
        marks = {}
        for frev in revs:
            markseq, mark = mark_file_revision(
                frev, do_incremental, rcs, markseq)
            if mark is not None:
                marks[frev] = mark

        output('commit ' + ref)
        markseq = markseq + 1
        output('mark :%d' % markseq)
        commit_data = partial_pick_commit_data(
            source, revs[0], log_encodings, email_domain)
        output(b'author ' + commit_data['author'])
        output(b'committer ' + commit_data['committer'])
        output('data', len(commit_data['log']))
        output(commit_data['log'], end='')
        for frev in revs:
            if frev.state == 'dead':
                output('D', frev.fn)
            else:
                output('M %o :%d %s' % (
                    frev.mode(), marks[frev], frev.fn))
        output('')

    return markseq


def partial_pick_groups(adjustments):
    groups = []
    for source, frev, timestamp in sorted(
            adjustments,
            key=lambda item: (item[2], partial_pick_source_key(item[0]),
                              item[1].fn)):
        key = partial_pick_source_key(source)
        if len(groups) == 0 or groups[-1][0] != key:
            groups.append((key, source, []))
        groups[-1][2].append(frev)
    return [(source, revs) for _key, source, revs in groups]


def partial_pick_source_key(source):
    if source is None:
        return ''
    return '%s/%d/%d/%s' % (
        source.author, source.min_time, source.max_time, source.log_hash)


def partial_pick_commit_data(source, frev, log_encodings, email_domain):
    if source is None:
        commit_data = file_revision_commit_data(
            frev, log_encodings, email_domain)
    else:
        commit_data = changeset_commit_data(
            source, log_encodings, email_domain)

    source_name = partial_pick_source_name(source, frev)
    trailer = ('\n\n(this commit was partially cherry picked from %s)\n' %
               source_name).encode('utf-8')
    commit_data['log'] = commit_data['log'].rstrip(b'\n') + trailer
    return commit_data


def partial_pick_source_name(source, frev):
    if source is not None:
        if source.commitid is not None:
            return 'CVS commitid %s' % source.commitid
        return 'CVS changeset %s@%d' % (source.author, source.min_time)
    return 'CVS revision %s' % frev.rev


def file_revision_commit_data(frev, log_encodings, email_domain):
    rcsfile = rcsparse.rcsfile(frev.path)
    rev = rcsfile.revs[frev.rev]
    log = decode_log(rcsfile.getlog(rev[0]), log_encodings).encode(
        'utf-8', 'ignore')
    email = rev[2] if email_domain is None else rev[2] + '@' + email_domain
    author = ('%s <%s> %d +0000' % (rev[2], email, rev[1])).encode('utf-8')
    return {
        'author': author,
        'committer': author,
        'log': log,
    }


def git_tip_info(git_dir, ref, email_domain):
    git = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, '-c',
         'i18n.logOutputEncoding=UTF-8', 'log', '--max-count', '1',
         '--format=%ae%x00%ct%x00%H%x00%B', ref],
        encoding='utf-8', errors='replace', stdout=subprocess.PIPE)
    out = git.stdout.read()
    git.wait()
    if git.returncode != 0:
        print("Couldn't exec git", file=sys.stderr)
        sys.exit(git.returncode)

    parts = out.split('\x00', 3)
    if len(parts) != 4:
        raise Exception('could not read git tip for %s' % ref)

    author, timestamp, commit, log = parts
    return {
        'author': strip_email_domain(author, email_domain),
        'time': int(timestamp),
        'commit': commit,
        'log': log.rstrip('\n'),
    }


def git_branch_tips(git_dir, existing_branches, changesets, email_domain):
    branches = set()
    for changeset in changesets:
        if is_cvs_branch(changeset.branch) and \
                changeset.branch in existing_branches:
            branches.add(changeset.branch)

    tips = dict()
    for branch in sorted(branches):
        tips[branch] = git_tip_info(
            git_dir, 'refs/heads/%s' % branch, email_domain)

    return tips


def changeset_matches_git_tip(changeset, tip, log_encodings):
    if int(changeset.min_time) != tip['time'] or \
            changeset.author != tip['author']:
        return False

    return git_key_from_changeset(changeset, log_encodings) == \
        (tip['author'], tip['time'], tip['log'])


def git_branch_sources(git_dir, git_branch, cvs, existing_branches, commits,
                       log_encodings):
    sources = dict()
    missing = []

    for branch, base in list(cvs.branch_bases.items()):
        if branch in existing_branches:
            continue

        key = git_key_from_changeset(base, log_encodings)
        if key in commits:
            if len(commits[key]) > 1:
                raise Exception('ambiguous branch base for %s' % branch)
            sources[branch] = commits[key][0]
            continue

        commit = git_commit_before(git_dir, git_branch, int(base.max_time))
        if commit is not None:
            sources[branch] = commit
            continue

        missing.append(branch)

    if len(missing) > 0:
        raise Exception('could not find branch base for %s' %
                        ', '.join(sorted(missing)))

    return sources


def git_tag_sources(git_dir, git_branch, cvs, existing_refs,
                    existing_branches, existing_ref_commits, commits,
                    log_encodings, last_ctime, branch_tips):
    sources = dict()
    missing = []

    for tag, changeset in list(cvs.tags.items()):
        if tag in cvs.collapsed_symbols:
            continue
        if is_cvs_branch(changeset.branch) and \
                changeset.branch not in existing_branches:
            continue

        ref = git_ref(changeset.branch, git_branch)
        source = None
        if not is_cvs_branch(changeset.branch):
            key = git_key_from_changeset(changeset, log_encodings)
            if key in commits:
                source = git_commit_from_candidates(
                    git_dir, ref, commits[key], int(changeset.max_time))
                if source is None:
                    raise Exception('ambiguous tag source for %s' % tag)
        elif changeset.branch in branch_tips and \
                changeset.max_time > branch_tips[changeset.branch]['time']:
            continue

        if source is None and not is_cvs_branch(changeset.branch) and \
                changeset.max_time > last_ctime:
            continue

        if source is None:
            commit = git_commit_before(git_dir, ref, int(changeset.max_time))
            if commit is not None:
                source = commit

        if source is None:
            missing.append(tag)
            continue

        add_tag_source(
            git_dir, existing_refs, existing_ref_commits, sources, tag, source)

    if len(missing) > 0:
        raise Exception('could not find tag source for %s' %
                        ', '.join(sorted(missing)))

    return sources


def add_tag_source(git_dir, existing_refs, existing_ref_commits, sources, tag,
                   source):
    tag_ref = 'refs/tags/%s' % tag
    if tag_ref in existing_refs and \
            git_ref_matches(git_dir, existing_ref_commits, tag_ref, source):
        return

    sources[tag] = source


def git_ref_matches(git_dir, ref_commits, ref, source):
    return git_ref_commit(git_dir, ref_commits, ref) == \
        git_ref_commit(git_dir, ref_commits, source)


def git_ref_commit(git_dir, ref_commits, ref):
    if ref in ref_commits:
        return ref_commits[ref]
    if re.match('^[0-9a-f]{40}$', ref):
        return ref

    git = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, 'rev-parse', '--verify',
         ref + '^{commit}'],
        encoding='utf-8', stdout=subprocess.PIPE)
    outs = git.stdout.readlines()
    git.wait()
    if git.returncode != 0:
        print("Couldn't exec git", file=sys.stderr)
        sys.exit(git.returncode)
    return outs[0].strip()


def git_commit_from_candidates(git_dir, ref, candidates, timestamp):
    if len(candidates) == 1:
        return candidates[0]

    commit = git_commit_before(git_dir, ref, timestamp)
    if commit in candidates:
        return commit
    return None


def git_commit_map(git_dir, git_branch, email_domain):
    git = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, '-c',
         'i18n.logOutputEncoding=UTF-8', 'log',
         '--format=%x1e%H%x00%ae%x00%ct%x00%B', git_branch],
        encoding='utf-8', errors='replace', stdout=subprocess.PIPE)
    out = git.stdout.read()
    git.wait()
    if git.returncode != 0:
        print("Couldn't exec git", file=sys.stderr)
        sys.exit(git.returncode)

    commits = dict()
    for record in out.split('\x1e'):
        record = record.lstrip('\n')
        if len(record) == 0:
            continue

        parts = record.split('\x00', 3)
        if len(parts) != 4:
            continue

        commit, author, timestamp, log = parts
        author = strip_email_domain(author, email_domain)
        key = (author, int(timestamp), log.rstrip('\n'))
        if key not in commits:
            commits[key] = []
        commits[key].append(commit)

    return commits


def git_commit_before(git_dir, git_branch, timestamp):
    git = subprocess.Popen(
        ['git', '--git-dir=' + git_dir, 'rev-list', '-1',
         '--before=@%d' % (timestamp + 1), git_branch],
        encoding='utf-8', stdout=subprocess.PIPE)
    outs = git.stdout.readlines()
    git.wait()
    if git.returncode != 0:
        print("Couldn't exec git", file=sys.stderr)
        sys.exit(git.returncode)
    if len(outs) == 0:
        return None
    return outs[0].strip()


def strip_email_domain(author, email_domain):
    if email_domain is not None and \
            author.lower().endswith(('@' + email_domain).lower()):
        return author[:-1 * (1 + len(email_domain))]
    return author


def changeset_commit_data(changeset, log_encodings, email_domain):
    log = rcsparse.rcsfile(changeset.revs[0].path).getlog(
        changeset.revs[0].rev)
    log = decode_log(log, log_encodings).encode('utf-8', 'ignore')
    email = changeset.author if email_domain is None \
        else changeset.author + '@' + email_domain
    author = ('%s <%s> %d +0000' %
              (changeset.author, email, changeset.min_time)).encode('utf-8')
    return {
        'author': author,
        'committer': author,
        'log': log,
    }


def git_key_from_changeset(changeset, log_encodings):
    log = rcsparse.rcsfile(changeset.revs[0].path).getlog(
        changeset.revs[0].rev)
    return (changeset.author, int(changeset.min_time),
            decode_log(log, log_encodings).rstrip('\n'))


def decode_log(log, log_encodings):
    for i, e in enumerate(log_encodings):
        try:
            how = 'ignore' if i == len(log_encodings) - 1 else 'strict'
            return log.decode(e, how)
        except UnicodeError:
            pass
    return log.decode(log_encodings[-1], 'ignore')


def git_dump_file(path, k, rcs, markseq):
    try:
        cont = rcs.expand_keyword(path, k)
    except RuntimeError as msg:
        print('Unexpected runtime error on parsing',
              path, k, ':', msg, file=sys.stderr)
        print('unlimit the resource limit may fix this problem.',
              file=sys.stderr)
        sys.exit(1)
    output('blob')
    output('mark :%d' % markseq)
    output('data', len(cont))
    output(cont)


class RcsKeywords:
    RCS_KW_AUTHOR   = (1 << 0)
    RCS_KW_DATE     = (1 << 1)
    RCS_KW_LOG      = (1 << 2)
    RCS_KW_NAME     = (1 << 3)
    RCS_KW_RCSFILE  = (1 << 4)
    RCS_KW_REVISION = (1 << 5)
    RCS_KW_SOURCE   = (1 << 6)
    RCS_KW_STATE    = (1 << 7)
    RCS_KW_FULLPATH = (1 << 8)
    RCS_KW_MDOCDATE = (1 << 9)
    RCS_KW_LOCKER   = (1 << 10)

    RCS_KW_ID       = (RCS_KW_RCSFILE | RCS_KW_REVISION | RCS_KW_DATE |
                       RCS_KW_AUTHOR | RCS_KW_STATE)
    RCS_KW_HEADER   = (RCS_KW_ID | RCS_KW_FULLPATH)

    rcs_expkw = {
        b"Author":   RCS_KW_AUTHOR,
        b"Date":     RCS_KW_DATE,
        b"Header":   RCS_KW_HEADER,
        b"Id":       RCS_KW_ID,
        b"Log":      RCS_KW_LOG,
        b"Name":     RCS_KW_NAME,
        b"RCSfile":  RCS_KW_RCSFILE,
        b"Revision": RCS_KW_REVISION,
        b"Source":   RCS_KW_SOURCE,
        b"State":    RCS_KW_STATE,
        b"Mdocdate": RCS_KW_MDOCDATE,
        b"Locker":   RCS_KW_LOCKER
    }

    RCS_KWEXP_NONE    = (1 << 0)
    RCS_KWEXP_NAME    = (1 << 1)    # include keyword name
    RCS_KWEXP_VAL     = (1 << 2)    # include keyword value
    RCS_KWEXP_LKR     = (1 << 3)    # include name of locker
    RCS_KWEXP_OLD     = (1 << 4)    # generate old keyword string
    RCS_KWEXP_ERR     = (1 << 5)    # mode has an error
    RCS_KWEXP_DEFAULT = (RCS_KWEXP_NAME | RCS_KWEXP_VAL)
    RCS_KWEXP_KVL     = (RCS_KWEXP_NAME | RCS_KWEXP_VAL | RCS_KWEXP_LKR)

    def __init__(self):
        self.rerecomple()

    def rerecomple(self):
        pat = b'|'.join([re.escape(k) for k in self.rcs_expkw.keys()])
        self.re_kw = re.compile(b".*?\\$(" + pat + b")[\\$:]")
        self.re_kw_start = re.compile(b"\\$(" + pat + b")[\\$:]")

    def add_id_keyword(self, keyword):
        self.rcs_expkw[keyword.encode('ascii')] = self.RCS_KW_ID
        self.rerecomple()

    def kflag_get(self, flags):
        if flags is None:
            return self.RCS_KWEXP_DEFAULT
        fl = 0
        for fc in flags:
            if fc == 'k':
                fl |= self.RCS_KWEXP_NAME
            elif fc == 'v':
                fl |= self.RCS_KWEXP_VAL
            elif fc == 'l':
                fl |= self.RCS_KWEXP_LKR
            elif fc == 'o':
                if len(flags) != 1:
                    fl |= self.RCS_KWEXP_ERR
                fl |= self.RCS_KWEXP_OLD
            elif fc == 'b':
                if len(flags) != 1:
                    fl |= self.RCS_KWEXP_ERR
                fl |= self.RCS_KWEXP_NONE
            else:
                fl |= self.RCS_KWEXP_ERR
        return fl

    def expand_keyword(self, filename, r):
        rcs = rcsparse.rcsfile(filename)
        return self.expand_rcs_keyword(rcs, filename, r)

    def expand_rcs_keyword(self, rcs, filename, r):
        rev = rcs.revs[r]

        mode = self.kflag_get(rcs.expand)
        if (mode & (self.RCS_KWEXP_NONE | self.RCS_KWEXP_OLD)) != 0:
            return rcs.checkout(rev[0])

        ret = []
        lines = rcs.checkout(rev[0]).split(b'\n')
        for i, line in enumerate(lines):
            has_next_line = i + 1 < len(lines)
            if has_next_line and i + 2 == len(lines) and lines[-1] == b'':
                has_next_line = False
            line, logbuf = self.expand_keyword_line(
                rcs, filename, rev, line, mode, has_next_line)
            ret += [line]
            if logbuf is not None:
                ret += [logbuf]
        return b'\n'.join(ret)

    def expand_keyword_line(self, rcs, filename, rev, line, mode,
                            has_next_line=False, expand_log=True):
        logbuf = None
        m = self.re_kw.match(line)
        if m is None:
            return line, logbuf

        line0 = b''
        while m is not None:
            delim = m.end(0) - 1
            shared_dollar = False
            try:
                if line[delim:delim + 1] == b'$':
                    dsign = delim
                else:
                    dsign = m.end(0) + line[m.end(0):].index(b'$')
            except ValueError:
                break
            prefix = line[:m.start(1) - 1]
            if line[delim:delim + 1] == b':' and \
                    self.re_kw_start.match(line[dsign:]):
                shared_dollar = True
                line = line[dsign:]
            else:
                line = line[dsign + 1:]
            line0 += prefix
            expbuf = ''
            if (mode & self.RCS_KWEXP_NAME) != 0:
                expbuf += '$'
                expbuf += m.group(1).decode('ascii')
                if (mode & self.RCS_KWEXP_VAL) != 0:
                    expbuf += ': '
            if (mode & self.RCS_KWEXP_VAL) != 0:
                expkw = self.rcs_expkw[m.group(1)]
                if (expkw & self.RCS_KW_RCSFILE) != 0:
                    expbuf += filename \
                        if (expkw & self.RCS_KW_FULLPATH) != 0 \
                        else os.path.basename(filename)
                    expbuf += " "
                if (expkw & self.RCS_KW_REVISION) != 0:
                    expbuf += rev[0]
                    expbuf += " "
                if (expkw & self.RCS_KW_DATE) != 0:
                    expbuf += time.strftime(
                        "%Y/%m/%d %H:%M:%S ", time.gmtime(rev[1]))
                if (expkw & self.RCS_KW_MDOCDATE) != 0:
                    d = time.gmtime(rev[1])
                    expbuf += time.strftime(
                        "%B%e %Y " if (d.tm_mday < 10) else "%B %e %Y ", d)
                if (expkw & self.RCS_KW_AUTHOR) != 0:
                    expbuf += rev[2]
                    expbuf += " "
                if (expkw & self.RCS_KW_STATE) != 0:
                    expbuf += rev[3]
                    expbuf += " "
                if (expkw & self.RCS_KW_LOG) != 0:
                    p = prefix
                    expbuf += filename \
                        if (expkw & self.RCS_KW_FULLPATH) != 0 \
                        else os.path.basename(filename)
                    expbuf += " "
                    if expand_log:
                        log = rcs.getlog(rev[0])
                        logbuf = p + (
                            'Revision %s  %s  %s\n' % (
                                rev[0], time.strftime(
                                    "%Y/%m/%d %H:%M:%S", time.gmtime(rev[1])),
                                rev[2])).encode('ascii')
                        for lline in log.rstrip().split(b'\n'):
                            if len(lline) == 0:
                                logbuf += p.rstrip() + b'\n'
                            else:
                                logbuf += p + lline + b'\n'
                        if len(line) == 0:
                            logbuf += p.rstrip()
                            if has_next_line and log.endswith(b'\n\n'):
                                logbuf += b'\n' + p.rstrip()
                        else:
                            tail, _tail_logbuf = self.expand_keyword_line(
                                rcs, filename, rev, line.lstrip(), mode,
                                False, False)
                            logbuf += p + tail
                        line = b''
                if (expkw & self.RCS_KW_SOURCE) != 0:
                    expbuf += filename
                    expbuf += " "
                if (expkw & (self.RCS_KW_NAME | self.RCS_KW_LOCKER)) != 0:
                    expbuf += " "
            if (mode & self.RCS_KWEXP_NAME) != 0 and \
                    not shared_dollar:
                expbuf += '$'
            line0 += expbuf[:255].encode('ascii')
            m = self.re_kw.match(line)

        return line0 + line, logbuf


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
if __name__ == '__main__':
    main()

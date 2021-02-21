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
#   % python cvs2svndump.py -k OpenBSD /cvs/openbsd/src > openbsd.dump
#   % svnadmin create /svnrepo
#   % svn mkdir --parents -m 'mkdir /vendor/openbsd/head/src' \
#       file:///svnrepo/vendor/openbsd/head/src
#   % svnadmin load --parent-dir /vendor/openbsd/head/src /svnrepo \
#       < openbsd.dump
#
#   Periodic import:
#   % sudo cvsync
#   % python cvs2svndump.py -k OpenBSD /cvs/openbsd/src file:///svnrepo \
#       vendor/openbsd/head/src > openbsd2.dump
#   % svnadmin load /svnrepo < openbsd2.dump
#

import getopt
import os
import re
import sys
import time

from hashlib import md5

from svn import core, fs, delta, repos
import rcsparse

CHANGESET_FUZZ_SEC = 300


def usage():
    print('usage: cvs2svndump [-ah] [-z fuzz] [-e email_domain] '
          '[-E log_encodings]\n'
          '\t[-k rcs_keywords] [-m module] cvsroot [svnroot svnpath]]',
          file=sys.stderr)


def main():
    email_domain = None
    do_incremental = False
    dump_all = False
    log_encoding = 'utf-8,iso-8859-1'
    rcs = RcsKeywords()
    modules = []
    fuzzsec = CHANGESET_FUZZ_SEC

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'ahm:z:e:E:k:')
        for opt, v in opts:
            if opt == '-z':
                fuzzsec = int(v)
            elif opt == '-e':
                email_domain = v
            elif opt == '-a':
                dump_all = True
            elif opt == '-E':
                log_encoding = v
            elif opt == '-k':
                rcs.add_id_keyword(v)
            elif opt == '-m':
                modules.append(v)
            elif opt == '-h':
                usage()
                sys.exit(1)
    except getopt.GetoptError as msg:
        print(msg, file=sys.stderr)
        usage()
        sys.exit(1)

    if len(args) != 1 and len(args) != 3:
        usage()
        sys.exit(1)

    log_encodings = log_encoding.split(',')

    cvsroot = args[0]
    while cvsroot[-1] == '/':
        cvsroot = cvsroot[:-1]
    if len(args) == 3:
        svnroot = args[1]
        svnpath = args[2]
    else:
        svnroot = None
        svnpath = None

    if svnroot is None:
        svn = SvnDumper()
    else:
        svn = SvnDumper(svnpath)
        svn.load(svnroot)
        if svn.last_rev is not None:
            do_incremental = True
            print('** svn loaded revision r%d by %s' %
                  (svn.last_rev, svn.last_author), file=sys.stderr)

        # strip off the domain part from the last author since cvs doesn't have
        # the domain part.
        if do_incremental and email_domain is not None and \
                svn.last_author.lower().endswith(('@' + email_domain).lower()):
            last_author = svn.last_author[:-1 * (1 + len(email_domain))]
        else:
            last_author = svn.last_author

    cvs = CvsConv(cvsroot, rcs, not do_incremental, fuzzsec)
    print('** walk cvs tree', file=sys.stderr)
    if len(modules) == 0:
        cvs.walk()
    else:
        for module in modules:
            cvs.walk(module)

    svn.dump = True

    changesets = sorted(cvs.changesets)
    nchangesets = len(changesets)
    print('** cvs has %d changeset' % (nchangesets), file=sys.stderr)

    if nchangesets <= 0:
        sys.exit(0)

    if not dump_all:
        # don't use last 10 minutes for safety
        max_time_max = changesets[-1].max_time - 600
    else:
        max_time_max = changesets[-1].max_time
    printOnce = False

    found_last_revision = False
    for chg_idx, k in enumerate(changesets):
        if do_incremental and not found_last_revision:
            if k.min_time == svn.last_date and k.author == last_author:
                found_last_revision = True
            continue
        if k.max_time > max_time_max:
            break
        if not printOnce:
            output('SVN-fs-dump-format-version: 2')
            output('')
            printOnce = True

        # parse the first file to get log
        log = rcsparse.rcsfile(k.revs[0].path).getlog(k.revs[0].rev)
        for i, e in enumerate(log_encodings):
            try:
                how = 'ignore' if i == len(log_encodings) - 1 else 'strict'
                log = log.decode(e, how)
                break
            except UnicodeError:
                pass

        if email_domain is None:
            email = k.author
        else:
            email = k.author + '@' + email_domain

        revprops = str_prop('svn:author', email)
        revprops += str_prop('svn:date', svn_time(k.min_time))
        revprops += str_prop('svn:log', log)
        revprops += 'PROPS-END\n'

        output('Revision-number: %d' % (chg_idx + 1))
        output('Prop-content-length: %d' % (len(revprops)))
        output('Content-length: %d' % (len(revprops)))
        output('')
        output(revprops)

        for f in k.revs:
            fileprops = ''
            if os.access(f.path, os.X_OK):
                fileprops += str_prop('svn:executable', '*')
            fileprops += 'PROPS-END\n'
            filecont = rcs.expand_keyword(f.path, f.rev)

            md5sum = md5()
            md5sum.update(filecont)

            p = node_path(cvs.cvsroot, svnpath, f.path)
            if f.state == 'dead':
                if not svn.exists(p):
                    print("Warning: remove '%s', but it does "
                          "not exist." % (p), file=sys.stderr)
                    continue
                output('Node-path: %s' % (p))
                output('Node-kind: file')
                output('Node-action: delete')
                output('')
                svn.remove(p)
                continue
            if not svn.exists(p):
                svn.add(p)
                output('Node-path: %s' % (p))
                output('Node-kind: file')
                output('Node-action: add')
            else:
                output('Node-path: %s' % (p))
                output('Node-kind: file')
                output('Node-action: change')

            output('Prop-content-length: %d' % (len(fileprops)))
            output('Text-content-length: %s' % (len(filecont)))
            output('Text-content-md5: %s' % (md5sum.hexdigest()))
            output('Content-length: %d' % (len(fileprops) + len(filecont)))
            output('')
            output(fileprops, end='')
            output(filecont)
            output('')

    if do_incremental and not found_last_revision:
        raise Exception('could not find the last revision')

    print('** dumped', file=sys.stderr)


#
# Write string objects to stdout with the code decided by Python.
# Also write byte objects in raw, without any code conversion (file
# bodies might be various encoding).
#
def output(*args, end='\n'):
    if len(args) == 0:
        pass
    elif len(args) > 1 or isinstance(args[0], str):
        lines = ' '.join(
            [arg if isinstance(arg, str) else str(arg) for arg in args])
        sys.stdout.write(lines)
    else:
        sys.stdout.buffer.write(args[0])
    if len(end) > 0:
        sys.stdout.write(end)
    sys.stdout.flush()


class FileRevision:
    def __init__(self, path, rev, state, markseq):
        self.path = path
        self.rev = rev
        self.state = state
        self.markseq = markseq


class ChangeSetKey:
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

    def put_file(self, path, rev, state, markseq):
        self.revs.append(FileRevision(path, rev, state, markseq))


def _cmp2(a, b):
    _a = a is not None
    _b = b is not None
    return (a > b) - (a < b) if _a and _b else (_a > _b) - (_a < _b)


class CvsConv:
    def __init__(self, cvsroot, rcs, dumpfile, fuzzsec):
        self.cvsroot = cvsroot
        self.rcs = rcs
        self.changesets = dict()
        self.dumpfile = dumpfile
        self.markseq = 0
        self.tags = dict()
        self.fuzzsec = fuzzsec

    def walk(self, module=None):
        p = [self.cvsroot]
        if module is not None:
            p.append(module)
        path = os.path.join(*p)

        for root, _, files in os.walk(path):
            for f in files:
                if not f[-2:] == ',v':
                    continue
                self.parse_file(root + os.sep + f)

        for t, c in list(self.tags.items()):
            c.tags.append(t)

    def parse_file(self, path):
        rtags = dict()
        rcsfile = rcsparse.rcsfile(path)
        branches = {'1': 'HEAD', '1.1.1': 'VENDOR'}
        for k, v in list(rcsfile.symbols.items()):
            r = v.split('.')
            if len(r) == 3:
                branches[v] = 'VENDOR'
            elif len(r) >= 3 and r[-2] == '0':
                branches['.'.join(r[:-2] + r[-1:])] = k
            if len(r) == 2 and branches[r[0]] == 'HEAD':
                if v not in rtags:
                    rtags[v] = list()
                rtags[v].append(k)

        revs = rcsfile.revs.items()
        # sort by revision descending to priorize 1.1.1.1 than 1.1
        revs = sorted(revs, key=lambda a: a[1][0], reverse=True)
        # sort by time
        revs = sorted(revs, key=lambda a: a[1][1])
        novendor = False
        have_initial_revision = False
        last_vendor_status = None
        for k, v in revs:
            r = k.split('.')
            if len(r) == 4 and r[0] == '1' and r[1] == '1' and r[2] == '1' \
                    and r[3] == '1':
                if have_initial_revision:
                    continue
                if v[3] == 'dead':
                    continue
                last_vendor_status = v[3]
                have_initial_revision = True
            elif len(r) == 4 and r[0] == '1' and r[1] == '1' and r[2] == '1':
                if novendor:
                    continue
                last_vendor_status = v[3]
            elif len(r) == 2:
                if r[0] == '1' and r[1] == '1':
                    if have_initial_revision:
                        continue
                    if v[3] == 'dead':
                        continue
                    have_initial_revision = True
                elif r[0] == '1' and r[1] != '1':
                    novendor = True
                if last_vendor_status == 'dead' and v[3] == 'dead':
                    last_vendor_status = None
                    continue
                last_vendor_status = None
            else:
                # trunk only
                continue

            if self.dumpfile:
                self.markseq = self.markseq + 1

            b = '.'.join(r[:-1])
            try:
                a = ChangeSetKey(
                    branches[b], v[2], v[1], rcsfile.getlog(v[0]), v[6],
                    self.fuzzsec)
            except Exception as e:
                print('Aborted at %s %s' % (path, v[0]), file=sys.stderr)
                raise e

            a.put_file(path, k, v[3], self.markseq)
            while a in self.changesets:
                c = self.changesets[a]
                del self.changesets[a]
                c.merge(a)
                a = c
            self.changesets[a] = a
            if k in rtags:
                for t in rtags[k]:
                    if t not in self.tags or \
                            self.tags[t].max_time < a.max_time:
                        self.tags[t] = a


def node_path(r, n, p):
    if r.endswith('/'):
        r = r[:-1]
    path = p[:-2]               # drop ",v"
    p = path.split('/')
    if len(p) > 0 and p[-2] == 'Attic':
        path = '/'.join(p[:-2] + [p[-1]])
    if path.startswith(r):
        path = path[len(r) + 1:]
    if n is None or len(n) == 0:
        return path
    return '%s/%s' % (n, path)


def str_prop(k, v):
    return 'K %d\n%s\nV %d\n%s\n' % (len(k), k, len(v), v)


def svn_time(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(t))


class SvnDumper:
    def __init__(self, root=''):
        self.root = root
        if self.root != '' and self.root[-1] == '/':
            self.root = self.root[:-1]
        self.dirs = {}
        self.dirs[self.root] = {'dontdelete': 1}
        self.dump = False
        self.last_author = None
        self.last_date = None
        self.last_rev = None

    def exists(self, path):
        d = os.path.dirname(path)
        if d not in self.dirs:
            return False
        return os.path.basename(path) in self.dirs[d]

    def add(self, path):
        d = os.path.dirname(path)
        if d not in self.dirs:
            self.mkdir(d)
        self.dirs[d][os.path.basename(path)] = 1

    def remove(self, path):
        d = os.path.dirname(path)
        if d == path:
            return
        del self.dirs[d][os.path.basename(path)]
        self.rmdir(d)

    def rmdir(self, path):
        if len(self.dirs[path]) > 0:
            return
        for r in list(self.dirs.keys()):
            if r != path and r.startswith(path + '/'):
                return
        if self.dump:
            output('Node-path: %s' % (path))
            output('Node-kind: dir')
            output('Node-action: delete')
            output('')
        del self.dirs[path]
        d = os.path.dirname(path)
        if d == path or d not in self.dirs:
            return
        self.rmdir(d)

    def mkdir(self, path):
        if path not in self.dirs:
            d = os.path.dirname(path)
            if d == path:
                return
            self.mkdir(d)
            if self.dump:
                output('Node-path: %s' % (path))
                output('Node-kind: dir')
                output('Node-action: add')
                output('')
                output('')
            self.dirs[path] = {}

    def load(self, repo_path):
        repo_path = core.svn_path_canonicalize(repo_path)
        repos_ptr = repos.open(repo_path)
        fs_ptr = repos.fs(repos_ptr)
        rev = fs.youngest_rev(fs_ptr)
        base_root = fs.revision_root(fs_ptr, 0)
        root = fs.revision_root(fs_ptr, rev)
        hist = fs.node_history(root, self.root)
        while hist is not None:
            hist = fs.history_prev(hist, 0)
            dummy, rev = fs.history_location(hist)
            d = fs.revision_prop(fs_ptr, rev, core.SVN_PROP_REVISION_DATE)
            author = fs.revision_prop(
                fs_ptr, rev, core.SVN_PROP_REVISION_AUTHOR)
            if author == 'svnadmin':
                continue
            self.last_author = author
            self.last_date = core.svn_time_from_cstring(d) / 1000000
            self.last_rev = rev

            def authz_cb(root, path, pool):
                return 1

            editor = SvnDumperEditor(self)
            e_ptr, e_baton = delta.make_editor(editor)
            repos.dir_delta(
                base_root, '', '', root, self.root, e_ptr, e_baton, authz_cb,
                0, 1, 0, 0)
            break


class SvnDumperEditor(delta.Editor):
    def __init__(self, dumper):
        self.dumper = dumper

    def add_file(self, path, *args):
        self.dumper.add(self.dumper.root + '/' + path)

    def add_directory(self, path, *args):
        self.dumper.mkdir(self.dumper.root + '/' + path)


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
        pat = b'|'.join(list(self.rcs_expkw.keys()))
        self.re_kw = re.compile(b".*?\\$(" + pat + b")[\\$:]")

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
        rev = rcs.revs[r]

        mode = self.kflag_get(rcs.expand)
        if (mode & (self.RCS_KWEXP_NONE | self.RCS_KWEXP_OLD)) != 0:
            return rcs.checkout(rev[0])

        ret = []
        for line in rcs.checkout(rev[0]).split(b'\n'):
            logbuf = None
            m = self.re_kw.match(line)
            if m is None:
                # No RCS Keywords, use it as it is
                ret += [line]
                continue

            line0 = b''
            while m is not None:
                try:
                    dsign = m.end(1) + line[m.end(1):].index(b'$')
                except ValueError:
                    break
                prefix = line[:m.start(1) - 1]
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
                        logbuf = p + (
                            'Revision %s  %s  %s\n' % (
                                rev[0], time.strftime(
                                    "%Y/%m/%d %H:%M:%S", time.gmtime(rev[1])),
                                rev[2])).encode('ascii')
                        for lline in rcs.getlog(rev[0]).rstrip().split(b'\n'):
                            if len(lline) == 0:
                                logbuf += p.rstrip() + b'\n'
                            else:
                                logbuf += p + lline.lstrip() + b'\n'
                        if len(line) == 0:
                            logbuf += p.rstrip()
                        else:
                            logbuf += p + line.lstrip()
                        line = b''
                    if (expkw & self.RCS_KW_SOURCE) != 0:
                        expbuf += filename
                        expbuf += " "
                    if (expkw & (self.RCS_KW_NAME | self.RCS_KW_LOCKER)) != 0:
                        expbuf += " "
                if (mode & self.RCS_KWEXP_NAME) != 0:
                    expbuf += '$'
                line0 += expbuf[:255].encode('ascii')
                m = self.re_kw.match(line)

            ret += [line0 + line]
            if logbuf is not None:
                ret += [logbuf]
        return b'\n'.join(ret)


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
if __name__ == '__main__':
    main()

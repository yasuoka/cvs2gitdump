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
import rcsparse
import re
import subprocess
import sys
import time

CHANGESET_FUZZ_SEC = 300

def usage():
    print('usage: cvs2gitdump [-ah] [-z fuzz] [-e email_domain] ' \
                '[-E log_encodings]\n' \
            '\t[-k rcs_keywords] [-b branch] [-m module] [-l last_revision]\n'\
            '\tcvsroot [git_dir]', file=sys.stderr)

def main():
    email_domain = None
    do_incremental = False
    git_tip = None
    git_branch = 'master'
    dump_all = False
    log_encoding = 'utf-8,iso-8859-1'
    rcs = RcsKeywords();
    modules = []
    last_revision = None
    fuzzsec = CHANGESET_FUZZ_SEC

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'ab:hm:z:e:E:k:t:l:')
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
                    print('Cannot handle the path named \'.git\'', file=sys.stderr)
                    sys.exit(1)
                modules.append(v)
            elif opt == '-l':
                last_revision = v
            elif opt == '-h':
                usage()
                sys.exit(1)
    except Exception as msg:
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
        git = subprocess.Popen(['git', '--git-dir=' + args[1], 'log',
            '--max-count', '1', '--date=raw', '--format=%ae%n%ad%n%H',
            git_branch], stdout=subprocess.PIPE)
        outs = git.stdout.readlines()
        git.wait()
        if git.returncode != 0:
            print("Coundn't exec git", file=sys.stderr)
            sys.exit(git.returncode)
        git_tip = outs[2].strip()

        if last_revision is not None:
            git = subprocess.Popen(['git', '--git-dir=' + args[1], 'log',
                '--max-count', '1', '--date=raw', '--format=%ae%n%ad%n%H',
                last_revision], stdout=subprocess.PIPE)
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

    cvs = CvsConv(cvsroot, rcs, not do_incremental, fuzzsec)
    print('** walk cvs tree', file=sys.stderr)
    if len(modules) == 0:
        cvs.walk()
    else:
        for module in modules:
            cvs.walk(module)

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

    found_last_revision = False
    markseq = cvs.markseq
    extags = set()
    for k in changesets:
        if do_incremental and not found_last_revision:
            if k.min_time == last_ctime and k.author == last_author:
                found_last_revision = True
            for tag in k.tags:
                extags.add(tag)
            continue
        if k.max_time > max_time_max:
            break

        marks = {}

        for f in k.revs:
            if not do_incremental:
                marks[f.markseq] = f
            else:
                markseq = markseq + 1
                git_dump_file(f.path, f.rev, rcs, markseq)
                marks[markseq] = f
        log = rcsparse.rcsfile(k.revs[0].path).getlog(k.revs[0].rev)
        for i, e in enumerate(log_encodings):
            try:
                how = 'ignore' if i == len(log_encodings) - 1 else 'strict';
                log = log.decode(e, how)
                break
            except:
                pass
        log = log.encode('utf-8', 'ignore')

        print('commit refs/heads/' + git_branch)
        markseq = markseq + 1
        print('mark :%d' % (markseq))
        email = k.author if email_domain is None \
                else k.author + '@' + email_domain
        print('author %s <%s> %d +0000' % (k.author, email, k.min_time))
        print('committer %s <%s> %d +0000' % (k.author, email, k.min_time))

        print('data', len(log))
        print(log, end=' ')
        if do_incremental and git_tip is not None:
            print('from', git_tip)
            git_tip = None

        for m in marks:
            f = marks[m]
            mode = 0o100755 if os.access(f.path, os.X_OK) else 0o100644
            fn = node_path(cvs.cvsroot, None, f.path) # XXX
            if f.state == 'dead':
                print('D', fn)
            else:
                print('M %o :%d %s' % (mode, m, fn))
        print('')
        for tag in k.tags:
            if tag in extags:
                continue
            print('reset refs/tags/%s' % (tag))
            print('from :%d' % (markseq))
            print('')

    if do_incremental and not found_last_revision:
        raise Exception('could not find the last revision')

    print('** dumped', file=sys.stderr)

class FileRevision:
    def __init__(self, path, rev, state, markseq):
        self.path = path
        self.rev = rev
        self.state = state
        self.markseq = markseq

class ChangeSetKey:
    def __init__(self, branch, author, time, log, commitid, fuzzsec):
        self.branch = branch
        self.author = author
        self.min_time = time
        self.max_time = time
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
        return self._cmp(other, False) < 0
    def __gt__(self, other):
        return self._cmp(other, False) > 0
    def __eq__(self, other):
        return self._cmp(other, False) == 0
    def __le__(self, other):
        return self._cmp(other, False) <= 0
    def __ge__(self, other):
        return self._cmp(other, False) >= 0
    def __ne__(self, other):
        return self._cmp(other, False) != 0

    def _cmp(obja, objb, quick):
        # compare by the commitid
        cid = _cmp2(obja.commitid, objb.commitid)
        if cid == 0 and self.commitid is not None:
            # both have commitid and they are same
            return 0

        # compare by the time
        ma = objb.min_time - obja.max_time
        mi = obja.min_time - objb.max_time
        ct = obja.min_time - objb.min_time
        if ma > obja.fuzzsec or mi > obja.fuzzsec:
            return ct

        if cid != 0:
            # only one has the commitid, this means different commit
            return cid if ct == 0 else ct

        # compare by log, branch and author
        c = _cmp2(obja.log_hash, objb.log_hash)
        if c == 0: c = _cmp2(obja.branch, objb.branch)
        if c == 0: c = _cmp2(obja.author, objb.author)
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

    def walk(self, module = None):
        p = [self.cvsroot]
        if module is not None: p.append(module)
        path = os.path.join(*p)

        for root, dirs, files in os.walk(path):
            if '.git' in dirs:
                print('Ignore %s: cannot handle the path ' \
                    'named \'.git\'' % (root + os.sep + '.git'), file=sys.stderr)
                dirs.remove('.git')
            if '.git' in files:
                print('Ignore %s: cannot handle the path ' \
                    'named \'.git\'' % (root + os.sep + '.git'), file=sys.stderr)
                files.remove('.git')
            for f in files:
                if not f[-2:] == ',v': continue
                self.parse_file(root + os.sep + f)

        for t,c in list(self.tags.items()):
            c.tags.append(t)

    def parse_file(self, path):
        rtags = dict()
        rcsfile=rcsparse.rcsfile(path)
        path_related = path[len(self.cvsroot) + 1:][:-2]
        branches = {'1': 'HEAD', '1.1.1': 'VENDOR' }
        have_111 = False
        for k,v in list(rcsfile.symbols.items()):
            r = v.split('.')
            if len(r) == 3:
                branches[v] = 'VENDOR'
            elif len(r) >= 3 and r[-2] == '0':
                branches['.'.join(r[:-2] + r[-1:])] = k
            if len(r) == 2 and branches[r[0]] == 'HEAD':
                if v not in rtags:
                    rtags[v] = list()
                rtags[v].append(k)

        # sort by time and revision
        revs = sorted(list(rcsfile.revs.items()), \
                lambda a,b: cmp(a[1][1], b[1][1]) or cmp(b[1][0], a[1][0]))
        p = '0'
        novendor = False
        have_initial_revision = False
        last_vendor_status = None
        for k,v in revs:
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
                git_dump_file(path, k, self.rcs, self.markseq)

            b = '.'.join(r[:-1])
            try:
                a = ChangeSetKey(branches[b], v[2], v[1], rcsfile.getlog(v[0]),
                        v[6], self.fuzzsec)
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
            p = k
            if k in rtags:
                for t in rtags[k]:
                    if t not in self.tags or \
                            self.tags[t].max_time < a.max_time:
                        self.tags[t] = a

def node_path(r,n,p):
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

def git_dump_file(path, k, rcs, markseq):
    try:
        cont = rcs.expand_keyword(path, k)
    except RuntimeError as msg:
        print('Unexpected runtime error on parsing', \
                path, k, ':', msg, file=sys.stderr)
        print('unlimit the resource limit may fix ' \
                'this problem.', file=sys.stderr)
        sys.exit(1)
    print('blob')
    print('mark :%d' % markseq)
    print('data', len(cont))
    print(cont)

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
                       RCS_KW_AUTHOR | RCS_KW_STATE )
    RCS_KW_HEADER   = (RCS_KW_ID | RCS_KW_FULLPATH)

    rcs_expkw = {
        "Author":   RCS_KW_AUTHOR,
        "Date":     RCS_KW_DATE ,
        "Header":   RCS_KW_HEADER,
        "Id":       RCS_KW_ID,
        "Log":      RCS_KW_LOG,
        "Name":     RCS_KW_NAME,
        "RCSfile":  RCS_KW_RCSFILE,
        "Revision": RCS_KW_REVISION,
        "Source":   RCS_KW_SOURCE,
        "State":    RCS_KW_STATE,
        "Mdocdate": RCS_KW_MDOCDATE,
        "Locker":   RCS_KW_LOCKER
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
        pat = '|'.join(list(self.rcs_expkw.keys()))
        self.re_kw = re.compile(r".*?\$(" + pat + ")[\$:]")

    def add_id_keyword(self, keyword):
        self.rcs_expkw[keyword] = self.RCS_KW_ID
        self.rerecomple()

    def kflag_get(self,flags):
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

        s = logbuf = ''
        for line in rcs.checkout(rev[0]).split('\n'):
            while True:
                m = self.re_kw.match(line)
                if m is None:
                    break
                try:
                    dsign = m.end(1) + line[m.end(1):].index('$')
                except:
                    braek;
                prefix = line[:m.start(1)-1]
                line = line[dsign + 1:]
                s += prefix
                expbuf = ''
                if (mode & self.RCS_KWEXP_NAME) != 0:
                    expbuf += '$'
                    expbuf += m.group(1)
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
                        expbuf += time.strftime("%Y/%m/%d %H:%M:%S ", \
                                time.gmtime(rev[1]))
                    if (expkw & self.RCS_KW_MDOCDATE) != 0:
                        d = time.gmtime(rev[1])
                        expbuf += time.strftime("%B%e %Y " \
                                if (d.tm_mday < 10) else "%B %e %Y ", d)
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
                        logbuf += '%sRevision %s  ' % (p, rev[0])
                        logbuf += time.strftime("%Y/%m/%d %H:%M:%S  ",\
                                time.gmtime(rev[1]))
                        logbuf += rev[2] + '\n'
                        for lline in rcs.getlog(rev[0]).rstrip().split('\n'):
                            if lline == '':
                                logbuf += p.rstrip() + '\n'
                            else:
                                logbuf += p + lline.lstrip() +  '\n'
                        if line == '':
                            logbuf += p.rstrip() + '\n'
                        else:
                            logbuf += p + line.lstrip() +  '\n'
                        line = ''
                    if (expkw & self.RCS_KW_SOURCE) != 0:
                        expbuf += filename
                        expbuf += " "
                    if (expkw & (self.RCS_KW_NAME | self.RCS_KW_LOCKER)) != 0:
                        expbuf += " "
                if (mode & self.RCS_KWEXP_NAME) != 0:
                    expbuf += '$'
                s += expbuf[:255]
            s += line + '\n'
            if len(logbuf) > 0:
                s += logbuf
                logbuf = ''
        return s[:-1]

# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
if __name__ == '__main__':
    main()

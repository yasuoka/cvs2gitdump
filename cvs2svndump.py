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
#	file:///svnrepo/vendor/openbsd/head/src
#   % svnadmin load --parent-dir /vendor/openbsd/head/src /svnrepo \
#	< openbsd.dump
#
#   Periodic import:
#   % sudo cvsync
#   % python cvs2svndump.py -k OpenBSD /cvs/openbsd/src file:///svnrepo \
#	vendor/openbsd/head/src > openbsd2.dump
#   % svnadmin load /svnrepo < openbsd2.dump
#	

# $Id$

import getopt
import os
import rcsparse
import re
import string
import subprocess
import sys
import time

from hashlib import md5
from svn import core, fs, delta, repos

CHANGESET_FUZZ_SEC = 300

def usage():
    print >>sys.stderr, \
	    'usage: cvs2svndump [-ah] [-z fuzz] [-e email_domain] '\
		'[-E log_encodings]\n'\
	    '\t[-k rcs_keywords] [-m module] cvsroot [svnroot svnpath]]'

def main():
    email_domain = None
    do_incremental = False
    dump_all = False
    log_encoding = 'utf-8,iso-8859-1'
    rcs = RcsKeywords();
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
    except Exception, msg:
	print >>sys.stderr, msg
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
	try:
	    svn.load(svnroot)
	    if svn.last_rev is not None:
		do_incremental = True
		print >>sys.stderr, '** svn loaded revision r%d by %s' % \
			(svn.last_rev, svn.last_author)
	except:
	    pass

	# strip off the domain part from the last author since cvs doesn't have
	# the domain part.
	if do_incremental and email_domain is not None and \
		svn.last_author.lower().endswith(('@' + email_domain).lower()):
	    last_author = svn.last_author[:-1 * (1 + len(email_domain))]
	else:
	    last_author = svn.last_author

    cvs = CvsConv(cvsroot, rcs, not do_incremental, fuzzsec)
    print >>sys.stderr, '** walk cvs tree'
    if len(modules) == 0:
	cvs.walk()
    else:
	for module in modules:
	    cvs.walk(module)

    svn.dump = True

    changesets = sorted(cvs.changesets)
    nchangesets = len(changesets)
    print >>sys.stderr, '** cvs has %d changeset' % (nchangesets)

    if nchangesets <= 0:
	sys.exit(0)

    if not dump_all:
	# don't use last 10 minutes for safety
	max_time_max = changesets[-1].max_time - 600
    else:
	max_time_max = changesets[-1].max_time
    printOnce = False

    found_last_revision = False
    for i, k in enumerate(changesets):
	if do_incremental and not found_last_revision:
	    if k.min_time == svn.last_date and k.author == last_author:
		found_last_revision = True
	    continue
	if k.max_time > max_time_max:
	    break
	if not printOnce:
	    print 'SVN-fs-dump-format-version: 2'
	    print ''
	    printOnce = True

	# parse the first file to get log
	finfo = k.revs[0]
	rcsfile = rcsparse.rcsfile(finfo.path)
	log = rcsparse.rcsfile(k.revs[0].path).getlog(k.revs[0].rev)
	for i, e in enumerate(log_encodings):
	    try:
		how = 'ignore' if i == len(log_encodings) - 1 else 'strict';
		log = log.decode(e, how)
		break
	    except:
		pass
	log = log.encode('utf-8', 'ignore')

	if email_domain is None:
	    email = k.author
        else:
	    email = k.author + '@' + email_domain

	revprops = str_prop('svn:author', email)
	revprops += str_prop('svn:date', svn_time(k.min_time))
	revprops += str_prop('svn:log', log)
	revprops += 'PROPS-END\n'

	print 'Revision-number: %d' % (i + 1)
	print 'Prop-content-length: %d' % (len(revprops))
	print 'Content-length: %d' % (len(revprops))
	print ''
	print revprops

	for f in k.revs:
	    rcsfile = rcsparse.rcsfile(f.path)
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
		    print >> sys.stderr, "Warning: remove '%s', but it does "\
			"not exist." % (p)
		    continue
		print 'Node-path: %s' % (p)
		print 'Node-kind: file'
		print 'Node-action: delete'
		print ''
		svn.remove(p)
		continue
	    elif not svn.exists(p):
		svn.add(p)
		print 'Node-path: %s' % (p)
		print 'Node-kind: file'
		print 'Node-action: add'
	    else:
		print 'Node-path: %s' % (p)
		print 'Node-kind: file'
		print 'Node-action: change'

	    print 'Prop-content-length: %d' % (len(fileprops))
	    print 'Text-content-length: %s' % (len(filecont))
	    print 'Text-content-md5: %s' % (md5sum.hexdigest())
	    print 'Content-length: %d' % (len(fileprops) + len(filecont))
	    print ''
	    print fileprops + filecont
	    print ''

    if do_incremental and not found_last_revision:
	raise Exception('could not find the last revision')

    print >>sys.stderr, '** dumped'

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
	    h = 31 * h + ord(c)
	self.log_hash = h

    def __cmp__(self, anon):
	if isinstance(anon, ChangeSetKey):

	    # compare by the commitid
	    cid = cmp(self.commitid, anon.commitid)
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
	    c = cmp(self.log_hash, anon.log_hash)
	    if c == 0: c = cmp(self.branch, anon.branch)
	    if c == 0: c = cmp(self.author, anon.author)
	    if c == 0:
		return 0
	    return ct if ct != 0 else c

	return -1

    def merge(self, anon):
	self.max_time = max(self.max_time, anon.max_time)
	self.min_time = min(self.min_time, anon.min_time)
	self.revs.extend(anon.revs)

    def __hash__(self):
	return hash(self.branch + '/' + self.author) * 31 + self.log_hash

    def put_file(self, path, rev, state, markseq):
	self.revs.append(FileRevision(path, rev, state, markseq))

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
	path = reduce(os.path.join, p)

	for root, dirs, files in os.walk(path):
	    for f in files:
		if not f[-2:] == ',v': continue
		self.parse_file(root + os.sep + f)

	for t,c in self.tags.items():
	    c.tags.append(t)

    def parse_file(self, path):
	rtags = dict()
	rcsfile=rcsparse.rcsfile(path)
	path_related = path[len(self.cvsroot) + 1:][:-2]
	branches = {'1': 'HEAD', '1.1.1': 'VENDOR' }
	have_111 = False
	for k,v in rcsfile.symbols.items():
	    r = v.split('.')
	    if len(r) == 3:
		branches[v] = 'VENDOR'
	    elif len(r) >= 3 and r[-2] == '0':
		z = reduce(lambda a, b: a + '.' + b, r[:-2] + r[-1:])
		branches[reduce(lambda a, b: a + '.' + b, r[:-2] + r[-1:])] = k
	    if len(r) == 2 and branches[r[0]] == 'HEAD':
		if not rtags.has_key(v):
		    rtags[v] = list()
		rtags[v].append(k)

	# sort by time and revision
	revs = sorted(rcsfile.revs.items(), \
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

	    b = reduce(lambda a, b: a + '.' + b, r[:-1])
	    try:
		a = ChangeSetKey(branches[b], v[2], v[1], rcsfile.getlog(v[0]),
			v[6], self.fuzzsec)
	    except Exception as e:
		print >>sys.stderr, 'Aborted at %s %s' % (path, v[0])
		raise e

	    a.put_file(path, k, v[3], self.markseq)
	    while self.changesets.has_key(a):
		c = self.changesets[a]
		del self.changesets[a]
		c.merge(a)
		a = c
	    self.changesets[a] = a
	    p = k
	    if rtags.has_key(k):
		for t in rtags[k]:
		    if not self.tags.has_key(t) or \
			    self.tags[t].max_time < a.max_time:
			self.tags[t] = a

def node_path(r,n,p):
    if r.endswith('/'):
	r = r[:-1]
    path = p[:-2]
    p = path.split('/')
    if len(p) > 0 and p[-2] == 'Attic':
	path = string.join(p[:-2], '/') + '/' + p[-1]
    if path.startswith(r):
	path = path[len(r) + 1:]
    if n is None or len(n) == 0:
	return path
    return '%s/%s' % (n, path)

def str_prop(k,v):
    return 'K %d\n%s\nV %d\n%s\n' % (len(k), k, len(v), v)

def svn_time(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(t))

class SvnDumper:
    def __init__(self, root=''):
	self.root = root
	if self.root != '' and self.root[-1] == '/':
	    self.root = self.root[:-1]
	self.dirs = {}
	self.dirs[self.root] = {'dontdelete': 1}
	self.dump = False

    def exists(self, path):
	d = os.path.dirname(path)
	if not self.dirs.has_key(d):
	    return False
	return self.dirs[d].has_key(os.path.basename(path))

    def add(self, path):
	d = os.path.dirname(path)
	if not self.dirs.has_key(d):
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
	for r in self.dirs.keys():
	    if r != path and r.startswith(path + '/'):
		return
	if self.dump:
	    print 'Node-path: %s' % (path)
	    print 'Node-kind: dir'
	    print 'Node-action: delete'
	    print ''
	del self.dirs[path]
	d = os.path.dirname(path)
	if d == path or not self.dirs.has_key(d):
	    return
	self.rmdir(d)

    def mkdir(self, path):
	if not self.dirs.has_key(path):
	    d = os.path.dirname(path)
	    if d == path:
		return
	    self.mkdir(d)
	    if self.dump:
		print 'Node-path: %s' % (path)
		print 'Node-kind: dir'
		print 'Node-action: add'
		print ''
		print ''
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
	    hist = fs.history_prev(hist,0)
	    dummy,rev = fs.history_location(hist)
	    d = fs.revision_prop(fs_ptr, rev, core.SVN_PROP_REVISION_DATE)
	    author = fs.revision_prop(fs_ptr, rev, \
		    core.SVN_PROP_REVISION_AUTHOR)
	    if author == 'svnadmin':
		continue
	    self.last_author = author
	    self.last_date = core.svn_time_from_cstring(d) / 1000000
	    self.last_rev = rev
	    def authz_cb(root, path, pool):
		return 1
	    editor = SvnDumperEditor(self)
	    e_ptr, e_baton = delta.make_editor(editor)
	    repos.dir_delta(base_root, '', '', root, self.root, e_ptr, e_baton,
		authz_cb, 0, 1, 0, 0)
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
	pat = '|'.join(self.rcs_expkw.keys())
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
		if len(line) > m.end(1) and line[m.end(1)] == '$':
		    dsign = m.end(1)
		else:
		    try:
			dsign = string.index(line, '$', m.end(1))
			if dsign < 0:
			    break
		    except:
			break
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

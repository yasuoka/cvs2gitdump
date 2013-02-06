cvs2gitdump
===========

A small python script which imports cvs tree into git repository.

- requires 'rcsparse' is installed
  - http://gitorious.org/fromcvs/rcsparse
- imports incremetally
- converts only tags on HEAD
- not convert any branches

Example
-------

First import:

    % git init --bare openbsd.git
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src > openbsd.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd.dump

Periodic import:

    % sudo cvsync
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src /git/openbsd.git > openbsd2.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd2.dump

cvs2svndump
===========

A small python script which imports cvs tree into subversion repository.

- requires 'rcsparse' is installed
  - http://gitorious.org/fromcvs/rcsparse
- imports incremetally
- not convert any tags and any branches

Example
-------

First import:

    % python cvs2svndump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src > openbsd.dump
    % svnadmin create /svnrepo
    % svn mkdir --parents -m 'mkdir /vendor/openbsd/head' file:///svnrepo/vendor/openbsd/head
    % svnadmin load --parent-dir /vendor/openbsd/head /svnrepo < openbsd.dump

Periodic import:

    % sudo cvsync
    % python cvs2svndump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src file:///svnrepo vendor/openbsd/head > openbsd2.dump
    % svnadmin load --parent-dir /vendor/openbsd/head /svnrepo < openbsd2.dump


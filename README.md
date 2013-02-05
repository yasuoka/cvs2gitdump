cvs2gitdump
===========

A python script which imports cvs tree into git repository.

- requires `rcsparse'
- supports incremetal import
- support convert tags on HEAD only
- not support any branches

Usage
-----

First import:

    % git init --bare openbsd.git
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src \
        > openbsd.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd.dump

Periodic import:

    % sudo cvsync
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src \
        /git/openbsd.git > openbsd2.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd2.dump

cvs2svndump
===========

XXX

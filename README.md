cvs2gitdump
===========

A small python script which imports cvs tree into git repository.

Pros:
- Small footprint
- Incremental import is supported.  It's fast
- Convert tags on HEAD

Cons:
- Don't convert branches

An alternative to
- [git-cvs](https://github.com/ustuehler/git-cvs)
- [bigcvs2git](https://github.com/jcs/bigcvs2git)
- [cvs2svn](http://cvs2svn.tigris.org/)

Requires:
- [rcsparse](http://gitorious.org/fromcvs/rcsparse)


Usage
-----

    usage: cvs2gitdump [-ah] [-z fuzz] [-e email_domain] [-E log_encoding]
        [-k rcs_keywords] [-b branch] cvsroot [git_dir]


### Options

* -a

  As the default the script will not use the changes in the last 10
  minutes because they are not stable if the repository is changing.
  This option will change this behavior, it will use the entire changes.

* -b branch

  The branch name of the git repository which is used for incremental
  import.

* -h

  Show the usage.

* -z fuzz

  When the script collects changesets from CVS repository, commits by
  the same author, using the same log message and within ``fuzz``
  seconds are collected into the same changeset.  300 (seconds) is used
  as the default.

* -e email_domain

  Append the email domain to the author.

* -E log_encoding

  Specify the character encoding which is used in CVS logs.

* -k rcs_keywords

  Add an extra RCS keyword which are used by CVS.  The script
  substitutes the RCS keyword by the same way as $Id$.

* cvsroot

  The target cvsroot or the sub directory of the cvsroot.

* git_dir

  The git repository.  Specify this for incremental import.

Example
-------

First import:

    % git init --bare /git/openbsd.git
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src > openbsd.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd.dump

Periodic import:

    % sudo cvsync
    % python cvs2gitdump.py -k OpenBSD -e openbsd.org /cvs/openbsd/src /git/openbsd.git > openbsd2.dump
    % git --git-dir /git/openbsd.git fast-import < openbsd2.dump


cvs2svndump
===========

A small python script which imports cvs tree into subversion repository.

Pros:
- Small footprint
- Incremental import is supported.  It's fast

Cons:
- Don't convert tags and branches

Requires:
- [rcsparse](http://gitorious.org/fromcvs/rcsparse)
- svn (Python interface for subversion)


Usage
-----

    usage: cvs2gitdump [-ah] [-z fuzz] [-e email_domain] [-E log_encoding]
        [-k rcs_keywords] cvsroot [git_dir]

    usage: cvs2svndump [-ah] [-z fuzz] [-e email_domain] [-E log_encoding]
	[-k rcs_keywords] cvsroot [svnroot svnpath]]


### Options

* -a

  As the default the script will not use the changes in the last 10
  minutes because they are not stable if the repository is changing.
  This option will change this behavior, it will use the entire changes.

* -h

  Show the usage.

* -z fuzz

  When the script collects changesets from CVS repository, commits by
  the same author, using the same log message and within ``fuzz``
  seconds are collected into the same changeset.  300 (seconds) is used
  as the default.

* -e email_domain

  Append the email domain to the author.

* -E log_encoding

  Specify the character encoding which is used in CVS logs.

* -k rcs_keywords

  Add an extra RCS keyword which are used by CVS.  The script
  substitutes the RCS keyword by the same way as $Id$.

* cvsroot

  The target cvsroot or the sub directory of the cvsroot.

* svn_dir svn_path

  Specify the svn repository and path.  Specify these for incremental
  import.


Example
-------

First import:

    % python cvs2svndump.py -k OpenBSD /cvs/openbsd/src > openbsd.dump
    % svnadmin create /svnrepo
    % svn mkdir --parents -m 'mkdir /vendor/openbsd/head/src' file:///svnrepo/vendor/openbsd/head/src
    % svnadmin load --parent-dir /vendor/openbsd/head/src /svnrepo < openbsd.dump

Periodic import:

    % sudo cvsync
    % python cvs2svndump.py -k OpenBSD /cvs/openbsd/src file:///svnrepo vendor/openbsd/head/src > openbsd2.dump
    % svnadmin load /svnrepo < openbsd2.dump


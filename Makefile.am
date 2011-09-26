AUTOMAKE_OPTIONS = dist-bzip2 no-dist-gzip 1.8

ACLOCAL_AMFLAGS = -I m4 -I common

include $(srcdir)/common/changelog.mk
include $(srcdir)/common/common.mk
include $(srcdir)/common/pychecker.mk
include $(srcdir)/common/deb.mk
include $(srcdir)/common/rpm.mk

SUBDIRS = flumotion conf common misc po

EXTRA_DIST = \
	env.in \
	.version \
	RELEASE \
	LICENSE.LGPL \
	flumotion-flashmedia.spec \
        intltool-extract.in \
        intltool-merge.in \
        intltool-update.in

PYCHECKER_WHITELIST = flumotion/component/*/*/*.py \
                      flumotion/component/*/*.py \
PYCHECKER_BLACKLIST =

COVERAGE_MODULES = \
	flumotion.component.muxers.asf \
	flumotion.component.encoders.wmv \
	flumotion.component.encoders.wma

BUILT_SOURCES = $(top_srcdir)/.version
$(top_srcdir)/.version:
	echo $(VERSION) > $@-t && mv $@-t $@

dist: changelog

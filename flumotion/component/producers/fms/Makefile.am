include $(top_srcdir)/common/python.mk

componentdir = $(libdir)/flumotion/python/flumotion/component/producers/fms
component_PYTHON = __init__.py fms.py live.py admin_gtk.py
component_DATA = \
	fms.xml  \
        fms.glade

clean-local:
	rm -rf *.pyc *.pyo

EXTRA_DIST = $(component_DATA)

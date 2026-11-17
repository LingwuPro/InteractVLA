"""Expose the exported modules alongside an installed StarVLA framework."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

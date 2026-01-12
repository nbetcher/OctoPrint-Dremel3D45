# -*- coding: utf-8 -*-
from setuptools import setup

plugin_identifier = "dremel3d45"
plugin_package = "octoprint_dremel3d45"
plugin_name = "OctoPrint-Dremel3D45"
plugin_version = "0.1.0"
plugin_description = "Virtual driver for Dremel 3D45 printer over network (REST API)"
plugin_author = "Nick Betcher"
plugin_author_email = "nick@nickbetcher.com"
plugin_url = "https://www.nickbetcher.com/projects/octoprint_dremel3d45"
plugin_license = "MIT"

# Use forked dremel3dpy with layer field support until upstream PR is merged.
# Once merged, revert to: plugin_requires = ["dremel3dpy>=2.2.0"]
# PEP 440 direct URL reference for GitHub dependency:
plugin_requires = [
    "dremel3dpy @ https://github.com/nbetcher/dremel3dpy/archive/refs/heads/feat/add-layer-field.zip"
]

plugin_additional_data = []
plugin_additional_packages = []
plugin_ignored_packages = []
plugin_python_requires = ">=3.7,<4"

setup(
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    author_email=plugin_author_email,
    url=plugin_url,
    license=plugin_license,
    packages=[plugin_package],
    package_data={plugin_package: ["templates/*.jinja2", "static/js/*.js"]},
    include_package_data=True,
    install_requires=plugin_requires,
    python_requires=plugin_python_requires,
    entry_points={
        "octoprint.plugin": [f"{plugin_identifier} = {plugin_package}"]
    },
)

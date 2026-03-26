# -*- coding: utf-8 -*-
from setuptools import find_packages, setup

plugin_identifier = "dremel3d45"
plugin_package = "octoprint_dremel3d45"
plugin_name = "OctoPrint-Dremel3D45"
plugin_version = "1.0.0"
plugin_description = "Virtual driver for Dremel 3D45 printer over network (REST API)"
plugin_author = "Nick Betcher"
plugin_author_email = "nick@nickbetcher.com"
plugin_url = "https://www.nickbetcher.com/projects/octoprint_dremel3d45"
plugin_license = "MIT"

# Read long description from README
try:
    with open("README.md", "r", encoding="utf-8") as f:
        plugin_long_description = f.read()
except FileNotFoundError:
    plugin_long_description = plugin_description

# We vendor a minimal copy of dremel3dpy inside the plugin package to avoid
# heavyweight binary dependencies (NumPy/OpenBLAS) on OctoPrint hosts.
plugin_requires = [
    "requests>=2.0",
    "validators>=0.0",
]

plugin_additional_data = []
plugin_additional_packages = []
plugin_ignored_packages = []
plugin_python_requires = ">=3.7,<4"

setup(
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    long_description=plugin_long_description,
    long_description_content_type="text/markdown",
    author=plugin_author,
    author_email=plugin_author_email,
    url=plugin_url,
    license=plugin_license,
    packages=find_packages(include=[plugin_package, f"{plugin_package}.*"]),
    package_data={plugin_package: ["templates/*.jinja2", "static/js/*.js"]},
    include_package_data=True,
    install_requires=plugin_requires,
    python_requires=plugin_python_requires,
    entry_points={
        "octoprint.plugin": [f"{plugin_identifier} = {plugin_package}"]
    },
)

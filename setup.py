#!/usr/bin/env python3
"""
Setup script for flp-crawler
"""
from setuptools import setup, find_packages
from pathlib import Path

# Read the long description from README
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text() if readme_file.exists() else ""

setup(
    name="flp-gatherer",
    version="1.0.0",
    description="Collect logs from heterogeneous nodes using rsync and systemd journal",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Marco Concas",
    author_email="marco.concas@cern.ch",
    url="https://github.com/mconcas/log-puller",
    packages=find_packages(),
    python_requires=">=3.7",
    install_requires=[
        "PyYAML>=5.1",
    ],
    entry_points={
        "console_scripts": [
            "flp-gatherer=src.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: System :: Logging",
        "Topic :: System :: Monitoring",
    ],
    keywords="logs rsync systemd journal collection monitoring",
    include_package_data=True,
    zip_safe=False,
)

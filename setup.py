"""Setup for telegram-export"""

from setuptools import setup, find_packages
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

with open("README.rst", "r") as readme:
    desc=readme.read()

with open("requirements.txt", "r") as req:
    requires=req.read()

setup(
    name='telegram-export',
    license="MPL 2.0",
    version='1.9.2',
    description='A tool to download Telegram data (users, chats, messages, '
                'and media) into a database (and display the saved data).',
    long_description=desc,
    url='https://github.com/gumblex/telegram-export',
    author='expectocode, Lonami, Sascha Markus, gumblex',
    author_email='expectocode@gmail.com',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)',
        'Programming Language :: Python :: 3'
    ],
    keywords='Telegram messaging database',
    packages=find_packages(),
    install_requires=requires,
    scripts=['bin/telegram-export'],
    test_suite='telegram_export.tests',
    project_urls={
        'Bug Reports': 'https://github.com/gumblex/telegram-export/issues',
        'Source': 'https://github.com/gumblex/telegram-export'
    }
)

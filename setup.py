from setuptools import setup, find_packages


setup(
    name='futami',
    version='0.0.1',
    description='4Chan IRC bridge',
    long_description='',
    url='https://github.com/ATRAN2/Futami',
    author='Andy',
    license='GPL2',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Topic :: Internet',
    ],
    keywords='4chan IRC bridge',
    packages=find_packages(),
    install_requires=[
        'requests==2.6.0',
        'retrying==1.3.3',
    ],
    tests_require=[
        'pep8==1.6.2',
        'pyflakes==0.8.1',
        'nose==1.3.4',
    ],
    test_suite = 'nose.collector',
    entry_points={
        'console_scripts': [
            'mami-server=futami.mami:main',
        ],
    },
)

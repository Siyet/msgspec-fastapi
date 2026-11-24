import sys
import sysconfig

from setuptools import Extension, setup

# Free-threaded builds have no limited API support before Python 3.15.
free_threaded = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
limited_api = (
    {}
    if free_threaded
    else {
        'define_macros': [('Py_LIMITED_API', '0x030b0000')],
        'py_limited_api': True,
    }
)

setup(
    ext_modules=[
        Extension(
            'msgspec_fastapi._defaults',
            ['src/msgspec_fastapi/_defaults.c', 'src/msgspec_fastapi/_defaults_copy.c'],
            extra_compile_args=['/std:c11'] if sys.platform == 'win32' else [],
            **limited_api,
        )
    ],
    options={} if free_threaded else {'bdist_wheel': {'py_limited_api': 'cp311'}},
)

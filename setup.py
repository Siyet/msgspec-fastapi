import sys

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            'msgspec_fastapi._defaults',
            ['src/msgspec_fastapi/_defaults.c', 'src/msgspec_fastapi/_defaults_copy.c'],
            define_macros=[('Py_LIMITED_API', '0x030b0000')],
            py_limited_api=True,
            extra_compile_args=['/std:c11'] if sys.platform == 'win32' else [],
        )
    ],
    options={'bdist_wheel': {'py_limited_api': 'cp311'}},
)

# Copyright 2019 The JAX, M.D. Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import setuptools


INSTALL_REQUIRES = [
    'absl-py',
    'numpy',
    'jax',
]

setuptools.setup(
    name='jax-md',
    version='0.0.0',
    license='Apache 2.0',
    author='jaxmd',
    author_email='jaxmd.iclr2020@google.com',
    install_requires=INSTALL_REQUIRES,
    url='https://github.com/jax-md/jax-md',
    packages=setuptools.find_packages()
    )

from setuptools import setup, find_packages

setup(
    name="langchain-ibm",
    version="0.1.0",
    description="A example Python package",
    url="https://github.com/nacartwright/langchain-ibm",
    author="Nathan Cartwright",
    author_email="nathan.cartwright@cdw.com",
    license="MIT",
    packages=find_packages(),
    install_requires=["ibm_watsonx_ai", "langchain_core", "jsonschema"],
)

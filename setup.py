from setuptools import find_namespace_packages, setup

setup(
    name="own-api",
    version="0.1.0",
    description="API'si olmayan sitelerden kendi Chrome'unuz üzerinden API üretir.",
    python_requires=">=3.10",
    packages=find_namespace_packages(include=["own_api*", "core*"]),
    install_requires=[
        "click",
        "playwright",
        "anthropic",
        "openai",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "pydantic",
        "mcp",
        "cryptography",
    ],
    entry_points={
        'console_scripts': [
            'own-api=own_api.cli:cli',
        ],
    },
)

from setuptools import find_packages, setup


setup(
    name="home-agent",
    version="0.1.0",
    description="Modular home AI agent framework (LLM + integrations + scheduler).",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.8",
    install_requires=[
        "pydantic>=2.6",
        "pydantic-settings>=2.2",
        "httpx>=0.27",
        "tenacity>=8.2",
        "apscheduler>=3.10",
        "structlog>=24.1",
        "rich>=13.7",
        "typer>=0.12",
        "paho-mqtt>=2.0",
        "psycopg[binary]>=3.2",
    ],
    extras_require={
        "sonos": ["soco>=0.30"],
        "dev": ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.8"],
    },
    entry_points={"console_scripts": ["home-agent=home_agent.cli:app"]},
)


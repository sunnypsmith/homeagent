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
        "astral>=3.2",
    ],
    extras_require={
        "sonos": ["soco>=0.30"],
        "camect": ["camect-py>=0.2.1"],
        "caseta": ["pylutron-caseta>=0.26.0", "pylutron-caseta[cli]>=0.26.0"],
        "gcal": ["icalendar>=6.0.0", "recurring-ical-events>=2.3.0"],
        "ui": ["fastapi>=0.110", "uvicorn>=0.27"],
        "dashboard": ["playwright>=1.40"],
        "snmp": ["pysnmp"],
        "net": ["pythonping>=1.1.4"],
        "voice": ["pvporcupine>=3.0", "webrtcvad>=2.0.10", "noisereduce>=3.0", "numpy>=1.24", "websockets>=12.0"],
        "llm-anthropic": ["anthropic>=0.40"],
        "dev": ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.8"],
    },
    entry_points={"console_scripts": ["home-agent=home_agent.cli:app"]},
)

# src/crypto_bot/cli.py

import asyncio
import sys
from pathlib import Path

import click

from crypto_bot.config.settings import load_settings
from crypto_bot.core.exceptions import ConfigError
from crypto_bot.orchestrator import run_orchestrator


@click.command()
@click.option(
    "--config",
    default="config/settings.yaml",
    help="Path to settings YAML file",
)
def main(config: str) -> None:
    """Запуск крипто-бота."""
    try:
        config_obj = load_settings(yaml_path=Path(config))
        settings = config_obj.settings

        click.echo(f"🚀 crypto_bot started in {settings.runtime.mode} mode")
        click.echo(f"Exchange: {settings.exchange.name} (sandbox: {settings.exchange.sandbox})")

        from crypto_bot.core.validators import validate_runtime_safety

        validate_runtime_safety(config_obj)
        asyncio.run(run_orchestrator(config_obj))

    except ConfigError as e:
        click.echo(f"❌ Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        click.echo(f"💥 Fatal error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
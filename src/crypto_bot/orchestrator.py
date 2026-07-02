# src/crypto_bot/orchestrator.py

import asyncio
import time
from datetime import datetime

from crypto_bot.config.settings import Settings
from crypto_bot.core.exceptions import OrchestratorError
from crypto_bot.core.logging_setup import get_logger
from crypto_bot.core.policy import Policy
from crypto_bot.data.feed import MarketFeed
from crypto_bot.features.builder import FeatureBuilder
from crypto_bot.storage.db import Database
from crypto_bot.strategy.signal_engine import SignalEngine

logger = get_logger(__name__)


async def run_orchestrator(settings: Settings) -> None:
    """Main trading loop."""
    logger.info("Starting orchestrator in %s mode", settings.runtime.mode)

    db = Database(settings.storage.db_path)
    await db.initialize()

    feed = MarketFeed(settings)
    feature_builder = FeatureBuilder()
    signal_engine = SignalEngine(settings)
    policy = Policy(settings)

    try:
        while True:
            cycle_start = time.time()
            logger.info("Starting new scan cycle at %s", datetime.utcnow())

            try:
                # 1. Получаем данные
                market_data = await feed.fetch_multi_tf_data()

                # 2. Строим признаки
                candidates = []
                for symbol, candles in market_data.items():
                    features = feature_builder.build(candles)
                    if features is None:
                        continue

                    # 3. Генерируем сигнал
                    signal_result = signal_engine.analyze(features)
                    if signal_result.signal == "HOLD":
                        continue

                    candidates.append((symbol, signal_result, features))

                # 4. Скоринг + фильтры + risk
                scored = signal_engine.score_candidates(candidates)
                approved = policy.filter_and_rank(scored)

                # 5. Исполнение (signal_only / paper)
                for candidate in approved:
                    await execute_signal(candidate, settings, db)

            except Exception as e:  # noqa: BLE001
                logger.error("Cycle error: %s", e, exc_info=True)

            # Sleep until next cycle
            elapsed = time.time() - cycle_start
            sleep_time = max(0, settings.runtime.loop_interval_seconds - elapsed)
            await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.info("Orchestrator stopped gracefully")
    except Exception as e:
        logger.critical("Orchestrator crashed", exc_info=True)
        raise OrchestratorError("Orchestrator failure") from e


async def execute_signal(candidate, settings, db):
    """Placeholder for signal execution."""
    logger.info("Signal: %s %s", candidate[1].signal, candidate[0])
    # TODO: paper / live execution in later stages
    await db.save_decision(...)  # placeholder
